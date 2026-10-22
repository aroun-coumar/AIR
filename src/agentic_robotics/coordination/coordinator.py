"""Team coordinator: a transactional coordinator within one authority domain
(baseline section 11, "Fast agreement and ledger choices").

For every mission task it runs the sequence: discover -> propose -> collect bids ->
allocate -> reserve (scoped loans, all-or-nothing, sorted) -> readiness for this
revision -> record commitment (ledger) -> release as an order update -> receive outcome
-> verify -> release resources only when both logical and physical release conditions
hold. Lost or uncertain outcomes become UNRESOLVED and are settled by querying the
participant, never by re-dispatching.

Released prefix vs horizon (VDA 5050): with ``TeamPlan.release_window > 1`` the
coordinator releases several consecutive steps of one participant at once, holding their
resources first. A participant that disconnects finishes only that released prefix; the
horizon is never executed without contact.

Cancellation (ROS 2 actions, VDA 5050 ``cancelOrder``, Open-RMF cancel vs kill): a
cancel is a request with its own response; in-flight tasks go to CANCELLING and become
CANCELLED/KILLED only when the robot confirms the stop (or that the step never started).
An unconfirmed stop is UNRESOLVED and its holds are kept. A cancel is never reported as
success.

Liveness: connection state per participant comes from transport liveliness facts and
heartbeats. When a participant's connection breaks, its holds are kept and its in-flight
tasks become UNRESOLVED; nothing else in the team is reset.
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
from ..runtime.resources import Lease, ResourceManager
from ..runtime.trace import Trace
from ..skills import SkillRegistry
from .allocation import Allocator, Bid, LowestCostAllocator
from .hlc import ClockDriftError, HybridLogicalClock
from .liveness import ConnectionState, LivenessMonitor, SeqStatus, SeqTracker, Stamper
from .orders import OrderStep, OrderUpdate
from .protocol import (
    RESPONSE_KINDS,
    Assignment,
    CancelMode,
    CoordinationMessage,
    InstantAction,
    MessageKind,
    Mission,
    MissionTask,
    TaskState,
    TeamPlan,
    TrustLevel,
    Unassigned,
    check_task_transition,
)
from .transport import LivelinessTransport, Transport

__requirements__ = ("REQ06", "REQ07", "REQ09", "REQ10", "REQ17", "G06", "G07", "G14")

COORDINATOR = "coordinator"
T = TaskState
_IN_FLIGHT = (T.COMMITTED, T.EXECUTING)
_NOT_STARTED = (T.PENDING, T.PROPOSED, T.ACCEPTED)


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
    cancel_s: float = 0.5
    instant_s: float = 0.5
    heartbeat_timeout_s: float | None = None   # None: liveness from transport facts only
    watchdog_s: float = 0.05


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
    released_with: str | None = None    # task whose order update released this one
    update_id: int = 0
    cancel_mode: CancelMode | None = None

    @property
    def task_id(self) -> str:
        return self.task.task_id


class MissionState(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    CANCELLED = "cancelled"


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


@dataclass
class CancelReport:
    """What a cancel request achieved, per participant and command. ``tasks`` is the
    state right after the responses; final states arrive with the results."""

    mission_id: str
    mode: CancelMode
    responses: dict[str, dict[str, Any]]
    tasks: dict[str, str]

    @property
    def rejected(self) -> dict[str, str]:
        return {cid: str(r.get("reason")) for p in self.responses.values()
                for cid, r in p.items() if isinstance(r, dict) and not r.get("accepted")}


@dataclass
class _OrderState:
    order_id: str
    update_id: int = 0                 # last update id sent
    last_released: str | None = None   # last step the participant is known to hold


class _Aborted(Exception):
    pass


class _Cancelled(Exception):
    pass


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
        self.trust: dict[str, TrustLevel] = {}
        self.hlc = HybridLogicalClock(self.endpoint, clock.now)
        self.stamper = Stamper(self.hlc)
        self.seq = SeqTracker()
        self.liveness = LivenessMonitor(clock.now,
                                        heartbeat_timeout_s=self.timeouts.heartbeat_timeout_s)
        self.liveness.on_change(self._connection_changed)
        self._seen: set[str] = set()
        self._inbox: dict[tuple[str, str], asyncio.Queue[CoordinationMessage]] = {}
        self._pending: dict[str, asyncio.Future[CoordinationMessage]] = {}
        self._participant_locks: dict[str, asyncio.Lock] = {}
        self._records: dict[str, dict[str, TaskRecord]] = {}
        self._orders: dict[tuple[str, str], _OrderState] = {}
        self._leases: dict[str, list[Lease]] = {}
        self._link_down: dict[str, asyncio.Event] = {}
        self._aborts: dict[tuple[str, str], asyncio.Event] = {}
        self._cancel: dict[str, CancelMode] = {}
        self._driving: set[str] = set()
        self._running: dict[str, set[str]] = {}
        self._announced = asyncio.Event()
        self._watchdog: asyncio.Task[None] | None = None
        transport.register(self.endpoint, self.handle)
        if isinstance(transport, LivelinessTransport):
            transport.subscribe_liveliness(self.endpoint, self.liveness.liveliness)
            transport.declare_liveliness(self.endpoint)

    # -- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        if self.timeouts.heartbeat_timeout_s is not None and self._watchdog is None:
            self._watchdog = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.timeouts.watchdog_s)
            self.liveness.check()

    # -- trust and liveness ------------------------------------------------------------------
    def trust_of(self, endpoint: str) -> TrustLevel:
        return self.trust.get(endpoint, TrustLevel.UTILIZED)

    def set_trust(self, endpoint: str, level: TrustLevel | str, reason: str = "") -> None:
        level = TrustLevel(level)
        old = self.trust_of(endpoint)
        self.trust[endpoint] = level
        self.ledger.append("coordination.trust_changed", endpoint,
                           {"old": old.value, "new": level.value, "reason": reason})
        self.trace.record("participant_trust", endpoint, old=old.value, new=level.value,
                          reason=reason)

    def connection(self, endpoint: str) -> ConnectionState:
        return self.liveness.state(endpoint)

    def records(self, mission_id: str) -> dict[str, TaskRecord]:
        """Live task records of a mission (also while it executes)."""
        return dict(self._records.get(mission_id, {}))

    def _down(self, endpoint: str) -> asyncio.Event:
        return self._link_down.setdefault(endpoint, asyncio.Event())

    def _connection_changed(self, endpoint: str, old: ConnectionState, new: ConnectionState,
                            cause: str) -> None:
        if new is ConnectionState.ONLINE:
            self._down(endpoint).clear()
        else:
            self._down(endpoint).set()
        in_flight = sorted(f"{conv}/{r.task_id}" for conv, recs in self._records.items()
                           for r in recs.values()
                           if r.assignee == endpoint and r.state in (*_IN_FLIGHT,
                                                                       T.CANCELLING))
        self.ledger.append("coordination.connection", endpoint,
                           {"old": old.value, "new": new.value, "cause": cause,
                            "in_flight": in_flight})
        self.trace.record("participant_connection", endpoint, old=old.value, new=new.value,
                          cause=cause, in_flight=in_flight)

    # -- inbound -----------------------------------------------------------------------
    async def handle(self, msg: CoordinationMessage) -> None:
        if msg.message_id in self._seen:
            self.trace.record("duplicate_message_ignored", self.endpoint,
                              message_id=msg.message_id, message_kind=msg.kind.value,
                              sender=msg.sender)
            return
        self._seen.add(msg.message_id)
        self.liveness.heard(msg.sender)
        verdict = self.seq.observe(msg.sender, msg.seq)
        if verdict.status in (SeqStatus.GAP, SeqStatus.REORDERED):
            self.trace.record(f"message_{verdict.status.value}", msg.sender, seq=msg.seq,
                              expected=verdict.expected, missing=verdict.missing,
                              message_kind=msg.kind.value)
            if verdict.status is SeqStatus.GAP:
                self.ledger.append("coordination.message_gap", msg.sender,
                                   {"seq": msg.seq, "expected": verdict.expected,
                                    "missing": verdict.missing})
        if msg.hlc:
            try:
                self.hlc.update(msg.hlc)
            except ClockDriftError as exc:
                self.trace.record("hlc_drift_rejected", msg.sender, **exc.details)
                return
        if msg.kind is MessageKind.HEARTBEAT:
            return
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
        if self.trust_of(msg.sender) is TrustLevel.IGNORED:
            self.trace.record("report_ignored", msg.sender, message_kind=msg.kind.value,
                              trust=TrustLevel.IGNORED.value)
            return
        if msg.kind in RESPONSE_KINDS and msg.causal_ref in self._pending:
            fut = self._pending.pop(msg.causal_ref)
            if not fut.done():
                fut.set_result(msg)
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

    @staticmethod
    async def _next(queue: asyncio.Queue[CoordinationMessage], timeout: float,
                    aborts: Iterable[asyncio.Event]) -> CoordinationMessage | None:
        """Next message, ``None`` on timeout; raises :class:`_Aborted` if an abort event
        fires first (a message already queued still wins)."""
        events = list(aborts)
        if any(e.is_set() for e in events):
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                raise _Aborted from None
        getter = asyncio.ensure_future(queue.get())
        waiters = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            done, _ = await asyncio.wait([getter, *waiters], timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        if done:
            raise _Aborted
        return None

    async def _collect(self, conversation: str, task_id: str, revision: int,
                       senders: set[str], kinds: set[MessageKind], timeout: float,
                       aborts: Iterable[asyncio.Event] = ()) -> dict[str, CoordinationMessage]:
        """Collect one reply of ``kinds`` from each sender for exactly this revision. Stops
        early (with what it has) if an abort event fires."""
        got: dict[str, CoordinationMessage] = {}
        queue = self._queue(conversation, task_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        aborts = list(aborts)
        while set(got) < senders:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await self._next(queue, remaining, aborts)
            except _Aborted:
                break
            if msg is None:
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
                    recipients: Iterable[str], kind: MessageKind, *, update_id: int = 0,
                    **payload: Any) -> list[str]:
        """Send one message per recipient (each channel has its own sequence)."""
        task_id = rec_or_task.task_id if isinstance(rec_or_task, TaskRecord) else rec_or_task
        ctx = tracing.current()
        ids = []
        for recipient in recipients:
            seq, hlc = self.stamper.stamp(recipient)
            message_id = new_id("msg")
            ids.append(message_id)
            await self.transport.send(CoordinationMessage(
                message_id=message_id, conversation_id=conversation, task_id=task_id,
                task_revision=revision, sender=self.endpoint, recipients=(recipient,),
                kind=kind, sent_at=self.clock.now(), payload=payload,
                trace_id=ctx.trace_id if ctx else None, seq=seq, update_id=update_id,
                hlc=hlc))
        return ids

    async def _request(self, participant: str, conversation: str, kind: MessageKind,
                       timeout: float, **payload: Any) -> CoordinationMessage | None:
        """Send a request whose response is matched by ``causal_ref`` (any revision)."""
        seq, hlc = self.stamper.stamp(participant)
        message_id = new_id("msg")
        fut: asyncio.Future[CoordinationMessage] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = fut
        ctx = tracing.current()
        await self.transport.send(CoordinationMessage(
            message_id=message_id, conversation_id=conversation, task_id="-",
            task_revision=0, sender=self.endpoint, recipients=(participant,), kind=kind,
            sent_at=self.clock.now(), payload=payload,
            trace_id=ctx.trace_id if ctx else None, seq=seq, hlc=hlc))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(message_id, None)

    # -- planning ------------------------------------------------------------------------
    def _candidates(self, task: MissionTask, assigned: dict[str, str],
                    exclude: set[str]) -> list[str]:
        def usable(e: str) -> bool:
            return (e not in exclude and self.trust_of(e).receives_work
                    and self.connection(e) is ConnectionState.ONLINE)

        if task.assign_to:
            return [task.assign_to] if usable(task.assign_to) else []
        if task.colocated_with:
            who = assigned.get(task.colocated_with)
            return [who] if who is not None and usable(who) else []
        return sorted(e for e, info in self.directory.items()
                      if task.skill_id in info.skills and usable(e))

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

    async def plan(self, mission: Mission, *, release_window: int = 1) -> TeamPlan:
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
                            trace_id=ctx.trace_id, release_window=release_window)
            self.ledger.append("coordination.planned", mission.mission_id, {
                "plan_id": plan.plan_id, "release_window": release_window,
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
        conv = mission.mission_id
        self._driving.add(conv)
        running_ids = self._running.setdefault(conv, set())
        try:
            with tracing.span(f"mission:{conv}", trace_id=plan.trace_id) as ctx:
                running: dict[str, asyncio.Task[None]] = {}
                while True:
                    for rec in records.values():
                        if rec.state is not T.ACCEPTED or rec.task_id in running or \
                                rec.released_with is not None:
                            continue
                        deps = [records[d] for d in rec.task.depends_on]
                        if conv in self._cancel:
                            self._stop(conv, rec, "mission_cancelled")
                        elif any(d.state.stopped for d in deps):
                            self._move(conv, rec, T.CANCELLED, "dependency failed")
                            rec.reason = "dependency_not_verified"
                        elif all(d.state is T.VERIFIED for d in deps):
                            running_ids.add(rec.task_id)
                            running[rec.task_id] = asyncio.get_running_loop().create_task(
                                self._run_task(plan, rec, records))
                    if not running:
                        break
                    done, _ = await asyncio.wait(running.values(),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for tid in [k for k, v in running.items() if v in done]:
                        running_ids.discard(tid)
                        running.pop(tid).result()
                self._release_eligible(records)   # e.g. holds of tasks cancelled unstarted
                states = {r.state for r in records.values()}
                if T.UNRESOLVED in states:
                    state = MissionState.UNRESOLVED
                elif states == {T.VERIFIED}:
                    state = MissionState.VERIFIED
                elif conv in self._cancel and states & {T.CANCELLED, T.KILLED}:
                    state = MissionState.CANCELLED
                else:
                    state = MissionState.FAILED
                self.ledger.append("coordination.mission_finished", conv,
                                   {"state": state.value,
                                    "tasks": {r.task_id: r.state.value
                                              for r in records.values()}})
                self.trace.record("mission_finished", conv, state=state.value)
                return MissionResult(conv, state, records, plan, ctx.trace_id)
        finally:
            self._driving.discard(conv)

    async def _run_task(self, plan: TeamPlan, rec: TaskRecord,
                        records: dict[str, TaskRecord]) -> None:
        conv = plan.mission.mission_id
        with tracing.span(f"task:{rec.task_id}"):
            for _attempt in range(self.timeouts.max_reallocations + 1):
                assert rec.assignee is not None
                lock = self._participant_locks.setdefault(rec.assignee, asyncio.Lock())
                async with lock:
                    outcome = await self._attempt(plan, rec, records)
                if outcome != "reallocate":
                    return
                if conv in self._cancel:
                    self._stop(conv, rec, "mission_cancelled")
                    self._release_eligible(records)
                    return
                if not await self._reallocate(conv, rec, records):
                    return
            self._fail(conv, rec, "reallocation_limit")

    def _deadline(self, rec: TaskRecord) -> float:
        skill = self.skills.get(rec.task.skill_id)
        return (skill.execution.deadline_s or self.timeouts.default_result_s) + \
            self.timeouts.result_margin_s

    def _resources_for(self, rec: TaskRecord) -> list[str]:
        return sorted(set(self.skills.get(rec.task.skill_id).resources_for(rec.task.args)))

    async def _attempt(self, plan: TeamPlan, rec: TaskRecord,
                       records: dict[str, TaskRecord]) -> str:
        conv = plan.mission.mission_id
        participant = rec.assignee
        assert participant is not None
        info = self.directory[participant]
        if conv in self._cancel:
            self._stop(conv, rec, "mission_cancelled")
            return "done"
        if not self.trust_of(participant).receives_work or \
                self.connection(participant) is not ConnectionState.ONLINE:
            return self._not_ready(conv, rec, records, participant,
                                   f"trust:{self.trust_of(participant).value}"
                                   if not self.trust_of(participant).receives_work
                                   else f"connection:{self.connection(participant).value}")

        # 1. Reserve all exclusive resources as one scoped loan (bounded wait).
        try:
            lease = await self._acquire(self._resources_for(rec), info.principal, conv)
        except _Cancelled:
            self._stop(conv, rec, "mission_cancelled")
            return "done"
        except ResourceConflict as exc:
            self._fail(conv, rec, "reservation_timeout", conflict=exc.details)
            return "done"
        rec.reservations = lease.reservations
        self._move(conv, rec, T.RESERVED, ", ".join(
            f"{r.resource}#{r.fencing_token}" for r in rec.reservations) or "no resources")
        # 1b. Released prefix: following steps of this participant that can go now.
        ahead = self._extend(plan, rec, records, info.principal)

        # 2. Readiness for this exact revision (covering every step to be released).
        await self._send(conv, rec, rec.revision, [participant], MessageKind.READY_CHECK,
                         skill_id=rec.task.skill_id, args=rec.task.args,
                         also=[r.task.skill_id for r, _ in ahead],
                         capability_revision=info.capability_revision)
        reply = (await self._collect(conv, rec.task_id, rec.revision, {participant},
                                     {MessageKind.READY, MessageKind.NOT_READY},
                                     self.timeouts.ready_s,
                                     [self._down(participant)])).get(participant)
        if reply is None or reply.kind is MessageKind.NOT_READY:
            self._unrelease(ahead)
            why = "no_reply" if reply is None else str(reply.payload.get("reason"))
            if reply is not None and "capability_revision" in reply.payload:
                info.capability_revision = int(reply.payload["capability_revision"])
            return self._not_ready(conv, rec, records, participant, why)
        self._move(conv, rec, T.READY, f"{participant} ready")
        if conv in self._cancel:
            self._unrelease(ahead)
            self._stop(conv, rec, "mission_cancelled")
            self._release_eligible(records)
            return "done"

        # 3. Commit (ledgered) and release the steps as one order update.
        steps = [rec, *(r for r, _ in ahead)]
        order = self._orders.setdefault((conv, participant),
                                        _OrderState(f"{conv}:{participant}"))
        order.update_id += 1
        prefix_ids = {s.task_id for s in steps}
        order_steps = []
        for s in steps:
            if s is not rec:
                s.reservations = next(lease for r, lease in ahead if r is s).reservations
                self._move(conv, s, T.RESERVED, f"released with {rec.task_id}")
                self._move(conv, s, T.READY, f"{participant} ready (released prefix)")
            s.command_id = f"cmd-{conv}-{s.task_id}-r{s.revision}"
            s.update_id = order.update_id
            fencing = {r.resource: r.fencing_token for r in s.reservations}
            order_steps.append(OrderStep(
                task_id=s.task_id, task_revision=s.revision, command_id=s.command_id,
                skill_id=s.task.skill_id, args=dict(s.task.args), fencing=fencing,
                after=tuple(d for d in s.task.depends_on if d in prefix_ids)))
            self.ledger.append("coordination.committed", f"{conv}/{s.task_id}", {
                "revision": s.revision, "assignee": participant,
                "skill_id": s.task.skill_id, "args": s.task.args, "fencing": fencing,
                "command_id": s.command_id, "order_id": order.order_id,
                "update_id": order.update_id, "released_with": s.released_with})
            self._move(conv, s, T.COMMITTED, s.command_id)
        horizon = tuple(t for t in plan.sequence(participant)
                        if t not in prefix_ids and records[t].state in _NOT_STARTED)
        update = OrderUpdate(order_id=order.order_id, update_id=order.update_id,
                             steps=tuple(order_steps), stitch_after=order.last_released,
                             horizon=horizon)
        acked, first = False, None
        for _ in range(self.timeouts.commit_retries + 1):
            await self._send(conv, rec, rec.revision, [participant], MessageKind.COMMIT,
                             update_id=update.update_id, **update.payload())
            got = await self._collect(conv, rec.task_id, rec.revision, {participant},
                                      {MessageKind.ACK, MessageKind.NACK, MessageKind.RESULT,
                                       MessageKind.NOT_EXECUTED},
                                      self.timeouts.ack_s, [self._down(participant)])
            if participant in got:
                msg = got[participant]
                if msg.kind is MessageKind.NACK:
                    self._rejected(conv, steps, msg, records)
                    return "done"
                acked = True
                if msg.kind is not MessageKind.ACK:
                    first = msg
                break
            if self._down(participant).is_set():
                break
            self.trace.record("team_commit_retry", f"{conv}/{rec.task_id}",
                              participant=participant, update_id=update.update_id)
        if acked:
            order.last_released = steps[-1].task_id

        # 4. Outcomes, step by step. Missing results are queried, never re-dispatched.
        for i, s in enumerate(steps):
            outcome: CoordinationMessage | None = first if i == 0 else None
            aborts = [self._down(participant), self._abort(conv, s.task_id)]
            if outcome is None and acked:
                outcome = (await self._collect(
                    conv, s.task_id, s.revision, {participant},
                    {MessageKind.RESULT, MessageKind.NOT_EXECUTED}, self._deadline(s),
                    aborts)).get(participant)
            if outcome is not None:
                await self._settle(conv, s, outcome, records)
            elif self._down(participant).is_set():
                self._unresolved(conv, s, "connection_broken")
            elif self._abort(conv, s.task_id).is_set():
                self._unresolved(conv, s, "killed_stop_unconfirmed")
            else:
                await self._reconcile(conv, s, records,
                                      reason="no_acknowledgement" if not acked
                                      else "result_lost")
                if not acked and s.reason != "commitment_not_received":
                    order.last_released = s.task_id
        return "done"

    def _not_ready(self, conv: str, rec: TaskRecord, records: dict[str, TaskRecord],
                   participant: str, why: str) -> str:
        self.trace.record("team_not_ready", f"{conv}/{rec.task_id}",
                          participant=participant, reason=why, revision=rec.revision)
        rec.excluded.add(participant)
        rec.reason = f"not_ready:{why}"
        self._release_eligible(records)
        return "reallocate"

    def _extend(self, plan: TeamPlan, rec: TaskRecord, records: dict[str, TaskRecord],
                principal: str) -> list[tuple[TaskRecord, Lease]]:
        """Pick the following steps of the same participant to release with ``rec``:
        contiguous in its sequence, dependencies verified or inside the prefix, and
        their resources acquirable right now (a released step must already hold them)."""
        if plan.release_window <= 1:
            return []
        conv = plan.mission.mission_id
        assert rec.assignee is not None
        sequence = list(plan.sequence(rec.assignee))
        prefix = {rec.task_id}
        out: list[tuple[TaskRecord, Lease]] = []
        for task_id in sequence[sequence.index(rec.task_id) + 1:]:
            if len(out) + 1 >= plan.release_window:
                break
            nxt = records[task_id]
            if nxt.state is not T.ACCEPTED or nxt.released_with is not None or \
                    task_id in self._running.get(conv, set()) or \
                    nxt.assignee != rec.assignee:
                break
            if not all(d in prefix or records[d].state is T.VERIFIED
                       for d in nxt.task.depends_on):
                break
            try:
                lease = self.resources.acquire(self._resources_for(nxt), principal)
            except ResourceConflict:
                break
            self._keep(lease)
            nxt.released_with = rec.task_id
            out.append((nxt, lease))
            prefix.add(task_id)
        return out

    def _unrelease(self, ahead: list[tuple[TaskRecord, Lease]]) -> None:
        for r, lease in ahead:
            r.released_with = None
            for rid in lease.reservation_ids:
                if self.resources.shares(rid, lease.loan):
                    self.resources.release(rid, token=lease.token)
                with contextlib.suppress(ValueError):
                    self._leases.get(rid, []).remove(lease)

    def _rejected(self, conv: str, steps: list[TaskRecord], msg: CoordinationMessage,
                  records: dict[str, TaskRecord]) -> None:
        """The robot rejected the update and kept its current work: nothing released
        by this update was executed."""
        reason = str(msg.payload.get("reason"))
        self.trace.record("order_rejected", f"{conv}/{steps[0].task_id}", reason=reason,
                          update_id=msg.update_id)
        for s in steps:
            if reason == "order_cancelled" or s.state is T.CANCELLING:
                self._stop(conv, s, f"order_rejected:{reason}")
            else:
                self._fail(conv, s, f"order_rejected:{reason}")
        self._release_eligible(records)

    async def _settle(self, conv: str, rec: TaskRecord, msg: CoordinationMessage,
                      records: dict[str, TaskRecord]) -> None:
        """Apply a reported result; if the robot itself could not determine the outcome,
        ask it to reconcile by observation (never by re-dispatching)."""
        if msg.kind is MessageKind.NOT_EXECUTED:
            self._not_executed(conv, rec, msg, records)
            return
        self._apply_result(conv, rec, msg, records)
        if rec.state is T.UNRESOLVED and rec.reason not in ("stop_unconfirmed",):
            await self._reconcile(conv, rec, records, reason=rec.reason or "unresolved")

    def _not_executed(self, conv: str, rec: TaskRecord, msg: CoordinationMessage,
                      records: dict[str, TaskRecord]) -> None:
        reason = str(msg.payload.get("reason", "not_executed"))
        self._observe_occupancy(msg.payload.get("occupancy", {}), msg.sender)
        if reason == "cancelled" or rec.state is T.CANCELLING:
            self._stop(conv, rec, "not_started")
        else:
            self._fail(conv, rec, f"not_executed:{reason}")
        self._release_eligible(records)

    async def _reconcile(self, conv: str, rec: TaskRecord,
                         records: dict[str, TaskRecord] | None = None,
                         reason: str = "unresolved") -> None:
        records = records if records is not None else self._records.get(conv, {})
        participant = rec.assignee
        assert participant is not None and rec.command_id is not None
        for _ in range(self.timeouts.query_retries + 1):
            if self._down(participant).is_set():
                reason = "connection_broken"
                break
            await self._send(conv, rec, rec.revision, [participant], MessageKind.QUERY,
                             command_id=rec.command_id, reconcile=True)
            got = (await self._collect(conv, rec.task_id, rec.revision, {participant},
                                       {MessageKind.STATUS, MessageKind.RESULT,
                                        MessageKind.NOT_EXECUTED},
                                       self.timeouts.query_s,
                                       [self._down(participant)])).get(participant)
            if got is None:
                continue
            self._observe_occupancy(got.payload.get("occupancy", {}), got.sender)
            if got.kind is MessageKind.NOT_EXECUTED:
                self._not_executed(conv, rec, got, records)
                return
            if got.kind is MessageKind.RESULT:
                self._apply_result(conv, rec, got, records)
                return
            if got.payload.get("known") is False:
                why = str(got.payload.get("reason", "not_received"))
                if why == "not_received":
                    # The participant never received the commitment: nothing executed.
                    if rec.state is not T.UNRESOLVED:
                        self._move(conv, rec, T.UNRESOLVED, "commitment never received")
                    self._move(conv, rec, T.FAILED, "not executed")
                    rec.reason = "commitment_not_received"
                elif why.endswith("cancelled"):
                    self._stop(conv, rec, "not_started")
                else:
                    self._fail(conv, rec, why)
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
        self._unresolved(conv, rec, reason)

    def _unresolved(self, conv: str, rec: TaskRecord, reason: str) -> None:
        if rec.state is not T.UNRESOLVED:
            self._move(conv, rec, T.UNRESOLVED, reason)
        rec.reason = reason
        self.ledger.append("coordination.unresolved", f"{conv}/{rec.task_id}",
                           {"reason": reason, "assignee": rec.assignee,
                            "held": [r.resource for r in rec.reservations]})

    def _apply_result(self, conv: str, rec: TaskRecord, msg: CoordinationMessage,
                      records: dict[str, TaskRecord]) -> None:
        result = ExecutionResult.from_dict(msg.payload["result"])
        rec.result = result
        self._observe_occupancy(msg.payload.get("occupancy", {}), msg.sender)
        s = result.state
        if s is CommandState.CANCELLED and rec.state in (*_IN_FLIGHT, T.CANCELLING,
                                                         T.UNRESOLVED):
            effects = result.observed_effects
            confirmed = effects.get("stopped_confirmed") is True or \
                effects.get("started") is False
            if confirmed:
                if rec.state in _IN_FLIGHT:   # stopped by the robot side, not by us
                    self._move(conv, rec, T.COMPLETED, "reported cancelled")
                self._stop(conv, rec, result.reason or "cancelled", confirmed=True)
                self._release_eligible(records)
            else:
                # Never a success, and not a confirmed stop either: holds are kept.
                self._unresolved(conv, rec, "stop_unconfirmed")
            return
        if rec.state in (*_IN_FLIGHT, T.CANCELLING):
            self._move(conv, rec, T.COMPLETED, f"reported {s.value}")
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
        else:
            self._fail(conv, rec, result.reason or s.value)
        self._release_eligible(records)

    def _fail(self, conv: str, rec: TaskRecord, reason: str, **details: Any) -> None:
        self._move(conv, rec, T.FAILED, reason)
        rec.reason = reason
        self.ledger.append("coordination.failed", f"{conv}/{rec.task_id}",
                           {"reason": reason, "assignee": rec.assignee, **details})

    def _stop(self, conv: str, rec: TaskRecord, reason: str, *,
              confirmed: bool = True) -> None:
        mode = rec.cancel_mode or self._cancel.get(conv, CancelMode.CANCEL)
        state = T.KILLED if mode is CancelMode.KILL else T.CANCELLED
        self._move(conv, rec, state, reason)
        rec.reason = reason
        self.ledger.append(f"coordination.{state.value}", f"{conv}/{rec.task_id}",
                           {"reason": reason, "assignee": rec.assignee, "mode": mode.value,
                            "stop_confirmed": confirmed})

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

    # -- cancellation and instant actions -------------------------------------------------
    def _abort(self, conv: str, task_id: str) -> asyncio.Event:
        return self._aborts.setdefault((conv, task_id), asyncio.Event())

    async def cancel(self, mission_id: str, *, mode: CancelMode | str = CancelMode.CANCEL,
                     reason: str = "cancel_requested") -> CancelReport:
        """Cancel (or kill) a mission. Not-started tasks stop at once; in-flight tasks go
        to CANCELLING and end CANCELLED/KILLED only when the robot confirms the stop. The
        cancel gets its own response per command; it never reports success."""
        mode = CancelMode(mode)
        self._cancel[mission_id] = mode
        self.ledger.append("coordination.cancel_requested", mission_id,
                           {"mode": mode.value, "reason": reason})
        self.trace.record("mission_cancel_requested", mission_id, mode=mode.value,
                          reason=reason)
        records = self._records.get(mission_id, {})
        if mission_id not in self._driving:
            for rec in records.values():
                if rec.state in _NOT_STARTED:
                    self._stop(mission_id, rec, "mission_cancelled")
        groups: dict[str, list[TaskRecord]] = {}
        for rec in records.values():
            if rec.state in _IN_FLIGHT and rec.command_id and rec.assignee:
                groups.setdefault(rec.assignee, []).append(rec)
        responses: dict[str, dict[str, Any]] = {}
        for participant, recs in sorted(groups.items()):
            for rec in recs:
                rec.cancel_mode = mode
                self._move(mission_id, rec, T.CANCELLING, f"{mode.value} requested")
            order = self._orders.get((mission_id, participant))
            resp = None
            if not self._down(participant).is_set():
                resp = await self._request(
                    participant, mission_id, MessageKind.CANCEL, self.timeouts.cancel_s,
                    order_id=order.order_id if order else f"{mission_id}:{participant}",
                    command_ids=[r.command_id for r in recs], mode=mode.value,
                    reason=reason)
            if resp is None:
                why = "connection_broken" if self._down(participant).is_set() \
                    else "no_response"
                responses[participant] = {r.command_id or "": {"accepted": False,
                                                               "reason": why}
                                          for r in recs}
                if mode is CancelMode.KILL:
                    for rec in recs:
                        self._abort(mission_id, rec.task_id).set()
                continue
            results = dict(resp.payload.get("results", {}))
            responses[participant] = results
            for rec in recs:
                r = dict(results.get(rec.command_id or "", {}))
                self.ledger.append("coordination.cancel_response",
                                   f"{mission_id}/{rec.task_id}",
                                   {"command_id": rec.command_id, "mode": mode.value,
                                    "accepted": bool(r.get("accepted")),
                                    "reason": r.get("reason")})
                if r.get("accepted") or rec.state is not T.CANCELLING:
                    continue
                if r.get("reason") == "not_interruptible":
                    # Uncancellable work keeps executing; it is never called cancelled.
                    self._move(mission_id, rec, T.EXECUTING, "cancel rejected: "
                                                              "not interruptible")
                    if mode is CancelMode.KILL:
                        self._abort(mission_id, rec.task_id).set()
        return CancelReport(mission_id, mode, responses,
                            {t: r.state.value for t, r in records.items()})

    async def instant(self, participant: str, action: InstantAction | str,
                      **params: Any) -> dict[str, Any] | None:
        """Send an instant action (valid at any revision); returns the response payload,
        or ``None`` if none arrived in time."""
        action = InstantAction(action)
        self.ledger.append("coordination.instant_action", participant,
                           {"action": action.value, "params": params})
        resp = await self._request(participant, "instant", MessageKind.INSTANT,
                                   self.timeouts.instant_s, action=action.value, **params)
        self.trace.record("instant_action_sent", participant, action=action.value,
                          answered=resp is not None)
        return None if resp is None else dict(resp.payload)

    async def pause(self, participant: str) -> dict[str, Any] | None:
        return await self.instant(participant, InstantAction.PAUSE)

    async def resume_participant(self, participant: str) -> dict[str, Any] | None:
        return await self.instant(participant, InstantAction.RESUME)

    async def state_request(self, participant: str) -> dict[str, Any] | None:
        resp = await self.instant(participant, InstantAction.STATE_REQUEST)
        return None if resp is None else dict(resp.get("state", {}))

    # -- resources -------------------------------------------------------------------------
    async def _acquire(self, resources: list[str], holder: str, conv: str) -> Lease:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeouts.reservation_wait_s
        while True:
            try:
                lease = self.resources.acquire(resources, holder)
            except ResourceConflict as exc:
                if conv in self._cancel:
                    raise _Cancelled from exc
                if loop.time() >= deadline:
                    raise ResourceConflict("timed out waiting for resources",
                                           resources=resources, **exc.details) from exc
                await asyncio.sleep(0.01)
                continue
            self._keep(lease)
            return lease

    def _keep(self, lease: Lease) -> None:
        for rid in lease.reservation_ids:
            self._leases.setdefault(rid, []).append(lease)

    def _free(self, res: ResourceReservation) -> None:
        leases = self._leases.pop(res.reservation_id, [])
        if not leases:
            self.resources.release(res.reservation_id)   # unscoped
            return
        for lease in leases:
            if self.resources.shares(res.reservation_id, lease.loan):
                self.resources.release(res.reservation_id, token=lease.token)

    def _observe_occupancy(self, occupancy: dict[str, Any], reporter: str) -> None:
        level = self.trust_of(reporter)
        for resource, occupant in occupancy.items():
            occupant = str(occupant or "")
            if not level.reports_applied and not occupant:
                # A robot we only notice can make occupancy more conservative, never free.
                self.trace.record("occupancy_report_not_applied", resource,
                                  reporter=reporter, trust=level.value)
                continue
            self.occupants[resource] = occupant
            self.resources.set_occupancy(resource,
                                         Occupancy.OCCUPIED if occupant else Occupancy.FREE)

    def _release_eligible(self, records: dict[str, TaskRecord]) -> None:
        """Release a participant's reservation only when no unfinished task of theirs
        needs it, no unresolved task of theirs holds it, and it is not physically occupied
        by them (logical release never implies physical vacancy). Holds of a participant
        whose connection is not ONLINE, or which no longer receives work (e.g. manual
        takeover), are kept."""
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
            if self.connection(endpoint) is not ConnectionState.ONLINE or \
                    not self.trust_of(endpoint).receives_work:
                continue
            for res in self.resources.held_by(principal):
                if any(needs(rec, endpoint, res.resource) for rec in records.values()):
                    continue
                if self.occupants.get(res.resource) == endpoint:
                    continue
                self._free(res)
                self.ledger.append("coordination.released", res.resource,
                                   {"holder": principal, "fencing_token": res.fencing_token,
                                    "loan": res.loan,
                                    "occupancy": self.occupants.get(res.resource, "unknown")})

    # -- bookkeeping -----------------------------------------------------------------------
    def _move(self, conv: str, rec: TaskRecord, new: TaskState, note: str = "") -> None:
        check_task_transition(rec.state, new)
        rec.state = new
        rec.history.append((self.clock.now(), new.value, rec.revision, note))
        self.trace.record(f"team_task_{new.value}", f"{conv}/{rec.task_id}",
                          revision=rec.revision, assignee=rec.assignee, note=note)
