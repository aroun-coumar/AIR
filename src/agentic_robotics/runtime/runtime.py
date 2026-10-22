"""The local runtime: admission, execution ownership, interruption and verification.

Flow for one command (baseline sections 5 and 6)::

    submit -> admission checks -> [AWAITING_APPROVAL ->] ACCEPTED -> dispatch checks
           -> journal "dispatch" -> EXECUTING [<-> PAUSED] -> adapter reports
           -> REPORTED_COMPLETE -> authoritative postconditions observed -> VERIFIED

Anything that leaves the physical outcome unknown (deadline exceeded after start,
adapter lost contact, inconclusive or non-authoritative evidence, a restart while a
command was dispatched) ends in UNRESOLVED, which only :meth:`Runtime.reconcile` can
settle. The runtime never resends a physical action by itself and never makes a model
call. ADR 0003 and ADR 0012 record the semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts.authority import ApprovalDecision, ApprovalOutcome, ApprovalRequest
from ..contracts.execution import (
    CancelCode,
    CancelResponse,
    Command,
    CommandState,
    ConditionResult,
    Evidence,
    EvidencePhase,
    ExecutionResult,
    ExitMode,
    FailureKind,
    QueuePolicy,
    RetryDecision,
    failure_kind_for,
)
from ..contracts.observation import Observation
from ..contracts.skill import Blocking, Interruption, SkillSpec
from ..core.clock import Clock, MonotonicClock
from ..core.errors import (
    AdmissionRejected,
    AuthorityError,
    OutcomeNotVerified,
    UnsupportedOperation,
)
from ..core.ids import new_id
from ..plugins import RESERVED_CLAIMS, AdapterReport, ExecutionContext, InterruptReport
from ..provenance import tracing
from ..robot import Robot
from ..skills import SkillRegistry
from .admission import (
    DEFAULT_CHECKS,
    DISPATCH_CHECKS,
    AdmissionCheck,
    AdmissionContext,
    apply_motion_limits,
)
from .authority import AuthorityService
from .holds import Hold, HoldRegistry
from .journal import CommandJournal
from .resources import ResourceManager
from .state import check_transition
from .trace import Trace

__requirements__ = ("REQ05", "REQ07", "REQ12", "REQ14", "REQ17")

S = CommandState
_RUNNING = (S.EXECUTING, S.PAUSED, S.CANCELLING)

#: Trace kinds added by ADR 0012 that change or settle a command. Pass
#: ``DEFAULT_RUNTIME_KINDS | COMMITMENT_TRACE_KINDS`` to ``Project.runtime_sink`` to
#: copy them into a project ledger.
COMMITMENT_TRACE_KINDS = frozenset({
    "approval_requested", "approval_decided", "awaiting_approval", "killed", "skipped",
    "approximate", "paused", "resumed", "keepalive_lapsed", "kill_requested",
    "halt_reported", "recovered", "retry_decided", "judgment_recorded",
    "judgment_disputes_verdict", "adapter_claim_ignored", "command_limited",
    "hold_acquired", "hold_released", "hold_release_refused", "keepalive_refused",
})


@dataclass(frozen=True)
class CommandStatus:
    """Instant ``state_request`` answer: what the runtime knows right now."""

    command_id: str
    state: CommandState
    started: bool
    cancel_requested: bool
    halt_reason: str | None
    holds: tuple[str, ...]
    applied_args: dict[str, Any] | None
    limited_by: tuple[str, ...]
    last_progress: dict[str, Any] | None
    keepalive_age_s: float | None
    approval_request: ApprovalRequest | None
    done: bool
    extra: dict[str, Any] = field(default_factory=dict)


class CommandHandle:
    """Tracks one submitted command. Await :meth:`wait` or
    :meth:`wait_for_verified_result`; call :meth:`cancel` to request interruption."""

    def __init__(self, command: Command, runtime: Runtime) -> None:
        self.command = command
        self.state = S.SUBMITTED
        self.result: ExecutionResult | None = None
        self.rejection: AdmissionRejected | None = None
        self.started_at: float | None = None
        self.reported_at: float | None = None
        self.cancel_requested = False
        self.queued = False
        self.inflight = False
        self.trace_id: str | None = None
        self.interrupt_report: InterruptReport | None = None
        self.skill: SkillSpec | None = None
        self.token = new_id("inv")
        self.holds: tuple[Hold, ...] = ()
        self.applied_args: dict[str, Any] | None = None
        self.limited_by: tuple[str, ...] = ()
        self.halt_kind: str | None = None      # "cancel" | "kill" | "deadline"
        self.halt_reason: str | None = None
        self.halted_notified = False
        self.last_keepalive: float | None = None
        self.last_progress: dict[str, Any] | None = None
        self.approval_request: ApprovalRequest | None = None
        self.approval: ApprovalDecision | None = None
        self.full_readmission = False
        self.retry_decision: RetryDecision | None = None
        self.judgments: list[Evidence] = []
        self.recovered = False
        self._runtime = runtime
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._exec_task: asyncio.Task[AdapterReport] | None = None
        self._timer: asyncio.Task[None] | None = None

    @property
    def command_id(self) -> str:
        return self.command.command_id

    @property
    def done(self) -> bool:
        return self._done.is_set()

    async def wait(self, timeout: float | None = None) -> ExecutionResult:
        await asyncio.wait_for(self._done.wait(), timeout)
        assert self.result is not None
        return self.result

    async def wait_for_verified_result(self, timeout: float | None = None) -> ExecutionResult:
        result = await self.wait(timeout)
        if not result.verified:
            raise OutcomeNotVerified("command did not reach a verified outcome", result,
                                     command_id=self.command_id, state=result.state.value,
                                     reason=result.reason)
        return result

    async def cancel(self, reason: str = "cancel_requested") -> ExecutionResult:
        return await self._runtime.cancel(self.command_id, reason)


class Runtime:
    def __init__(
        self,
        skills: SkillRegistry,
        *,
        clock: Clock | None = None,
        root_principals: Iterable[str] = ("operator:root",),
        authority: AuthorityService | None = None,
        resources: ResourceManager | None = None,
        checks: Sequence[AdmissionCheck] = DEFAULT_CHECKS,
        trace: Trace | None = None,
        debug: bool = False,
        record_shared_events: bool = True,
        journal: CommandJournal | None = None,
        unattended: bool = False,
        kill_grace_s: float = 1.0,
        watchdog_poll_s: float = 0.005,
    ) -> None:
        """``record_shared_events``: trace authority and reservation changes. When several
        robot-local runtimes share one authority domain and trace, enable it on one.

        ``journal``: a :class:`CommandJournal`; intents are written before dispatch and
        recovered on the next start. ``unattended``: nobody can approve, so every
        approval request is denied at once. ``kill_grace_s``: how long a forced stop
        waits for the adapter to return before the outcome becomes UNRESOLVED."""
        self.clock = clock or MonotonicClock()
        self.skills = skills
        self.authority = authority or AuthorityService(self.clock, root_principals)
        self.resources = resources or ResourceManager(self.clock)
        self.holds = HoldRegistry()
        self.checks = tuple(checks)
        self.trace = trace if trace is not None else Trace(self.clock)
        self.debug = debug
        self.journal = journal
        self.unattended = unattended
        self.kill_grace_s = kill_grace_s
        self.watchdog_poll_s = watchdog_poll_s
        if record_shared_events:
            self._trace_shared_events()
        self._robots: dict[str, Robot] = {}
        self._handles: dict[str, CommandHandle] = {}
        self._running: dict[str, dict[str, CommandHandle]] = {}
        self._waiting: dict[str, list[CommandHandle]] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self._inflight: dict[str, int] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._recovered = False
        self._to_reconcile: list[str] = []
        self.authority.on_revocation(self._on_revocation)

    def _trace_shared_events(self) -> None:
        self.authority.on_grant(lambda d: self.trace.record(
            "authority_granted", d.delegation_id, grantor=d.grantor, grantee=d.grantee,
            robots=d.robots, skills=d.skills, parent_id=d.parent_id,
            revision=self.authority.revision))
        self.authority.on_revocation(lambda ids: self.trace.record(
            "authority_revoked", ",".join(sorted(ids)), delegations=sorted(ids),
            revision=self.authority.revision))
        self.resources.on_change(lambda r: self.trace.record(
            f"reservation_{r.state.value}", r.resource, reservation_id=r.reservation_id,
            holder=r.holder, fencing_token=r.fencing_token,
            physical_occupancy=r.physical_occupancy.value))

    # -- robots and lifecycle --------------------------------------------------------
    def register(self, robot: Robot) -> Robot:
        if robot.robot_id in self._robots:
            raise UnsupportedOperation("robot already registered", robot_id=robot.robot_id)
        robot.adapter.bind_clock(self.clock)
        self._robots[robot.robot_id] = robot
        self.trace.record("robot_registered", robot.robot_id, adapter=robot.adapter.kind,
                          hardware_revision=robot.manifest.hardware_revision)
        return robot

    def robot(self, robot_id: str) -> Robot:
        try:
            return self._robots[robot_id]
        except KeyError:
            raise AdmissionRejected("unknown_robot", "robot is not registered",
                                    robot_id=robot_id) from None

    @property
    def robots(self) -> list[Robot]:
        return list(self._robots.values())

    async def start(self) -> None:
        self._recover()
        for r in self._robots.values():
            await r.adapter.start()
        self._reconcile_recovered()

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        for h in list(self._handles.values()):
            if h._timer and not h._timer.done():
                h._timer.cancel()
            if h._task and not h._task.done():
                h._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await h._task
        for r in self._robots.values():
            await r.adapter.stop()

    async def __aenter__(self) -> Runtime:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- durable recovery (W2.1) -----------------------------------------------------
    def _recover(self) -> None:
        """Rebuild handles from the journal. Settled outcomes are kept (deduplication
        survives); a dispatched intent without an outcome becomes UNRESOLVED; an intent
        that was never dispatched is CANCELLED. Nothing is resent."""
        if self._recovered or self.journal is None:
            self._recovered = True
            return
        self._recovered = True
        for entry in self.journal:
            cmd = entry.command
            if cmd.command_id in self._handles:
                continue
            h = CommandHandle(cmd, self)
            h.recovered = True
            h.applied_args = entry.applied_args
            self._handles[cmd.command_id] = h
            skill = self.skills.get(cmd.skill_id) if cmd.skill_id in self.skills else None
            h.skill = skill
            if entry.result is not None and entry.result.state is not S.UNRESOLVED:
                h.state, h.result = entry.result.state, entry.result
                h._done.set()
                self.trace.record("recovered", cmd.command_id, state=h.state.value)
                continue
            if entry.result is not None:  # already UNRESOLVED before the restart
                h.state, h.result = S.UNRESOLVED, entry.result
                h._done.set()
            elif entry.dispatched:
                h.state = S.UNRESOLVED
                self._finish(h, S.UNRESOLVED, reason="runtime_restarted",
                             message="dispatched before a restart with no recorded outcome; "
                                     "physical outcome unknown")
            else:
                h.state = S.CANCELLED
                self._finish(h, S.CANCELLED, reason="not_dispatched_before_restart",
                             effects={"started": False})
                self.trace.record("recovered", cmd.command_id, state=h.state.value)
                continue
            if skill is not None and skill.occupies:
                # The subsystem may still be moving: hold it until reconciled.
                h.holds = self.holds.acquire(cmd.robot_id, skill.occupies, h.token,
                                             cmd.command_id, self.clock.now())
            self.trace.record("recovered", cmd.command_id, state=h.state.value,
                              dispatched=entry.dispatched)
            self._to_reconcile.append(cmd.command_id)

    def _reconcile_recovered(self) -> None:
        pending, self._to_reconcile = self._to_reconcile, []
        for cid in pending:
            h = self._handles[cid]
            if h.state is not S.UNRESOLVED or h.command.robot_id not in self._robots \
                    or h.skill is None:
                continue
            self.reconcile(cid)

    # -- submission ----------------------------------------------------------------
    def handle(self, command_id: str) -> CommandHandle:
        return self._handles[command_id]

    def find(self, command_id: str) -> CommandHandle | None:
        return self._handles.get(command_id)

    def busy(self, robot_id: str) -> bool:
        return self._inflight.get(robot_id, 0) > 0

    def submit(self, command: Command) -> CommandHandle:
        """Submit a command. Each command gets its own span; a ``command.trace_id``
        received from another participant joins that trace."""
        with tracing.span(f"command:{command.skill_id}", trace_id=command.trace_id):
            return self._submit(command)

    def _submit(self, command: Command) -> CommandHandle:
        self._recover()
        existing = self._handles.get(command.command_id)
        if existing is not None:
            if existing.command.same_request(command):
                self.trace.record("duplicate_ignored", command.command_id,
                                  recovered=existing.recovered)
                return existing
            raise AdmissionRejected("command_id_reused",
                                    "command id was already used for a different request",
                                    command_id=command.command_id)

        handle = CommandHandle(command, self)
        ctx = tracing.current()
        handle.trace_id = ctx.trace_id if ctx else None
        self._handles[command.command_id] = handle
        self.trace.record("submitted", command.command_id, robot_id=command.robot_id,
                          skill_id=command.skill_id, issued_by=command.issued_by,
                          args=command.args, authority_revision=command.authority_revision,
                          retry_of=command.retry_of)
        try:
            skill = self._admit(command, self.checks)
            handle.skill = skill
            if skill.execution.queue_policy is QueuePolicy.REPLACE_LATEST:
                raise UnsupportedOperation("queue policy not implemented by this runtime",
                                           queue_policy=skill.execution.queue_policy.value)
            self._check_held(command, skill)
            if skill.requires_approval:
                if self.unattended:
                    raise AdmissionRejected("approval_unavailable_unattended",
                                            "skill requires approval and the runtime is "
                                            "unattended; denied", skill_id=skill.skill_id)
                self._await_approval(handle, skill)
                return handle
            self._check_busy(command, skill)
        except (AdmissionRejected, UnsupportedOperation) as exc:
            self._reject(handle, _as_rejection(exc))
            return handle
        self._accept(handle, skill)
        return handle

    def _accept(self, handle: CommandHandle, skill: SkillSpec) -> None:
        command = handle.command
        busy = self._conflicting(command.robot_id, skill, handle) is not None
        self._transition(handle, S.ACCEPTED)
        if self.journal is not None:
            self.journal.record_accepted(command)
        handle.queued = busy
        handle.inflight = True
        self._inflight[command.robot_id] = self._inflight.get(command.robot_id, 0) + 1
        self.trace.record("accepted", command.command_id, queued=busy)
        handle._task = asyncio.get_running_loop().create_task(self._run(handle, skill))

    def _check_busy(self, command: Command, skill: SkillSpec) -> None:
        other = self._conflicting(command.robot_id, skill, None)
        if other is not None and skill.execution.queue_policy is QueuePolicy.REJECT_IF_BUSY:
            raise AdmissionRejected("robot_busy", "robot is executing another command",
                                    robot_id=command.robot_id, blocking=skill.blocking.value,
                                    conflicting=other.command_id)

    def _check_held(self, command: Command, skill: SkillSpec) -> None:
        """A subsystem held by a command that is no longer in flight (an UNRESOLVED
        outcome) blocks new work until reconciliation; waiting would be forever."""
        for hold in self.holds.conflicts(command.robot_id, skill.occupies):
            holder = self._handles.get(hold.command_id)
            if holder is None or not holder.inflight:
                raise AdmissionRejected("subsystem_held_unresolved",
                                        "subsystem is held by a command whose outcome is "
                                        "unresolved; reconcile it first",
                                        subsystem=hold.subsystem, holder=hold.command_id)

    def _admit(self, command: Command, checks: Sequence[AdmissionCheck]) -> SkillSpec:
        robot = self.robot(command.robot_id)
        if command.skill_id not in self.skills:
            raise AdmissionRejected("unknown_skill", "skill is not registered",
                                    skill_id=command.skill_id)
        skill = self.skills.get(command.skill_id)
        ctx = AdmissionContext(
            command=command, skill=skill, robot=robot, clock=self.clock,
            authority=self.authority, resources=self.resources,
            capability=robot.capability_report(), observations=robot.observe(),
        )
        try:
            for check in checks:
                check(ctx)
        except AdmissionRejected:
            if self.debug:
                self._snapshot(ctx)
            raise
        return skill

    def _snapshot(self, ctx: AdmissionContext) -> None:
        """Debug mode: record exactly what admission saw when it rejected a command."""
        now = self.clock.now()
        self.trace.record(
            "admission_snapshot", ctx.command.command_id,
            clock=self.clock.status().sync_state.value,
            capability_revision=ctx.capability.capability_revision,
            capability_age_s=round(ctx.capability.age(now), 4),
            profiles={p.name: {"status": p.status.value,
                               "limiting_factors": list(p.limiting_factors)}
                      for p in ctx.capability.profiles},
            observations={k: {"value": o.value, "age_s": round(o.age(now), 4),
                              "quality": o.quality.value}
                          for k, o in ctx.observations.items()},
            authority_revision=self.authority.revision,
            max_input_age_s=ctx.skill.execution.max_input_age_s,
        )

    def _reject(self, handle: CommandHandle, rejection: AdmissionRejected) -> None:
        handle.rejection = rejection
        self.trace.record("rejected", handle.command_id, reason=rejection.reason,
                          message=rejection.message, details=rejection.details)
        self._finish(handle, S.REJECTED, reason=rejection.reason, message=rejection.message,
                     effects={"details": rejection.details})

    # -- scheduling: blocking and occupies ------------------------------------------
    @staticmethod
    def _conflicts(a: SkillSpec, b: SkillSpec) -> bool:
        if Blocking.HARD in (a.blocking, b.blocking):
            return True
        if a.blocking is Blocking.SOFT and b.blocking is Blocking.SOFT:
            return True
        return bool(set(a.occupies) & set(b.occupies))

    def _conflicting(self, robot_id: str, skill: SkillSpec,
                     me: CommandHandle | None) -> CommandHandle | None:
        """An in-flight command on ``robot_id`` that ``skill`` may not run beside."""
        for h in self._handles.values():
            if h is me or not h.inflight or h.command.robot_id != robot_id or h.skill is None:
                continue
            if self._conflicts(skill, h.skill):
                return h
        return None

    def _may_start(self, handle: CommandHandle) -> bool:
        rid = handle.command.robot_id
        assert handle.skill is not None
        for h in self._running.get(rid, {}).values():
            if h.skill is not None and self._conflicts(handle.skill, h.skill):
                return False
        for h in self._waiting.get(rid, []):
            if h is handle:
                break
            if h.skill is not None and self._conflicts(handle.skill, h.skill):
                return False  # first come, first served
        return True

    def _notify(self, robot_id: str) -> None:
        ev = self._changed.pop(robot_id, None)
        if ev is not None:
            ev.set()

    async def _wait_turn(self, handle: CommandHandle) -> None:
        rid = handle.command.robot_id
        waiting = self._waiting.setdefault(rid, [])
        waiting.append(handle)
        try:
            while not (handle.cancel_requested or self._may_start(handle)):
                ev = self._changed.setdefault(rid, asyncio.Event())
                await ev.wait()
        finally:
            waiting.remove(handle)
            self._notify(rid)

    # -- execution -----------------------------------------------------------------
    async def _run(self, handle: CommandHandle, skill: SkillSpec) -> None:
        cmd = handle.command
        rid = cmd.robot_id
        robot = self._robots[rid]
        await self._wait_turn(handle)
        running = self._running.setdefault(rid, {})
        running[cmd.command_id] = handle
        try:
            await self._dispatch(handle, skill, robot)
        finally:
            running.pop(cmd.command_id, None)
            self._notify(rid)

    async def _dispatch(self, handle: CommandHandle, skill: SkillSpec, robot: Robot) -> None:
        cmd = handle.command
        if handle.cancel_requested:
            self.trace.record("cancelled_before_start", cmd.command_id)
            self._finish(handle, S.CANCELLED, reason="cancelled_before_start",
                         effects={"started": False})
            return
        # Queued and approved commands are re-admitted in full; others re-check
        # authority and fencing to close the window between admission and dispatch.
        full = handle.queued or handle.full_readmission
        try:
            self._admit(cmd, self.checks if full else DISPATCH_CHECKS)
            self._check_held(cmd, skill)
            observations = robot.observe()
            now = self.clock.now()
            if skill.skip_if and self._applies(skill, cmd, observations, now):
                self.trace.record("skipped", cmd.command_id,
                                  conditions=[c.label(cmd.args) for c in skill.skip_if])
                self._finish(handle, S.SKIPPED, reason="not_applicable",
                             message="skip_if held: the skill does not apply; nothing moved",
                             effects={"started": False})
                return
            applied, limited = apply_motion_limits(skill, cmd.args, observations, now)
            if skill.occupies:
                handle.holds = self.holds.acquire(cmd.robot_id, skill.occupies, handle.token,
                                                  cmd.command_id, now)
                self.trace.record("hold_acquired", cmd.command_id,
                                  subsystems=list(skill.occupies), token=handle.token)
        except AdmissionRejected as exc:
            self._release_holds(handle)
            self._reject(handle, exc)
            return

        handle.applied_args, handle.limited_by = applied, limited
        if limited:
            self.trace.record("command_limited", cmd.command_id, requested=cmd.args,
                              applied=applied, limited_by=list(limited))
        # The intent is durable before anything can move.
        if self.journal is not None:
            self.journal.record_dispatch(cmd.command_id, applied)
        self._transition(handle, S.EXECUTING)
        handle.started_at = handle.last_keepalive = self.clock.now()
        self.trace.record("started", cmd.command_id)

        def progress(phase: str, data: Mapping[str, Any]) -> None:
            handle.last_progress = {"phase": phase, **dict(data)}
            self.trace.record("progress", cmd.command_id, phase=phase, **dict(data))

        dispatched = cmd.evolve(args=applied) if limited else cmd
        ctx = ExecutionContext(command=dispatched, skill=skill, clock=self.clock,
                               progress=progress,
                               authority_valid=lambda: self._authority_ok(cmd),
                               requested_args=dict(cmd.args), limited_by=limited)
        watchdog = None
        if skill.execution.continuous:
            watchdog = self._spawn(self._watchdog(handle, skill))
        exec_task = asyncio.get_running_loop().create_task(robot.adapter.execute(ctx))
        handle._exec_task = exec_task
        try:
            report = await asyncio.wait_for(exec_task, skill.execution.deadline_s)
        except TimeoutError:
            self.trace.record("deadline_exceeded", cmd.command_id,
                              deadline_s=skill.execution.deadline_s)
            handle.halt_kind = handle.halt_kind or "deadline"
            await self._interrupt_quietly(handle, skill, "deadline_exceeded")
            await self._notify_halted(handle, robot, "deadline_exceeded")
            self._finish(handle, S.UNRESOLVED, reason="deadline_exceeded",
                         message="execution started but did not report before its "
                                 "deadline; physical outcome unknown")
            return
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                self._finish(handle, S.UNRESOLVED, reason="runtime_stopped")
                raise
            # Only the adapter call was cancelled: a forced stop it did not honour.
            await self._notify_halted(handle, robot, handle.halt_reason or "killed")
            self._finish(handle, S.UNRESOLVED, reason="kill_unconfirmed",
                         message="forced stop requested but the adapter did not return; "
                                 "physical outcome unknown")
            return
        except Exception as exc:  # adapter lost contact after start
            self.trace.record("adapter_error", cmd.command_id, error=repr(exc))
            if handle.halt_kind:
                await self._notify_halted(handle, robot, handle.halt_reason or "halted")
            self._finish(handle, S.UNRESOLVED, reason="outcome_unknown",
                         message=f"adapter raised after start: {exc!r}")
            return
        finally:
            handle._exec_task = None
            if watchdog is not None:
                watchdog.cancel()
        if handle.halt_kind:
            await self._notify_halted(handle, robot, handle.halt_reason or handle.halt_kind)
        self._settle(handle, skill, report)

    def _applies(self, skill: SkillSpec, cmd: Command, observations: Mapping[str, Observation],
                 now: float) -> bool:
        """True when every ``skip_if`` condition holds on fresh authoritative evidence."""
        max_age = skill.execution.max_input_age_s
        for cond in skill.skip_if:
            obs = observations.get(cond.resolved_signal(cmd.args))
            if obs is None or obs.age(now) > max_age:
                return False
            if not cond.evaluate(observations, cmd.args, now,
                                 require_authoritative=True).satisfied:
                return False
        return True

    def _settle(self, handle: CommandHandle, skill: SkillSpec, report: AdapterReport) -> None:
        cmd = handle.command
        handle.reported_at = self.clock.now()
        effects = dict(report.effects)
        claims = sorted(k for k in effects if k in RESERVED_CLAIMS)
        if claims:
            # An adapter states facts; it cannot declare the outcome.
            self.trace.record("adapter_claim_ignored", cmd.command_id, keys=claims)
            for k in claims:
                effects.pop(k)
        limited = handle.limited_by
        if report.applied is not None and report.applied != (handle.applied_args or cmd.args):
            limited = (*limited, *(report.limited_by or ("adapter",)))
            effects["adapter_applied"] = report.applied
        handle.limited_by = limited
        self.trace.record("adapter_reported", cmd.command_id, completed=report.completed,
                          message=report.message, effects=effects,
                          failure=report.failure.value if report.failure else None,
                          adapter_applied=report.applied, limited_by=list(limited))
        if not report.completed:
            stopped = bool(handle.interrupt_report and handle.interrupt_report.stopped_confirmed)
            if handle.halt_kind == "kill":
                self._finish(handle, S.KILLED, reason=handle.halt_reason or "killed",
                             message="forced stop" if stopped else
                             "forced stop; stop is NOT physically confirmed",
                             effects={**effects, "stopped_confirmed": stopped,
                                      "exit_mode": effects.get("exit_mode",
                                                               skill.execution.exit_mode.value)})
            elif handle.state is S.CANCELLING:
                self._finish(handle, S.CANCELLED, reason="cancelled",
                             message="cancelled" if stopped else
                             "cancelled; stop is NOT physically confirmed",
                             effects={**effects, "stopped_confirmed": stopped})
            else:
                reason = report.failure_reason or "reported_failure"
                self._finish(handle, S.FAILED, reason=reason, message=report.message,
                             effects=effects,
                             failure=report.failure or failure_kind_for(reason),
                             retriable=report.retriable or reason in skill.retriable_failures)
            return

        self._transition(handle, S.REPORTED_COMPLETE)
        if handle.cancel_requested or handle.halt_kind:
            # Completed before the stop took effect. Report what happened, never "cancelled".
            self.trace.record("completed_despite_halt", cmd.command_id, halt=handle.halt_kind)
            effects["completed_despite_halt"] = handle.halt_kind or "cancel"
        if not skill.postconditions:
            self.trace.record("unverifiable", cmd.command_id)
            self._finish(handle, S.REPORTED_COMPLETE, reason="no_postconditions_declared",
                         message="adapter reported completion; skill declares nothing to verify",
                         effects=effects)
            return
        self._verify(handle, skill, effects)

    def _phase(self, handle: CommandHandle, obs: Observation, end: float | None) -> EvidencePhase:
        if handle.started_at is not None and obs.acquired_at < handle.started_at:
            return EvidencePhase.BEFORE
        if end is not None and obs.acquired_at < end:
            return EvidencePhase.DURING
        return EvidencePhase.AFTER

    def _verify(self, handle: CommandHandle, skill: SkillSpec, effects: dict[str, Any],
                reconciled: bool = False) -> None:
        cmd = handle.command
        args = handle.applied_args if handle.applied_args is not None else cmd.args
        robot = self._robots[cmd.robot_id]
        observations = robot.observe()
        now = self.clock.now()
        end = handle.reported_at
        if end is None and handle.result is not None and handle.started_at is not None:
            end = handle.result.finished_at  # when the runtime lost track of it
        results: list[ConditionResult] = []
        evidence: list[Evidence] = []
        inconclusive = False
        for cond in skill.postconditions:
            signal = cond.resolved_signal(args)
            obs = observations.get(signal)
            if obs is None:
                inconclusive = True
                results.append(ConditionResult(cond.label(args), False,
                                               detail="no observation for signal"))
                continue
            phase = self._phase(handle, obs, end)
            evidence.append(Evidence(
                signal=signal, source=obs.source, phase=phase,
                association_quality=obs.association_quality.value, origin=obs.origin.value,
                authoritative=obs.authoritative, acquired_at=obs.acquired_at,
                value=obs.value))
            if phase is not EvidencePhase.AFTER:
                inconclusive = True
                results.append(ConditionResult(
                    cond.label(args), False, observation_age_s=obs.age(now),
                    detail=f"no observation acquired after execution (phase={phase.value})"))
                continue
            r = cond.evaluate(observations, args, now, require_authoritative=True)
            if not r.satisfied and r.detail:
                inconclusive = True
            results.append(r)
        self.trace.record("verification", cmd.command_id, reconciled=reconciled,
                          results=[(r.description, r.satisfied) for r in results],
                          evidence=[(e.signal, e.phase.value, e.authoritative)
                                    for e in evidence])
        kw: dict[str, Any] = {"effects": effects, "verification": tuple(results),
                              "evidence": tuple(evidence)}
        if all(r.satisfied for r in results):
            if any(r.approximate for r in results):
                self._finish(handle, S.APPROXIMATE,
                             reason="reconciled_approximate" if reconciled
                             else "within_acceptance_band",
                             message="postconditions hold only within their acceptance band",
                             **kw)
            else:
                self._finish(handle, S.VERIFIED, reason="reconciled" if reconciled else None,
                             **kw)
        elif inconclusive:
            self._finish(handle, S.UNRESOLVED, reason="verification_inconclusive", **kw)
        else:
            self._finish(handle, S.FAILED, reason="postcondition_failed", **kw)

    # -- interruption, revocation, reconciliation -----------------------------------
    async def request_cancel(self, command_id: str,
                             reason: str = "cancel_requested") -> CancelResponse:
        """Ask a command to stop cooperatively and return the cancel's own answer at
        once. Await :meth:`CommandHandle.wait` for the command's outcome."""
        handle = self._handles.get(command_id)
        if handle is None:
            return CancelResponse(command_id=command_id, code=CancelCode.UNKNOWN_COMMAND)

        def answer(code: CancelCode, message: str = "",
                   stop: bool | None = None) -> CancelResponse:
            return CancelResponse(command_id=command_id, code=code, state=handle.state,
                                  started=handle.started_at is not None,
                                  stop_confirmed=stop, message=message)

        if handle.state is S.UNRESOLVED:
            return answer(CancelCode.REJECTED, "outcome is unresolved; reconcile it")
        if handle.done:
            return answer(CancelCode.ALREADY_TERMINAL)
        skill = handle.skill or self.skills.get(handle.command.skill_id)
        if handle.state is S.AWAITING_APPROVAL:
            self.trace.record("cancel_requested", command_id, reason=reason, started=False)
            self._end_approval(handle)
            self._finish(handle, S.CANCELLED, reason="cancelled_awaiting_approval",
                         effects={"started": False})
            return answer(CancelCode.ACCEPTED, "cancelled before approval", stop=True)
        if handle.state is S.ACCEPTED:
            handle.cancel_requested = True
            self.trace.record("cancel_requested", command_id, reason=reason, started=False)
            self._notify(handle.command.robot_id)
            return answer(CancelCode.ACCEPTED, "cancelled before start", stop=True)
        if handle.state is S.CANCELLING:
            r = handle.interrupt_report
            return answer(CancelCode.ACCEPTED, "already cancelling",
                          stop=r.stopped_confirmed if r else None)
        if handle.state in (S.EXECUTING, S.PAUSED):
            if skill.interruption is Interruption.NONE:
                return answer(CancelCode.REJECTED, "skill cannot be interrupted once started")
            handle.cancel_requested = True
            handle.halt_kind = handle.halt_kind or "cancel"
            handle.halt_reason = handle.halt_reason or reason
            self._transition(handle, S.CANCELLING)
            self.trace.record("cancel_requested", command_id, reason=reason, started=True)
            report = await self._robots[handle.command.robot_id].adapter.interrupt(command_id)
            handle.interrupt_report = report
            self.trace.record("interrupt_reported", command_id, accepted=report.accepted,
                              stopped_confirmed=report.stopped_confirmed)
            return answer(CancelCode.ACCEPTED, report.message, stop=report.stopped_confirmed)
        return answer(CancelCode.REJECTED, f"cannot cancel in state {handle.state.value}")

    async def cancel(self, command_id: str, reason: str = "cancel_requested") -> ExecutionResult:
        """Request cancellation and wait for the outcome. A command that completed before
        the stop took effect keeps its real outcome; it is never reported as cancelled,
        and a cancel is never reported as success."""
        handle = self._handles[command_id]
        if handle.done and handle.state is not S.UNRESOLVED:
            assert handle.result is not None
            return handle.result
        if handle.state is S.UNRESOLVED:
            return await handle.wait()
        response = await self.request_cancel(command_id, reason)
        if response.code is CancelCode.REJECTED:
            raise UnsupportedOperation("skill cannot be interrupted once started",
                                       skill_id=handle.command.skill_id,
                                       detail=response.message)
        return await handle.wait()

    async def kill(self, command_id: str, reason: str = "killed",
                   exit_mode: ExitMode | None = None) -> CancelResponse:
        """Forced stop: the adapter's :meth:`~RobotAdapter.halt` enters ``exit_mode``
        (default: the skill's), even for skills that cannot be cancelled. Ends KILLED;
        if the adapter does not return within ``kill_grace_s`` it ends UNRESOLVED."""
        handle = self._handles.get(command_id)
        if handle is None:
            return CancelResponse(command_id=command_id, code=CancelCode.UNKNOWN_COMMAND)
        if handle.done or handle.state is S.UNRESOLVED:
            return CancelResponse(command_id=command_id, code=CancelCode.ALREADY_TERMINAL,
                                  state=handle.state, started=handle.started_at is not None)
        if handle.state in (S.AWAITING_APPROVAL, S.ACCEPTED, S.SUBMITTED):
            resp = await self.request_cancel(command_id, reason)
            return resp
        skill = handle.skill or self.skills.get(handle.command.skill_id)
        mode = exit_mode or skill.execution.exit_mode
        handle.halt_kind = "kill"
        handle.halt_reason = reason
        self.trace.record("kill_requested", command_id, reason=reason, exit_mode=mode.value)
        robot = self._robots[handle.command.robot_id]
        try:
            report = await robot.adapter.halt(command_id, mode)
        except Exception as exc:
            self.trace.record("interrupt_failed", command_id, error=repr(exc))
            report = InterruptReport(False, False, f"halt raised: {exc!r}")
        handle.interrupt_report = report
        self.trace.record("halt_reported", command_id, accepted=report.accepted,
                          stopped_confirmed=report.stopped_confirmed, exit_mode=mode.value)
        task = handle._exec_task
        if task is not None and not task.done():
            self._spawn(self._kill_grace(task))
        return CancelResponse(command_id=command_id, code=CancelCode.ACCEPTED,
                              state=handle.state, started=True,
                              stop_confirmed=report.stopped_confirmed, message=report.message)

    async def _kill_grace(self, task: asyncio.Task[AdapterReport]) -> None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), self.kill_grace_s)
        if not task.done():
            task.cancel()

    async def _notify_halted(self, handle: CommandHandle, robot: Robot, reason: str) -> None:
        """Call the adapter's ``on_halted`` exactly once per halted command."""
        if handle.halted_notified:
            return
        handle.halted_notified = True
        try:
            await robot.adapter.on_halted(handle.command_id, reason)
            self.trace.record("on_halted", handle.command_id, reason=reason)
        except Exception as exc:
            self.trace.record("on_halted_failed", handle.command_id, error=repr(exc))

    async def _interrupt_quietly(self, handle: CommandHandle, skill: SkillSpec,
                                 reason: str) -> None:
        if skill.interruption is Interruption.NONE:
            return
        try:
            report = await self._robots[handle.command.robot_id].adapter.interrupt(
                handle.command_id)
            handle.interrupt_report = report
            self.trace.record("interrupt_reported", handle.command_id, reason=reason,
                              accepted=report.accepted,
                              stopped_confirmed=report.stopped_confirmed)
        except Exception as exc:
            self.trace.record("interrupt_failed", handle.command_id, error=repr(exc))

    # -- instant actions (W2.3) ------------------------------------------------------
    def state_request(self, command_id: str) -> CommandStatus:
        """Instant: what the runtime knows about a command right now."""
        h = self._handles.get(command_id)
        if h is None:
            raise UnsupportedOperation("unknown command", command_id=command_id)
        age = (self.clock.now() - h.last_keepalive
               if h.last_keepalive is not None and h.state in _RUNNING else None)
        return CommandStatus(
            command_id=command_id, state=h.state, started=h.started_at is not None,
            cancel_requested=h.cancel_requested, halt_reason=h.halt_reason,
            holds=tuple(x.subsystem for x in h.holds), applied_args=h.applied_args,
            limited_by=h.limited_by, last_progress=h.last_progress, keepalive_age_s=age,
            approval_request=h.approval_request, done=h.done)

    async def pause(self, command_id: str) -> CommandStatus:
        """Instant: pause a running command whose skill declares ``interruption: pause``."""
        h = self._running_handle(command_id, S.EXECUTING)
        skill = h.skill or self.skills.get(h.command.skill_id)
        if skill.interruption is not Interruption.PAUSE:
            raise UnsupportedOperation("skill does not declare pause support",
                                       skill_id=skill.skill_id)
        report = await self._robots[h.command.robot_id].adapter.pause(command_id)
        self.trace.record("pause_reported", command_id, accepted=report.accepted,
                          confirmed=report.stopped_confirmed)
        if report.accepted and h.state is S.EXECUTING:
            self._transition(h, S.PAUSED)
            self.trace.record("paused", command_id, confirmed=report.stopped_confirmed)
        return self.state_request(command_id)

    async def resume_execution(self, command_id: str) -> CommandStatus:
        """Instant: resume a paused command."""
        h = self._running_handle(command_id, S.PAUSED)
        report = await self._robots[h.command.robot_id].adapter.resume(command_id)
        self.trace.record("resume_reported", command_id, accepted=report.accepted)
        if report.accepted and h.state is S.PAUSED:
            self._transition(h, S.EXECUTING)
            h.last_keepalive = self.clock.now() if h.last_keepalive is not None else None
            self.trace.record("resumed", command_id)
        return self.state_request(command_id)

    def _running_handle(self, command_id: str, state: CommandState) -> CommandHandle:
        h = self._handles.get(command_id)
        if h is None:
            raise UnsupportedOperation("unknown command", command_id=command_id)
        if h.state is not state:
            raise UnsupportedOperation(f"command is not {state.value}", state=h.state.value)
        return h

    # -- intent deadman (W2.6) ---------------------------------------------------------
    def keepalive(self, command_id: str, issued_by: str) -> bool:
        """Refresh the intent of a continuous skill. Only the issuing principal, still
        holding authority, can keep a command alive. Returns whether it was accepted."""
        h = self._handles.get(command_id)
        if h is None:
            raise UnsupportedOperation("unknown command", command_id=command_id)
        skill = h.skill or self.skills.get(h.command.skill_id)
        if not skill.execution.continuous:
            raise UnsupportedOperation("skill does not take keepalives",
                                       skill_id=skill.skill_id)
        problem = None
        if h.state not in _RUNNING or h.halt_kind:
            problem = "not_running"
        elif issued_by != h.command.issued_by:
            problem = "wrong_principal"
        elif not self._authority_ok(h.command):
            problem = "authority"
        if problem:
            self.trace.record("keepalive_refused", command_id, reason=problem,
                              principal=issued_by)
            return False
        h.last_keepalive = self.clock.now()
        return True

    async def _watchdog(self, handle: CommandHandle, skill: SkillSpec) -> None:
        limit = skill.execution.max_input_age_s
        while handle.state in _RUNNING and not handle.done:
            await asyncio.sleep(self.watchdog_poll_s)
            if handle.halt_kind or handle.last_keepalive is None:
                return
            age = self.clock.now() - handle.last_keepalive
            if age > limit:
                self.trace.record("keepalive_lapsed", handle.command_id,
                                  age_s=round(age, 4), max_input_age_s=limit,
                                  exit_mode=skill.execution.exit_mode.value)
                await self.kill(handle.command_id, reason="keepalive_lapsed")
                return

    # -- human approval as an interrupt (W2.5) --------------------------------------
    def _await_approval(self, handle: CommandHandle, skill: SkillSpec) -> None:
        cmd = handle.command
        now = self.clock.now()
        req = ApprovalRequest(command_id=cmd.command_id, robot_id=cmd.robot_id,
                              skill_id=cmd.skill_id, issued_by=cmd.issued_by,
                              args=dict(cmd.args), args_digest=cmd.request_digest(),
                              authority_revision=self.authority.revision, requested_at=now,
                              expires_at=now + skill.approval_timeout_s,
                              reason=skill.description)
        handle.approval_request = req
        self._transition(handle, S.AWAITING_APPROVAL)
        if self.journal is not None:
            self.journal.record_awaiting_approval(cmd)
        self.trace.record("approval_requested", cmd.command_id, args_digest=req.args_digest,
                          expires_at=req.expires_at, skill_id=cmd.skill_id,
                          issued_by=cmd.issued_by)
        handle._timer = self._spawn(self._approval_timeout(handle, skill.approval_timeout_s))

    async def _approval_timeout(self, handle: CommandHandle, timeout_s: float) -> None:
        await asyncio.sleep(timeout_s)
        if handle.state is S.AWAITING_APPROVAL:
            self._deny_timeout(handle)

    def _deny_timeout(self, handle: CommandHandle) -> None:
        self.trace.record("approval_decided", handle.command_id, decision="reject",
                          approver=None, reason="approval_timeout")
        self._reject(handle, AdmissionRejected("approval_timeout", "nobody decided before the "
                                               "approval expired; denied by default"))

    def _end_approval(self, handle: CommandHandle) -> None:
        if handle._timer is not None and not handle._timer.done():
            handle._timer.cancel()
        handle._timer = None

    def pending_approvals(self) -> list[ApprovalRequest]:
        return [h.approval_request for h in self._handles.values()
                if h.state is S.AWAITING_APPROVAL and h.approval_request is not None]

    def expire_approvals(self) -> list[str]:
        """Deny every approval request whose ``expires_at`` has passed on the runtime
        clock (the timer does this in real time; call this with a manual clock)."""
        now = self.clock.now()
        expired = []
        for h in list(self._handles.values()):
            req = h.approval_request
            if h.state is S.AWAITING_APPROVAL and req is not None and req.expires_at is not None \
                    and now >= req.expires_at:
                self._end_approval(h)
                self._deny_timeout(h)
                expired.append(h.command_id)
        return expired

    def resume(self, command_id: str, decision: ApprovalOutcome | str, approver: str,
               authority_revision: int, *, args_digest: str, delegation_id: str | None = None,
               note: str = "") -> CommandHandle:
        """Decide a pending approval, bound by ``command_id`` and the exact
        ``args_digest`` of the request (never by position). Single use. The approver
        must be a root principal or hold an ``approve`` delegation for the robot and
        skill, must not be the requester, and must cite the current authority revision.
        An approved command is still re-admitted in full before it runs."""
        decision = ApprovalOutcome(decision)
        h = self._handles.get(command_id)
        if h is None:
            raise UnsupportedOperation("unknown command", command_id=command_id)
        req = h.approval_request
        if h.state is not S.AWAITING_APPROVAL or req is None:
            raise UnsupportedOperation("no pending approval for this command (approvals are "
                                       "single-use)", command_id=command_id,
                                       state=h.state.value)
        if req.expires_at is not None and self.clock.now() >= req.expires_at:
            self._end_approval(h)
            self._deny_timeout(h)
            return h
        cmd = h.command
        if args_digest != req.args_digest:
            raise AuthorityError("approval names different arguments than the request",
                                 reason="approval_digest_mismatch", expected=req.args_digest,
                                 actual=args_digest)
        if approver == cmd.issued_by:
            raise AuthorityError("a principal cannot approve its own command",
                                 reason="approver_is_requester", approver=approver)
        if authority_revision != self.authority.revision:
            raise AuthorityError("approval cites a stale authority revision",
                                 reason="stale_revision", cited=authority_revision,
                                 current=self.authority.revision)
        if approver not in self.authority.root_principals:
            if delegation_id is None:
                raise AuthorityError("approver holds no approve delegation",
                                     reason="out_of_scope", approver=approver)
            self.authority.check(delegation_id, approver, cmd.robot_id, cmd.skill_id,
                                 operation="approve")
        record = ApprovalDecision(command_id=command_id, decision=decision, approver=approver,
                                  authority_revision=authority_revision,
                                  args_digest=args_digest, decided_at=self.clock.now(),
                                  delegation_id=delegation_id, note=note)
        h.approval = record
        self._end_approval(h)
        self.trace.record("approval_decided", command_id, decision=decision.value,
                          approver=approver, args_digest=args_digest,
                          authority_revision=authority_revision, note=note)
        skill = h.skill or self.skills.get(cmd.skill_id)
        if decision is ApprovalOutcome.REJECT:
            self._reject(h, AdmissionRejected("approval_rejected", "approver rejected the "
                                              "command", approver=approver))
            return h
        try:
            self._check_busy(cmd, skill)
        except AdmissionRejected as exc:
            self._reject(h, exc)
            return h
        h.full_readmission = True
        self._accept(h, skill)
        return h

    # -- non-authoritative judgments (W2.2) ------------------------------------------
    def judge(self, command_id: str, verdict: str, source: str, *,
              confidence: float | None = None, rationale: str = "") -> ExecutionResult:
        """Record a non-authoritative judgment (e.g. a VLM looking at an image). It is
        evidence, never a verdict: it cannot make anything VERIFIED. A ``failure`` or
        ``inconclusive`` judgment on an unverifiable REPORTED_COMPLETE outcome moves it
        to UNRESOLVED; everything else is recorded only."""
        if verdict not in ("success", "failure", "inconclusive"):
            raise UnsupportedOperation("verdict must be success, failure or inconclusive",
                                       verdict=verdict)
        h = self._handles[command_id]
        if h.result is None:
            raise UnsupportedOperation("command has no outcome to judge yet",
                                       state=h.state.value)
        ev = Evidence(signal="judgment", source=source, phase=EvidencePhase.AFTER,
                      association_quality="best_effort", origin="model", authoritative=False,
                      acquired_at=self.clock.now(), value=confidence, verdict=verdict,
                      detail=rationale)
        h.judgments.append(ev)
        self.trace.record("judgment_recorded", command_id, source=source, verdict=verdict,
                          confidence=confidence, state=h.state.value)
        result = h.result
        if h.state is S.REPORTED_COMPLETE and verdict != "success":
            h.result = result.evolve(evidence=(*result.evidence, ev))
            self._finish(h, S.UNRESOLVED, reason="judgment_disputes_report",
                         message=f"{source} judged the reported completion {verdict}; "
                                 "physical outcome unknown",
                         effects=dict(result.observed_effects), evidence=h.result.evidence,
                         verification=result.verification)
        else:
            disputes = ((h.state is S.VERIFIED and verdict == "failure")
                        or (h.state is S.FAILED and verdict == "success"))
            if disputes:
                self.trace.record("judgment_disputes_verdict", command_id, source=source,
                                  verdict=verdict, state=h.state.value)
            h.result = result.evolve(evidence=(*result.evidence, ev))
        assert h.result is not None
        return h.result

    # -- explicit retry decisions (P1) ------------------------------------------------
    def decide(self, command_id: str, decision: RetryDecision | str,
               decided_by: str) -> CommandHandle | None:
        """Decide what happens after a RETRIABLE failure. ``retry`` submits a *new*
        command (``retry_of`` links it) that goes through full admission again; ``skip``
        records that the step is abandoned. The runtime never retries by itself, and an
        UNRESOLVED outcome cannot be retried (reconcile it first)."""
        decision = RetryDecision(decision)
        h = self._handles.get(command_id)
        if h is None:
            raise UnsupportedOperation("unknown command", command_id=command_id)
        result = h.result
        if result is None or result.state is not S.FAILED or not result.retriable:
            raise UnsupportedOperation("only a retriable FAILED outcome takes a retry decision",
                                       state=h.state.value)
        if h.retry_decision is not None:
            raise UnsupportedOperation("retry decision already made",
                                       decision=h.retry_decision.value)
        if decided_by != h.command.issued_by and decided_by not in self.authority.root_principals:
            raise AuthorityError("only the issuer or a root principal decides a retry",
                                 reason="wrong_principal", principal=decided_by)
        h.retry_decision = decision
        self.trace.record("retry_decided", command_id, decision=decision.value,
                          decided_by=decided_by)
        if decision is RetryDecision.SKIP:
            return None
        retry = h.command.evolve(command_id=new_id("cmd"), retry_of=command_id)
        return self.submit(retry)

    # -- revocation and reconciliation ------------------------------------------------
    def _on_revocation(self, revoked: frozenset[str]) -> None:
        for running in list(self._running.values()):
            for handle in list(running.values()):
                chain = {d.delegation_id
                         for d in self.authority.chain(handle.command.delegation_id)}
                if not chain & revoked:
                    continue
                skill = handle.skill or self.skills.get(handle.command.skill_id)
                self.trace.record("authority_revoked_during_execution", handle.command_id,
                                  interruptible=skill.interruption is not Interruption.NONE)
                if skill.interruption is not Interruption.NONE and \
                        handle.state in (S.EXECUTING, S.PAUSED):
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        self.trace.record("revocation_interrupt_deferred", handle.command_id)
                        continue
                    self._spawn(self.cancel(handle.command_id, reason="authority_revoked"))
        for h in list(self._handles.values()):
            if h.state is S.AWAITING_APPROVAL:
                chain = {d.delegation_id for d in self.authority.chain(h.command.delegation_id)}
                if chain & revoked:
                    self._end_approval(h)
                    self._reject(h, AdmissionRejected("authority_revoked", "authority was "
                                                      "revoked while awaiting approval"))

    def _authority_ok(self, cmd: Command) -> bool:
        try:
            self.authority.check(cmd.delegation_id, cmd.issued_by, cmd.robot_id, cmd.skill_id)
        except AuthorityError:
            return False
        return True

    def reconcile(self, command_id: str) -> ExecutionResult:
        """Settle an UNRESOLVED command by observing the world, not by re-executing."""
        handle = self._handles[command_id]
        if handle.state is not S.UNRESOLVED:
            raise UnsupportedOperation("only unresolved commands can be reconciled",
                                       state=handle.state.value)
        skill = self.skills.get(handle.command.skill_id)
        self.trace.record("reconcile_started", command_id)
        if not skill.postconditions:
            assert handle.result is not None
            return handle.result
        effects = handle.result.observed_effects if handle.result else {}
        self._verify(handle, skill, dict(effects), reconciled=True)
        assert handle.result is not None
        return handle.result

    # -- helpers -------------------------------------------------------------------
    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _transition(self, handle: CommandHandle, new: S) -> None:
        check_transition(handle.state, new)
        handle.state = new

    def _release_holds(self, handle: CommandHandle) -> None:
        if not handle.holds:
            return
        rid = handle.command.robot_id
        released = self.holds.release(rid, handle.token)
        handle.holds = ()
        self.trace.record("hold_released", handle.command_id,
                          subsystems=[h.subsystem for h in released])
        self._notify(rid)

    def _finish(self, handle: CommandHandle, state: S, *, reason: str | None = None,
                message: str = "", effects: dict[str, Any] | None = None,
                verification: tuple[ConditionResult, ...] = (),
                evidence: tuple[Evidence, ...] = (), failure: FailureKind | None = None,
                retriable: bool = False) -> None:
        if handle.state is not state:
            self._transition(handle, state)
        cmd = handle.command
        robot = self._robots.get(cmd.robot_id)
        cap_rev = None
        if robot is not None:
            with contextlib.suppress(Exception):
                cap_rev = robot.capability_report().capability_revision
        if failure is None and state not in (S.VERIFIED, S.SKIPPED, S.APPROXIMATE,
                                             S.REPORTED_COMPLETE, S.CANCELLED):
            failure = failure_kind_for(reason)
        evidence = (*evidence, *(e for e in handle.judgments if e not in evidence))
        handle.result = ExecutionResult(
            command_id=cmd.command_id, robot_id=cmd.robot_id, skill_id=cmd.skill_id,
            state=state, reason=reason, message=message, observed_effects=effects or {},
            verification=verification, authority_revision=self.authority.revision,
            capability_revision=cap_rev, started_at=handle.started_at,
            finished_at=self.clock.now(), failure=failure,
            retriable=retriable and state is S.FAILED,
            applied_args=handle.applied_args if handle.limited_by else None,
            limited_by=handle.limited_by, evidence=evidence, retry_of=cmd.retry_of,
        )
        self.trace.record(state.value, cmd.command_id, reason=reason, robot_id=cmd.robot_id,
                          skill_id=cmd.skill_id,
                          stopped_confirmed=(effects or {}).get("stopped_confirmed"),
                          failure=failure.value if failure else None)
        if self.journal is not None:
            self.journal.record_outcome(cmd, handle.result)
        if state is not S.UNRESOLVED:
            self._release_holds(handle)
        if handle.inflight:
            handle.inflight = False
            self._inflight[cmd.robot_id] -= 1
            self._notify(cmd.robot_id)
        handle._done.set()


def _as_rejection(exc: AdmissionRejected | UnsupportedOperation) -> AdmissionRejected:
    if isinstance(exc, AdmissionRejected):
        return exc
    return AdmissionRejected(exc.code, exc.message, **exc.details)


__all__ = ["COMMITMENT_TRACE_KINDS", "CommandHandle", "CommandStatus", "Runtime"]
