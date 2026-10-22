"""Plan execution: turns a typed :class:`TaskPlan` into fenced, authorized commands.

Reservations for a step's exclusive resources are acquired before its command is
issued and released only once the outcome is known. If the outcome is UNRESOLVED the
reservation is *kept*: the resource may still be physically occupied.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from ..contracts.execution import Command, CommandState, ExecutionResult
from ..contracts.resources import ResourceReservation
from ..contracts.skill import TaskPlan, TaskSpec
from ..core.errors import OutcomeNotVerified, ResourceConflict
from ..core.ids import new_id
from .runtime import Runtime


class PlanState(StrEnum):
    RUNNING = "running"
    VERIFIED = "verified"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    CANCELLED = "cancelled"


@dataclass
class PlanResult:
    plan_id: str
    state: PlanState
    steps: dict[str, ExecutionResult] = field(default_factory=dict)
    stopped_at: str | None = None
    held_reservations: list[ResourceReservation] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.state is PlanState.VERIFIED


class PlanHandle:
    def __init__(self, plan: TaskPlan, task: asyncio.Task[PlanResult]) -> None:
        self.plan = plan
        self._task = task

    async def wait(self, timeout: float | None = None) -> PlanResult:
        return await asyncio.wait_for(asyncio.shield(self._task), timeout)

    async def wait_for_verified_result(self, timeout: float | None = None) -> PlanResult:
        result = await self.wait(timeout)
        if not result.verified:
            raise OutcomeNotVerified("plan did not reach a verified outcome", result,
                                     plan_id=self.plan.plan_id, state=result.state.value,
                                     stopped_at=result.stopped_at)
        return result


class PlanExecutor:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def submit(self, plan: TaskPlan, *, principal: str, delegation_id: str,
               lease_s: float | None = None) -> PlanHandle:
        task = asyncio.get_running_loop().create_task(
            self._execute(plan, principal, delegation_id, lease_s))
        return PlanHandle(plan, task)

    async def _execute(self, plan: TaskPlan, principal: str, delegation_id: str,
                       lease_s: float | None) -> PlanResult:
        rt = self.runtime
        result = PlanResult(plan.plan_id, PlanState.RUNNING)
        rt.trace.record("plan_started", plan.plan_id, goal=plan.goal,
                        steps=[s.task_id for s in plan.steps], produced_by=plan.produced_by)
        for step in plan.steps:
            reservations: list[ResourceReservation] = []
            try:
                reservations = self._reserve(step, principal, lease_s)
            except ResourceConflict as exc:
                rt.trace.record("plan_step_blocked", plan.plan_id, task_id=step.task_id,
                                error=exc.to_dict())
                for r in reservations:
                    rt.resources.release(r.reservation_id)
                result.state, result.stopped_at = PlanState.FAILED, step.task_id
                break

            command = self._command(plan, step, principal, delegation_id, reservations)
            handle = rt.submit(command)
            step_result = await handle.wait()
            result.steps[step.task_id] = step_result

            if step_result.state is CommandState.UNRESOLVED:
                result.held_reservations.extend(reservations)
                result.state, result.stopped_at = PlanState.UNRESOLVED, step.task_id
                break
            for r in reservations:
                released = rt.resources.release(r.reservation_id)
                rt.trace.record("reservation_released", plan.plan_id,
                                resource=released.resource, task_id=step.task_id,
                                physical_occupancy=released.physical_occupancy.value)
            if step_result.state is not CommandState.VERIFIED:
                result.state = (PlanState.CANCELLED
                                if step_result.state is CommandState.CANCELLED
                                else PlanState.FAILED)
                result.stopped_at = step.task_id
                break
        else:
            result.state = PlanState.VERIFIED

        rt.trace.record("plan_finished", plan.plan_id, state=result.state.value,
                        stopped_at=result.stopped_at)
        return result

    def _reserve(self, step: TaskSpec, principal: str,
                 lease_s: float | None) -> list[ResourceReservation]:
        skill = self.runtime.skills.get(step.skill_id)
        out: list[ResourceReservation] = []
        try:
            for resource in skill.resources_for(step.args):
                out.append(self.runtime.resources.reserve(resource, principal, lease_s))
        except ResourceConflict:
            for r in out:
                self.runtime.resources.release(r.reservation_id)
            raise
        return out

    def _command(self, plan: TaskPlan, step: TaskSpec, principal: str, delegation_id: str,
                 reservations: list[ResourceReservation]) -> Command:
        robot = self.runtime.robot(step.robot_id)
        return Command(
            command_id=new_id("cmd"), robot_id=step.robot_id, skill_id=step.skill_id,
            issued_by=principal, delegation_id=delegation_id,
            authority_revision=self.runtime.authority.revision,
            capability_revision=robot.capability_report().capability_revision,
            args=dict(step.args), fencing={r.resource: r.fencing_token for r in reservations},
            task_id=step.task_id, plan_id=plan.plan_id,
        )
