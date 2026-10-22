"""Admission checks: the gate between a proposed command and execution.

Each check is a small callable that raises :class:`AdmissionRejected` with a stable
``reason``. The default pipeline covers authority, capability, freshness, clock quality,
preconditions and resource fencing (baseline section 5, "Command path"). Products can
append their own checks without replacing the runtime.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts.capability import CapabilityReport, ProfileStatus
from ..contracts.execution import Command
from ..contracts.observation import Observation, Quality
from ..contracts.skill import LimitAction, SkillSpec
from ..core.clock import Clock
from ..core.errors import AdmissionRejected, AuthorityError
from ..robot import Robot
from .authority import AuthorityService
from .resources import ResourceManager

__requirements__ = ("REQ12", "REQ13", "REQ14", "REQ17")


@dataclass
class AdmissionContext:
    command: Command
    skill: SkillSpec
    robot: Robot
    clock: Clock
    authority: AuthorityService
    resources: ResourceManager
    capability: CapabilityReport
    observations: dict[str, Observation]


AdmissionCheck = Callable[[AdmissionContext], None]


def check_supported(ctx: AdmissionContext) -> None:
    if ctx.skill.skill_id not in ctx.robot.adapter.supported_skills():
        raise AdmissionRejected("unsupported_skill", "robot adapter does not support skill",
                                robot_id=ctx.robot.robot_id, skill_id=ctx.skill.skill_id)


def check_arguments(ctx: AdmissionContext) -> None:
    problems = ctx.skill.validate_args(ctx.command.args)
    if problems:
        raise AdmissionRejected("invalid_arguments", "command arguments are invalid",
                                problems=problems)


def check_authority(ctx: AdmissionContext) -> None:
    c = ctx.command
    try:
        ctx.authority.check(c.delegation_id, c.issued_by, c.robot_id, c.skill_id,
                            issued_under_revision=c.authority_revision)
    except AuthorityError as exc:
        details = dict(exc.details)
        reason = str(details.pop("reason", "denied"))
        raise AdmissionRejected(f"authority_{reason}", exc.message, **details) from exc


def check_clock(ctx: AdmissionContext) -> None:
    status = ctx.clock.status()
    if status.sync_state not in ctx.skill.execution.allowed_clock_states:
        raise AdmissionRejected("clock_quality", "clock synchronization is inadequate",
                                domain=status.domain, sync_state=status.sync_state.value)


def check_capability(ctx: AdmissionContext) -> None:
    report, now = ctx.capability, ctx.clock.now()
    max_age = ctx.skill.execution.max_input_age_s
    if report.age(now) > max_age:
        raise AdmissionRejected("stale_capability", "capability report is too old",
                                age_s=round(report.age(now), 3), max_age_s=max_age)
    c = ctx.command
    if c.capability_revision is not None and c.capability_revision != report.capability_revision:
        raise AdmissionRejected("capability_revision_changed",
                                "command was planned against a different capability revision",
                                planned=c.capability_revision,
                                current=report.capability_revision)
    for name in ctx.skill.required_capabilities:
        profile = report.get(name)
        if profile is None or profile.status is ProfileStatus.UNAVAILABLE:
            raise AdmissionRejected(
                "capability_unavailable", "required capability is not currently available",
                capability=name,
                limiting_factors=list(profile.limiting_factors) if profile else ["not_reported"])


def check_preconditions(ctx: AdmissionContext) -> None:
    now = ctx.clock.now()
    max_age = ctx.skill.execution.max_input_age_s
    for cond in ctx.skill.preconditions:
        signal = cond.resolved_signal(ctx.command.args)
        obs = ctx.observations.get(signal)
        if obs is not None and obs.age(now) > max_age:
            raise AdmissionRejected("stale_input", "input observation is too old",
                                    signal=signal, age_s=round(obs.age(now), 3),
                                    max_age_s=max_age)
        if obs is not None and obs.quality is Quality.INVALID:
            raise AdmissionRejected("invalid_input", "input observation is invalid",
                                    signal=signal)
        result = cond.evaluate(ctx.observations, ctx.command.args, now)
        if not result.satisfied:
            raise AdmissionRejected("precondition_failed", "precondition is not satisfied",
                                    condition=result.description, observed=result.observed,
                                    detail=result.detail)


def check_fencing(ctx: AdmissionContext) -> None:
    for resource in ctx.skill.resources_for(ctx.command.args):
        problem = ctx.resources.validate_fence(resource, ctx.command.fencing.get(resource),
                                               ctx.command.issued_by)
        if problem:
            raise AdmissionRejected(problem, "command does not hold the required resource",
                                    resource=resource,
                                    current_token=ctx.resources.current_token(resource))


_BASE_CHECKS: tuple[AdmissionCheck, ...] = (
    check_supported,
    check_arguments,
    check_authority,
    check_clock,
    check_capability,
    check_preconditions,
    check_fencing,
)

# Re-checked immediately before dispatch, to close the gap between admission and start.
DISPATCH_CHECKS: tuple[AdmissionCheck, ...] = (check_authority, check_fencing)


def apply_motion_limits(skill: SkillSpec, args: Mapping[str, Any],
                        observations: Mapping[str, Observation],
                        now: float) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return ``(applied_args, limited_by)`` after the skill's workspace box and
    ``max_relative_target`` limits. Raises :class:`AdmissionRejected` for a non-finite
    target, or a violation of a ``reject`` limit. ``limited_by`` names each clamp."""
    applied = dict(args)
    limited: list[str] = []
    for name, value in args.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise AdmissionRejected("non_finite_target", "target is not a finite number",
                                    argument=name)
    box = skill.workspace
    if box is not None:
        for name, (lo, hi) in ((n, (b[0], b[1])) for n, b in box.bounds.items()):
            if name not in applied:
                continue
            v = float(applied[name])
            if lo <= v <= hi:
                continue
            if box.action is LimitAction.REJECT:
                raise AdmissionRejected("outside_workspace", "target lies outside the "
                                        "skill's workspace box", argument=name, value=v,
                                        bounds=[lo, hi], frame=box.frame)
            applied[name] = min(max(v, lo), hi)
            limited.append(f"workspace:{name}")
    max_age = skill.execution.max_input_age_s
    for limit in skill.max_relative_target:
        if limit.arg not in applied:
            continue
        obs = observations.get(limit.signal)
        current: Any = obs.value if obs is not None else None
        if obs is not None and limit.field:
            for part in limit.field.split("."):
                current = current.get(part) if isinstance(current, Mapping) else None
        if (obs is None or not obs.authoritative or obs.age(now) > max_age
                or not isinstance(current, (int, float)) or isinstance(current, bool)):
            raise AdmissionRejected("relative_target_unobservable",
                                    "cannot bound the target: no fresh authoritative "
                                    "observation of the current value",
                                    argument=limit.arg, signal=limit.signal)
        target = float(applied[limit.arg])
        delta = target - float(current)
        if abs(delta) <= limit.max_delta:
            continue
        if limit.action is LimitAction.REJECT:
            raise AdmissionRejected("relative_target_exceeded", "target moves further than "
                                    "max_relative_target allows", argument=limit.arg,
                                    delta=round(delta, 6), max_delta=limit.max_delta)
        applied[limit.arg] = float(current) + math.copysign(limit.max_delta, delta)
        limited.append(f"max_relative_target:{limit.arg}")
    return applied, tuple(limited)


def check_motion_limits(ctx: AdmissionContext) -> None:
    apply_motion_limits(ctx.skill, ctx.command.args, ctx.observations, ctx.clock.now())


DEFAULT_CHECKS: tuple[AdmissionCheck, ...] = (*_BASE_CHECKS, check_motion_limits)
