"""The local runtime: admission, execution ownership, interruption and verification.

Flow for one command (baseline sections 5 and 6)::

    submit -> admission checks -> ACCEPTED -> dispatch checks -> EXECUTING
           -> adapter reports -> REPORTED_COMPLETE -> postconditions observed -> VERIFIED

Anything that leaves the physical outcome unknown (deadline exceeded after start,
adapter lost contact, inconclusive observations) ends in UNRESOLVED, which only
:meth:`Runtime.reconcile` can settle. The runtime never makes a model call.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..contracts.execution import (
    Command,
    CommandState,
    ConditionResult,
    ExecutionResult,
    QueuePolicy,
)
from ..contracts.skill import Interruption, SkillSpec
from ..core.clock import Clock, MonotonicClock
from ..core.errors import (
    AdmissionRejected,
    AuthorityError,
    OutcomeNotVerified,
    UnsupportedOperation,
)
from ..plugins import AdapterReport, ExecutionContext, InterruptReport
from ..provenance import tracing
from ..robot import Robot
from ..skills import SkillRegistry
from .admission import DEFAULT_CHECKS, DISPATCH_CHECKS, AdmissionCheck, AdmissionContext
from .authority import AuthorityService
from .resources import ResourceManager
from .state import check_transition
from .trace import Trace

__requirements__ = ("REQ05", "REQ07", "REQ12", "REQ14", "REQ17")

S = CommandState


class CommandHandle:
    """Tracks one submitted command. Await :meth:`wait` or
    :meth:`wait_for_verified_result`; call :meth:`cancel` to request interruption."""

    def __init__(self, command: Command, runtime: Runtime) -> None:
        self.command = command
        self.state = S.SUBMITTED
        self.result: ExecutionResult | None = None
        self.rejection: AdmissionRejected | None = None
        self.started_at: float | None = None
        self.cancel_requested = False
        self.queued = False
        self.inflight = False
        self.trace_id: str | None = None
        self.interrupt_report: InterruptReport | None = None
        self._runtime = runtime
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

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
    ) -> None:
        self.clock = clock or MonotonicClock()
        self.skills = skills
        self.authority = authority or AuthorityService(self.clock, root_principals)
        self.resources = resources or ResourceManager(self.clock)
        self.checks = tuple(checks)
        self.trace = trace or Trace(self.clock)
        self.debug = debug
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
        self._robots: dict[str, Robot] = {}
        self._handles: dict[str, CommandHandle] = {}
        self._active: dict[str, CommandHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, int] = {}
        self._background: set[asyncio.Task[ExecutionResult]] = set()
        self.authority.on_revocation(self._on_revocation)

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
        for r in self._robots.values():
            await r.adapter.start()

    async def stop(self) -> None:
        for h in list(self._handles.values()):
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

    # -- submission ----------------------------------------------------------------
    def handle(self, command_id: str) -> CommandHandle:
        return self._handles[command_id]

    def submit(self, command: Command) -> CommandHandle:
        """Submit a command. Each command gets its own span; a ``command.trace_id``
        received from another participant joins that trace."""
        with tracing.span(f"command:{command.skill_id}", trace_id=command.trace_id):
            return self._submit(command)

    def _submit(self, command: Command) -> CommandHandle:
        existing = self._handles.get(command.command_id)
        if existing is not None:
            if existing.command.same_request(command):
                self.trace.record("duplicate_ignored", command.command_id)
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
                          args=command.args, authority_revision=command.authority_revision)
        try:
            skill = self._admit(command, self.checks)
            if skill.execution.queue_policy is QueuePolicy.REPLACE_LATEST:
                raise UnsupportedOperation("queue policy not implemented by this runtime",
                                           queue_policy=skill.execution.queue_policy.value)
            busy = self._inflight.get(command.robot_id, 0) > 0
            if busy and skill.execution.queue_policy is QueuePolicy.REJECT_IF_BUSY:
                raise AdmissionRejected("robot_busy", "robot is executing another command",
                                        robot_id=command.robot_id)
        except (AdmissionRejected, UnsupportedOperation) as exc:
            rejection = exc if isinstance(exc, AdmissionRejected) else AdmissionRejected(
                exc.code, exc.message, **exc.details)
            self._reject(handle, rejection)
            return handle

        self._transition(handle, S.ACCEPTED)
        handle.queued = busy
        handle.inflight = True
        self._inflight[command.robot_id] = self._inflight.get(command.robot_id, 0) + 1
        self.trace.record("accepted", command.command_id, queued=busy)
        handle._task = asyncio.get_running_loop().create_task(self._run(handle, skill))
        return handle

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

    # -- execution -----------------------------------------------------------------
    async def _run(self, handle: CommandHandle, skill: SkillSpec) -> None:
        cmd = handle.command
        robot = self._robots[cmd.robot_id]
        async with self._lock(cmd.robot_id):
            if handle.cancel_requested:
                self.trace.record("cancelled_before_start", cmd.command_id)
                self._finish(handle, S.CANCELLED, reason="cancelled_before_start",
                             effects={"started": False})
                return
            # Queued commands are re-admitted in full; others re-check authority and
            # fencing to close the window between admission and dispatch.
            try:
                self._admit(cmd, self.checks if handle.queued else DISPATCH_CHECKS)
            except AdmissionRejected as exc:
                self._reject(handle, exc)
                return

            self._transition(handle, S.EXECUTING)
            handle.started_at = self.clock.now()
            self._active[cmd.robot_id] = handle
            self.trace.record("started", cmd.command_id)
            def progress(phase: str, data: Mapping[str, Any]) -> None:
                self.trace.record("progress", cmd.command_id, phase=phase, **dict(data))

            ctx = ExecutionContext(command=cmd, skill=skill, clock=self.clock,
                                   progress=progress,
                                   authority_valid=lambda: self._authority_ok(cmd))
            try:
                report = await asyncio.wait_for(robot.adapter.execute(ctx),
                                                skill.execution.deadline_s)
            except TimeoutError:
                self.trace.record("deadline_exceeded", cmd.command_id,
                                  deadline_s=skill.execution.deadline_s)
                await self._interrupt_quietly(handle, skill, "deadline_exceeded")
                self._finish(handle, S.UNRESOLVED, reason="deadline_exceeded",
                             message="execution started but did not report before its "
                                     "deadline; physical outcome unknown")
                return
            except asyncio.CancelledError:
                self._finish(handle, S.UNRESOLVED, reason="runtime_stopped")
                raise
            except Exception as exc:  # adapter lost contact after start
                self.trace.record("adapter_error", cmd.command_id, error=repr(exc))
                self._finish(handle, S.UNRESOLVED, reason="outcome_unknown",
                             message=f"adapter raised after start: {exc!r}")
                return
            finally:
                self._active.pop(cmd.robot_id, None)

            self._settle(handle, skill, report)

    def _settle(self, handle: CommandHandle, skill: SkillSpec, report: AdapterReport) -> None:
        cmd = handle.command
        self.trace.record("adapter_reported", cmd.command_id, completed=report.completed,
                          message=report.message, effects=report.effects)
        if not report.completed:
            if handle.state is S.CANCELLING:
                stopped = bool(handle.interrupt_report and
                               handle.interrupt_report.stopped_confirmed)
                self._finish(handle, S.CANCELLED, reason="cancelled",
                             message="cancelled" if stopped else
                             "cancelled; stop is NOT physically confirmed",
                             effects={**report.effects, "stopped_confirmed": stopped})
            else:
                self._finish(handle, S.FAILED, reason=report.failure_reason or "reported_failure",
                             message=report.message, effects=report.effects)
            return

        self._transition(handle, S.REPORTED_COMPLETE)
        if not skill.postconditions:
            self.trace.record("unverifiable", cmd.command_id)
            self._finish(handle, S.REPORTED_COMPLETE, reason="no_postconditions_declared",
                         message="adapter reported completion; skill declares nothing to verify",
                         effects=report.effects)
            return
        self._verify(handle, skill, report.effects)

    def _verify(self, handle: CommandHandle, skill: SkillSpec, effects: dict[str, Any],
                reconciled: bool = False) -> None:
        cmd = handle.command
        robot = self._robots[cmd.robot_id]
        observations = robot.observe()
        now = self.clock.now()
        results: list[ConditionResult] = []
        inconclusive = False
        for cond in skill.postconditions:
            obs = observations.get(cond.signal)
            if obs is None or (handle.started_at is not None
                               and obs.acquired_at < handle.started_at):
                inconclusive = True
                results.append(ConditionResult(cond.label(cmd.args), False,
                                               detail="no observation acquired after start"))
                continue
            r = cond.evaluate(observations, cmd.args, now)
            if not r.satisfied and r.detail:
                inconclusive = True
            results.append(r)
        self.trace.record("verification", cmd.command_id, reconciled=reconciled,
                          results=[(r.description, r.satisfied) for r in results])
        if all(r.satisfied for r in results):
            self._finish(handle, S.VERIFIED, reason="reconciled" if reconciled else None,
                         effects=effects, verification=tuple(results))
        elif inconclusive:
            self._finish(handle, S.UNRESOLVED, reason="verification_inconclusive",
                         effects=effects, verification=tuple(results))
        else:
            self._finish(handle, S.FAILED, reason="postcondition_failed", effects=effects,
                         verification=tuple(results))

    # -- interruption, revocation, reconciliation -----------------------------------
    async def cancel(self, command_id: str, reason: str = "cancel_requested") -> ExecutionResult:
        handle = self._handles[command_id]
        if handle.done and handle.state is not S.UNRESOLVED:
            assert handle.result is not None
            return handle.result
        skill = self.skills.get(handle.command.skill_id)
        if handle.state is S.ACCEPTED:
            handle.cancel_requested = True
            self.trace.record("cancel_requested", command_id, reason=reason, started=False)
            return await handle.wait()
        if handle.state is S.EXECUTING:
            if skill.interruption is Interruption.NONE:
                raise UnsupportedOperation("skill cannot be interrupted once started",
                                           skill_id=skill.skill_id)
            handle.cancel_requested = True
            self._transition(handle, S.CANCELLING)
            self.trace.record("cancel_requested", command_id, reason=reason, started=True)
            handle.interrupt_report = await self._robots[handle.command.robot_id] \
                .adapter.interrupt(command_id)
            self.trace.record("interrupt_reported", command_id,
                              accepted=handle.interrupt_report.accepted,
                              stopped_confirmed=handle.interrupt_report.stopped_confirmed)
        return await handle.wait()

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

    def _on_revocation(self, revoked: frozenset[str]) -> None:
        for handle in list(self._active.values()):
            chain = {d.delegation_id for d in self.authority.chain(handle.command.delegation_id)}
            if not chain & revoked:
                continue
            skill = self.skills.get(handle.command.skill_id)
            self.trace.record("authority_revoked_during_execution", handle.command_id,
                              interruptible=skill.interruption is not Interruption.NONE)
            if skill.interruption is not Interruption.NONE and handle.state is S.EXECUTING:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    self.trace.record("revocation_interrupt_deferred", handle.command_id)
                    continue
                task = loop.create_task(self.cancel(handle.command_id,
                                                    reason="authority_revoked"))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

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
    def _lock(self, robot_id: str) -> asyncio.Lock:
        return self._locks.setdefault(robot_id, asyncio.Lock())

    def _transition(self, handle: CommandHandle, new: S) -> None:
        check_transition(handle.state, new)
        handle.state = new

    def _finish(self, handle: CommandHandle, state: S, *, reason: str | None = None,
                message: str = "", effects: dict[str, Any] | None = None,
                verification: tuple[ConditionResult, ...] = ()) -> None:
        if handle.state is not state:
            self._transition(handle, state)
        cmd = handle.command
        robot = self._robots.get(cmd.robot_id)
        cap_rev = None
        if robot is not None:
            with contextlib.suppress(Exception):
                cap_rev = robot.capability_report().capability_revision
        handle.result = ExecutionResult(
            command_id=cmd.command_id, robot_id=cmd.robot_id, skill_id=cmd.skill_id,
            state=state, reason=reason, message=message, observed_effects=effects or {},
            verification=verification, authority_revision=self.authority.revision,
            capability_revision=cap_rev, started_at=handle.started_at,
            finished_at=self.clock.now(),
        )
        self.trace.record(state.value, cmd.command_id, reason=reason)
        if handle.inflight:
            handle.inflight = False
            self._inflight[cmd.robot_id] -= 1
        handle._done.set()


__all__ = ["CommandHandle", "Runtime"]
