"""Team coordinator: a transactional coordinator within one authority domain
(baseline section 11, "Fast agreement and ledger choices").

For every mission task it runs the sequence: discover -> propose -> collect bids ->
allocate -> reserve (fenced, all-or-nothing, sorted) -> readiness for this revision ->
record commitment (ledger) -> dispatch -> receive outcome -> verify -> release only when
both logical and physical release conditions hold. Lost or uncertain outcomes become
UNRESOLVED and are settled by querying the participant, never by re-dispatching.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts.execution import CommandState, ExecutionResult
from ..contracts.resources import Occupancy, ResourceReservation
from ..core.clock import Clock
from ..core.errors import PlanError, ResourceConflict
from ..core.ids import new_id
from ..provenance import tracing
from ..provenance.ledger import Ledger
from ..runtime.resources import ResourceManager
from ..runtime.trace import Trace
from ..skills import SkillRegistry
from .allocation import Allocator, Bid, LowestCostAllocator
from .protocol import (
    Assignment,
    CoordinationMessage,
    MessageKind,
    Mission,
    MissionTask,
    TaskState,
    TeamPlan,
    Unassigned,
    check_task_transition,
)
from .transport import Transport

__requirements__ = ("REQ06", "REQ07", "REQ09", "REQ10")

COORDINATOR = "coordinator"
T = TaskState


@dataclass
class CoordinatorTimeouts:
    bid_s: float = 0.5
    ready_s: float = 0.5
    ack_s: float = 0.5
    result_margin_s: float = 2.0
    default_result_s: float = 10.0
    query_s: float = 0.5
    query_retries: int = 2
    commit_retries: int = 2
    reservation_wait_s: float = 5.0
    max_reallocations: int = 2


@dataclass
class ParticipantInfo:
    endpoint: str
    principal: str
    skills: frozenset[str]
    capability_revision: int
    capabilities: dict[str, str]


@dataclass
class TaskRecord:
    task: MissionTask
    state: TaskState = T.PENDING
    revision: int = 0
    assignee: str | None = None
    excluded: set[str] = field(default_factory=set)
    reservations: list[ResourceReservation] = field(default_factory=list)
    command_id: str | None = None
    result: ExecutionResult | None = None
    reason: str | None = None
    history: list[tuple[float, str, int, str]] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.task.task_id


class MissionState(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNRESOLVED = "unresolved"


@dataclass
class MissionResult:
    mission_id: str
    state: MissionState
    tasks: dict[str, TaskRecord]
    plan: TeamPlan
    trace_id: str | None = None

    @property
    def verified(self) -> bool:
        return self.state is MissionState.VERIFIED

    def summary(self) -> str:
        lines = [f"mission {self.mission_id}: {self.state.value}"]
        for r in self.tasks.values():
            lines.append(f"  {r.task_id:<12} {r.task.skill_id:<16} {r.assignee or '-':<12} "
                         f"{r.state.value:<10} rev{r.revision}"
                         + (f"  ({r.reason})" if r.reason else ""))
        return "\n".join(lines)


class TeamCoordinator:
    endpoint = COORDINATOR

    def __init__(self, transport: Transport, resources: ResourceManager, skills: SkillRegistry,
                 clock: Clock, trace: Trace, *, ledger: Ledger | None = None,
                 allocator: Allocator | None = None,
                 timeouts: CoordinatorTimeouts | None = None) -> None:
        self.transport = transport
        self.resources = resources
        self.skills = skills
        self.clock = clock
        self.trace = trace
        self.ledger = ledger if ledger is not None else Ledger()
        self.allocator = allocator or LowestCostAllocator()
        self.timeouts = timeouts or CoordinatorTimeouts()
        self.directory: dict[str, ParticipantInfo] = {}
        self.occupants: dict[str, str] = {}
        self._seen: set[str] = set()
        self._inbox: dict[tuple[str, str], asyncio.Queue[CoordinationMessage]] = {}
        self._participant_locks: dict[str, asyncio.Lock] = {}
        self._records: dict[str, dict[str, TaskRecord]] = {}
        self._announced = asyncio.Event()
        transport.register(self.endpoint, self.handle)

    # -- inbound -----------------------------------------------------------------------
    async def handle(self, msg: CoordinationMessage) -> None:
        if msg.message_id in self._seen:
            self.trace.record("duplicate_message_ignored", self.endpoint,
                              message_id=msg.message_id, message_kind=msg.kind.value,
                              sender=msg.sender)
            return
        self._seen.add(msg.message_id)
        if msg.kind is MessageKind.ANNOUNCE:
            p = msg.payload
            self.directory[msg.sender] = ParticipantInfo(
                endpoint=msg.sender, principal=str(p["principal"]),
                skills=frozenset(p.get("skills", [])),
                capability_revision=int(p.get("capability_revision", 0)),
                capabilities=dict(p.get("capabilities", {})))
            self.trace.record("participant_announced", msg.sender,
                              skills=sorted(p.get("skills", [])))
            self._announced.set()
            return
        self._queue(msg.conversation_id, msg.task_id).put_nowait(msg)

    async def wait_for_participants(self, endpoints: Iterable[str],
                                    timeout: float = 2.0) -> None:
        wanted = set(endpoints)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not wanted <= set(self.directory):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PlanError("participants did not announce",
                                missing=sorted(wanted - set(self.directory)))
            self._announced.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._announced.wait(), remaining)

    def _queue(self, conversation: str, task_id: str) -> asyncio.Queue[CoordinationMessage]:
        return self._inbox.setdefault((conversation, task_id), asyncio.Queue())

    async def _collect(self, conversation: str, task_id: str, revision: int,
                       senders: set[str], kinds: set[MessageKind],
                       timeout: float) -> dict[str, CoordinationMessage]:
        """Collect one reply of ``kinds`` from each sender for exactly this revision."""
        got: dict[str, CoordinationMessage] = {}
        queue = self._queue(conversation, task_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while set(got) < senders:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                break
            if msg.task_revision != revision:
                self.trace.record("stale_revision_ignored", f"{conversation}/{task_id}",
                                  sender=msg.sender, message_kind=msg.kind.value,
                                  message_revision=msg.task_revision, current=revision)
                continue
            if msg.sender not in senders or msg.kind not in kinds:
                if msg.kind is MessageKind.STARTED:
                    self._started(conversation, task_id, msg)
                continue
            got[msg.sender] = msg
        return got

    def _started(self, conversation: str, task_id: str, msg: CoordinationMessage) -> None:
        rec = self._records.get(conversation, {}).get(task_id)
        if rec is not None and rec.state is T.COMMITTED:
            self._move(conversation, rec, T.EXECUTING, "participant started")

    # -- outbound ----------------------------------------------------------------------
    async def _send(self, conversation: str, rec_or_task: TaskRecord | str, revision: int,
                    recipients: Iterable[str], kind: MessageKind, **payload: Any) -> str:
        task_id = rec_or_task.task_id if isinstance(rec_or_task, TaskRecord) else rec_or_task
        ctx = tracing.current()
        message_id = new_id("msg")
        await self.transport.send(CoordinationMessage(
            message_id=message_id, conversation_id=conversation, task_id=task_id,
            task_revision=revision, sender=self.endpoint, recipients=tuple(recipients),
            kind=kind, sent_at=self.clock.now(), payload=payload,
            trace_id=ctx.trace_id if ctx else None))
        return message_id

    # -- planning ------------------------------------------------------------------------
    def _candidates(self, task: MissionTask, assigned: dict[str, str],
                    exclude: set[str]) -> list[str]:
        if task.assign_to:
            return [] if task.assign_to in exclude else [task.assign_to]
        if task.colocated_with:
            who = assigned.get(task.colocated_with)
            return [] if who is None or who in exclude else [who]
        return sorted(e for e, info in self.directory.items()
                      if task.skill_id in info.skills and e not in exclude)

    async def _auction(self, conversation: str, task: MissionTask, revision: int,
                       candidates: list[str]) -> tuple[Bid | None, list[Bid], dict[str, str]]:
        if not candidates:
            return None, [], {}
        await self._send(conversation, task.task_id, revision, candidates, MessageKind.PROPOSE,
                         skill_id=task.skill_id, args=task.args, priority=task.priority)
        replies = await self._collect(conversation, task.task_id, revision, set(candidates),
                                      {MessageKind.BID, MessageKind.DECLINE},
                                      self.timeouts.bid_s)
        bids = [Bid(task.task_id, s, float(m.payload["cost"]),
                    int(m.payload.get("capability_revision", 0)))
                for s, m in replies.items() if m.kind is MessageKind.BID]
        declines = {s: str(m.payload.get("reason", "")) for s, m in replies.items()
                    if m.kind is MessageKind.DECLINE}
        for s in set(candidates) - set(replies):
            declines[s] = "no_reply"
        return self.allocator.choose(task, bids), bids, declines

    async def plan(self, mission: Mission) -> TeamPlan:
        """Allocate every task by auction among participants that advertise its skill."""
        with tracing.span(f"team_plan:{mission.mission_id}") as ctx:
            assigned: dict[str, str] = {}
            assignments, unassigned = [], []
            for task in mission.tasks:
                candidates = self._candidates(task, assigned, set())
                if not candidates:
                    reason = ("colocated_task_unassigned" if task.colocated_with
                              else "no_participant_supports_skill")
                    unassigned.append(Unassigned(task.task_id, reason))
                    continue
                choice, bids, declines = await self._auction(mission.mission_id, task, 1,
                                                             candidates)
                if choice is None:
                    unassigned.append(Unassigned(task.task_id, "all_declined", declines))
                    continue
                assigned[task.task_id] = choice.participant
                assignments.append(Assignment(
                    task.task_id, choice.participant, choice.cost, choice.capability_revision,
                    tuple((b.participant, b.cost) for b in bids),
                    rationale=f"{self.allocator.name}; declined: {declines}" if declines
                    else self.allocator.name))
            plan = TeamPlan(plan_id=new_id("tplan"), mission=mission,
                            assignments=tuple(assignments), unassigned=tuple(unassigned),
                            trace_id=ctx.trace_id)
            self.ledger.append("coordination.planned", mission.mission_id, {
                "plan_id": plan.plan_id,
                "assignments": {a.task_id: a.participant for a in assignments},
                "unassigned": {u.task_id: u.reason for u in unassigned}})
            self.trace.record("team_planned", mission.mission_id, plan_id=plan.plan_id,
                              assignments={a.task_id: a.participant for a in assignments},
                              unassigned={u.task_id: u.reason for u in unassigned})
            return plan

    # -- execution -----------------------------------------------------------------------
    async def execute(self, plan: TeamPlan) -> MissionResult:
        if not plan.complete:
            raise PlanError("team plan has unassigned tasks",
                            unassigned={u.task_id: u.reason for u in plan.unassigned})
        mission = plan.mission
        records: dict[str, TaskRecord] = {}
        for t in mission.tasks:
            rec = TaskRecord(t)
            records[t.task_id] = rec
            self._records.setdefault(mission.mission_id, {})[t.task_id] = rec
            rec.revision = 1
            self._move(mission.mission_id, rec, T.PROPOSED, "planned")
            rec.assignee = plan.assignee(t.task_id)
            self._move(mission.mission_id, rec, T.ACCEPTED, f"assigned to {rec.assignee}")
        return await self._drive(plan, records)

    async def resume(self, result: MissionResult) -> MissionResult:
        """Reconcile UNRESOLVED tasks by querying their participants, then continue."""
        with tracing.span(f"mission:{result.mission_id}", trace_id=result.trace_id):
            for rec in result.tasks.values():
                if rec.state is T.UNRESOLVED:
                    await self._reconcile(result.mission_id, rec)
            return await self._drive(result.plan, result.tasks)

    async def _drive(self, plan: TeamPlan, records: dict[str, TaskRecord]) -> MissionResult:
        mission = plan.mission
        with tracing.span(f"mission:{mission.mission_id}", trace_id=plan.trace_id) as ctx:
            running: dict[str, asyncio.Task[None]] = {}
            while True:
                for rec in records.values():
                    if rec.state is not T.ACCEPTED or rec.task_id in running:
                        continue
                    deps = [records[d] for d in rec.task.depends_on]
                    if any(d.state in (T.FAILED, T.CANCELLED) for d in deps):
                        self._move(mission.mission_id, rec, T.CANCELLED, "dependency failed")
                        rec.reason = "dependency_not_verified"
                    elif all(d.state is T.VERIFIED for d in deps):
                        running[rec.task_id] = asyncio.get_running_loop().create_task(
                            self._run_task(mission, rec, records))
                if not running:
                    break
                done, _ = await asyncio.wait(running.values(),
                                             return_when=asyncio.FIRST_COMPLETED)
                for tid in [k for k, v in running.items() if v in done]:
                    running.pop(tid).result()
            states = {r.state for r in records.values()}
            if T.UNRESOLVED in states:
                state = MissionState.UNRESOLVED
            elif states == {T.VERIFIED}:
                state = MissionState.VERIFIED
            else:
                state = MissionState.FAILED
            self.ledger.append("coordination.mission_finished", mission.mission_id,
                               {"state": state.value,
                                "tasks": {r.task_id: r.state.value for r in records.values()}})
            self.trace.record("mission_finished", mission.mission_id, state=state.value)
            return MissionResult(mission.mission_id, state, records, plan, ctx.trace_id)

    async def _run_task(self, mission: Mission, rec: TaskRecord,
                        records: dict[str, TaskRecord]) -> None:
        conv = mission.mission_id
        skill = self.skills.get(rec.task.skill_id)
        with tracing.span(f"task:{rec.task_id}"):
            for _attempt in range(self.timeouts.max_reallocations + 1):
                assert rec.assignee is not None
                participant = rec.assignee
                lock = self._participant_locks.setdefault(participant, asyncio.Lock())
                async with lock:
                    outcome = await self._attempt(conv, rec, records, skill.resources_for(
                        rec.task.args), (skill.execution.deadline_s or
                                         self.timeouts.default_result_s))
                if outcome != "reallocate":
                    return
                if not await self._reallocate(conv, rec, records):
                    return
            self._fail(conv, rec, "reallocation_limit")

    async def _attempt(self, conv: str, rec: TaskRecord, records: dict[str, TaskRecord],
                       resources: tuple[str, ...], deadline_s: float) -> str:
        participant = rec.assignee
        assert participant is not None
        info = self.directory[participant]
        # 1. Reserve all exclusive resources (sorted -> no circular wait), bounded wait.
        try:
            rec.reservations = await self._reserve_all(sorted(set(resources)), info.principal)
        except ResourceConflict as exc:
            self._fail(conv, rec, "reservation_timeout", **exc.details)
            return "done"
        self._move(conv, rec, T.RESERVED, ", ".join(
            f"{r.resource}#{r.fencing_token}" for r in rec.reservations) or "no resources")

        # 2. Readiness for this exact revision.
        await self._send(conv, rec, rec.revision, [participant], MessageKind.READY_CHECK,
                         skill_id=rec.task.skill_id, args=rec.task.args,
                         capability_revision=info.capability_revision)
        reply = (await self._collect(conv, rec.task_id, rec.revision, {participant},
                                     {MessageKind.READY, MessageKind.NOT_READY},
                                     self.timeouts.ready_s)).get(participant)
        if reply is None or reply.kind is MessageKind.NOT_READY:
            why = "no_reply" if reply is None else str(reply.payload.get("reason"))
            if reply is not None and "capability_revision" in reply.payload:
                info.capability_revision = int(reply.payload["capability_revision"])
            self.trace.record("team_not_ready", f"{conv}/{rec.task_id}",
                              participant=participant, reason=why, revision=rec.revision)
            rec.excluded.add(participant)
            rec.reason = f"not_ready:{why}"
            self._release_eligible(records)
            return "reallocate"
        self._move(conv, rec, T.READY, f"{participant} ready")

        # 3. Commit (ledgered) and dispatch.
        rec.command_id = f"cmd-{conv}-{rec.task_id}-r{rec.revision}"
        fencing = {r.resource: r.fencing_token for r in rec.reservations}
        self.ledger.append("coordination.committed", f"{conv}/{rec.task_id}", {
            "revision": rec.revision, "assignee": participant, "skill_id": rec.task.skill_id,
            "args": rec.task.args, "fencing": fencing, "command_id": rec.command_id})
        self._move(conv, rec, T.COMMITTED, rec.command_id)
        acked = False
        for _ in range(self.timeouts.commit_retries + 1):
            await self._send(conv, rec, rec.revision, [participant], MessageKind.COMMIT,
                             skill_id=rec.task.skill_id, args=rec.task.args, fencing=fencing,
                             command_id=rec.command_id)
            got = await self._collect(conv, rec.task_id, rec.revision, {participant},
                                      {MessageKind.ACK, MessageKind.RESULT},
                                      self.timeouts.ack_s)
            if participant in got:
                acked = True
                if got[participant].kind is MessageKind.RESULT:
                    await self._settle(conv, rec, got[participant], records)
                    return "done"
                break
            self.trace.record("team_commit_retry", f"{conv}/{rec.task_id}",
                              participant=participant)

        # 4. Outcome. Wait for the result; if it never comes, query; else UNRESOLVED.
        msg = None
        if acked:
            msg = (await self._collect(conv, rec.task_id, rec.revision, {participant},
                                       {MessageKind.RESULT},
                                       deadline_s + self.timeouts.result_margin_s)
                   ).get(participant)
        if msg is not None:
            await self._settle(conv, rec, msg, records)
        else:
            await self._reconcile(conv, rec, records,
                                  reason="no_acknowledgement" if not acked else "result_lost")
        return "done"

    async def _settle(self, conv: str, rec: TaskRecord, msg: CoordinationMessage,
                      records: dict[str, TaskRecord]) -> None:
        """Apply a reported result; if the robot itself could not determine the outcome,
        ask it to reconcile by observation (never by re-dispatching)."""
        self._apply_result(conv, rec, msg, records)
        if rec.state is T.UNRESOLVED:
            await self._reconcile(conv, rec, records, reason=rec.reason or "unresolved")

    async def _reconcile(self, conv: str, rec: TaskRecord,
                         records: dict[str, TaskRecord] | None = None,
                         reason: str = "unresolved") -> None:
        records = records if records is not None else self._records.get(conv, {})
        participant = rec.assignee
        assert participant is not None and rec.command_id is not None
        for _ in range(self.timeouts.query_retries + 1):
            await self._send(conv, rec, rec.revision, [participant], MessageKind.QUERY,
                             command_id=rec.command_id, reconcile=True)
            got = (await self._collect(conv, rec.task_id, rec.revision, {participant},
                                       {MessageKind.STATUS, MessageKind.RESULT},
                                       self.timeouts.query_s)).get(participant)
            if got is None:
                continue
            self._observe_occupancy(got.payload.get("occupancy", {}))
            if got.kind is MessageKind.RESULT:
                self._apply_result(conv, rec, got, records)
                return
            if got.payload.get("known") is False:
                # The participant never received the commitment: nothing executed.
                if rec.state is not T.UNRESOLVED:
                    self._move(conv, rec, T.UNRESOLVED, "commitment never received")
                self._move(conv, rec, T.FAILED, "not executed")
                rec.reason = "commitment_not_received"
                self._release_eligible(records)
                return
            if not got.payload.get("done"):
                await asyncio.sleep(self.timeouts.query_s)  # still executing: ask again
                continue
            result = ExecutionResult.from_dict(got.payload["result"])
            if result.state is not CommandState.UNRESOLVED:
                self._apply_result(conv, rec, got, records)
                return
            break  # the participant could not settle it either
        if rec.state is not T.UNRESOLVED:
            self._move(conv, rec, T.UNRESOLVED, reason)
        rec.reason = reason
        self.ledger.append("coordination.unresolved", f"{conv}/{rec.task_id}",
                           {"reason": reason, "assignee": participant,
                            "held": [r.resource for r in rec.reservations]})

    def _apply_result(self, conv: str, rec: TaskRecord, msg: CoordinationMessage,
                      records: dict[str, TaskRecord]) -> None:
        result = ExecutionResult.from_dict(msg.payload["result"])
        rec.result = result
        self._observe_occupancy(msg.payload.get("occupancy", {}))
        if rec.state in (T.COMMITTED, T.EXECUTING):
            self._move(conv, rec, T.COMPLETED, f"reported {result.state.value}")
        s = result.state
        if s is CommandState.VERIFIED:
            self._move(conv, rec, T.VERIFIED, result.reason or "postconditions observed")
            rec.reason = result.reason
            self.ledger.append("coordination.verified", f"{conv}/{rec.task_id}",
                               {"command_id": result.command_id, "assignee": rec.assignee,
                                "verification": [(c.description, c.satisfied)
                                                 for c in result.verification]})
        elif s is CommandState.UNRESOLVED:
            if rec.state is not T.UNRESOLVED:
                self._move(conv, rec, T.UNRESOLVED, result.reason or "unresolved")
            rec.reason = result.reason
            self.ledger.append("coordination.unresolved", f"{conv}/{rec.task_id}",
                               {"reason": result.reason, "assignee": rec.assignee})
            return
        elif s is CommandState.CANCELLED:
            self._move(conv, rec, T.CANCELLED, result.reason or "cancelled")
            rec.reason = result.reason
        else:
            self._fail(conv, rec, result.reason or s.value)
        self._release_eligible(records)

    def _fail(self, conv: str, rec: TaskRecord, reason: str, **details: Any) -> None:
        self._move(conv, rec, T.FAILED, reason)
        rec.reason = reason
        self.ledger.append("coordination.failed", f"{conv}/{rec.task_id}",
                           {"reason": reason, "assignee": rec.assignee, **details})

    async def _reallocate(self, conv: str, rec: TaskRecord,
                          records: dict[str, TaskRecord]) -> bool:
        assigned = {r.task_id: r.assignee for r in records.values() if r.assignee}
        candidates = self._candidates(rec.task, {k: v for k, v in assigned.items() if v},
                                      rec.excluded)
        rec.revision += 1
        self._move(conv, rec, T.PROPOSED, f"reallocating ({rec.reason})")
        choice, _bids, declines = await self._auction(conv, rec.task, rec.revision, candidates)
        if choice is None:
            self._fail(conv, rec, "no_ready_participant", declines=declines,
                       previous=rec.reason)
            return False
        previous, rec.assignee = rec.assignee, choice.participant
        self.directory[choice.participant].capability_revision = choice.capability_revision
        self._move(conv, rec, T.ACCEPTED, f"reassigned {previous} -> {rec.assignee}")
        self.ledger.append("coordination.reallocated", f"{conv}/{rec.task_id}",
                           {"from": previous, "to": rec.assignee, "revision": rec.revision})
        return True

    # -- resources -------------------------------------------------------------------------
    async def _reserve_all(self, resources: list[str],
                           holder: str) -> list[ResourceReservation]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeouts.reservation_wait_s
        while True:
            before = {r.reservation_id for r in self.resources.held_by(holder)}
            got: list[ResourceReservation] = []
            try:
                for name in resources:
                    got.append(self.resources.reserve(name, holder))
                return got
            except ResourceConflict as exc:
                # All-or-nothing: drop only what this attempt newly took, then wait.
                for taken in got:
                    if taken.reservation_id not in before:
                        self.resources.release(taken.reservation_id)
                if loop.time() >= deadline:
                    raise ResourceConflict("timed out waiting for resources",
                                           resources=resources, **exc.details) from exc
                await asyncio.sleep(0.01)

    def _observe_occupancy(self, occupancy: dict[str, Any]) -> None:
        for resource, occupant in occupancy.items():
            occupant = str(occupant or "")
            self.occupants[resource] = occupant
            self.resources.set_occupancy(resource,
                                         Occupancy.OCCUPIED if occupant else Occupancy.FREE)

    def _release_eligible(self, records: dict[str, TaskRecord]) -> None:
        """Release a participant's reservation only when no unfinished task of theirs
        needs it, no unresolved task of theirs holds it, and it is not physically occupied
        by them (logical release never implies physical vacancy)."""
        def needs(rec: TaskRecord, endpoint: str, resource: str) -> bool:
            if rec.assignee != endpoint or endpoint in rec.excluded:
                return False
            if rec.state is T.UNRESOLVED:
                return any(x.resource == resource for x in rec.reservations)
            if rec.state.terminal:
                return False
            return resource in self.skills.get(rec.task.skill_id).resources_for(rec.task.args)

        by_principal = {i.principal: e for e, i in self.directory.items()}
        for principal, endpoint in by_principal.items():
            for res in self.resources.held_by(principal):
                if any(needs(rec, endpoint, res.resource) for rec in records.values()):
                    continue
                if self.occupants.get(res.resource) == endpoint:
                    continue
                self.resources.release(res.reservation_id)
                self.ledger.append("coordination.released", res.resource,
                                   {"holder": principal, "fencing_token": res.fencing_token,
                                    "occupancy": self.occupants.get(res.resource, "unknown")})

    # -- bookkeeping -----------------------------------------------------------------------
    def _move(self, conv: str, rec: TaskRecord, new: TaskState, note: str = "") -> None:
        check_task_transition(rec.state, new)
        rec.state = new
        rec.history.append((self.clock.now(), new.value, rec.revision, note))
        self.trace.record(f"team_task_{new.value}", f"{conv}/{rec.task_id}",
                          revision=rec.revision, assignee=rec.assignee, note=note)
