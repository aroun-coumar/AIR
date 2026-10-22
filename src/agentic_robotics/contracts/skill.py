"""Skill and task specifications (baseline section 6).

A skill is a callable operation with declared requirements and observable outcomes.
Pre- and postconditions are declarative :class:`Condition` objects evaluated against
observations, so the runtime (not the implementation) decides whether an outcome is
verified.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from ..core.units import dimension_of
from .execution import ConditionResult, ExecutionContract
from .observation import Observation, Quality

__requirements__ = ("REQ01", "REQ05", "REQ12", "REQ14")

ARG_REF = "$args."


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    APPROX = "approx"
    PRESENT = "present"


def resolve_arg(value: Any, args: Mapping[str, Any]) -> Any:
    """Resolve ``"$args.name"`` references against command arguments."""
    if isinstance(value, str) and value.startswith(ARG_REF):
        key = value[len(ARG_REF):]
        if key not in args:
            raise SchemaError("condition references a missing argument", argument=key)
        return args[key]
    return value


@dataclass(frozen=True)
class Condition:
    signal: str
    op: Op
    value: Any = None
    field: str | None = None
    tolerance: float = 0.0
    description: str = ""
    accept_within: float | None = None

    def __post_init__(self) -> None:
        if self.accept_within is not None:
            if self.op is not Op.APPROX:
                raise SchemaError("accept_within applies only to approx conditions",
                                  signal=self.signal)
            if not (math.isfinite(self.accept_within) and self.accept_within >= self.tolerance):
                raise SchemaError("accept_within must be finite and at least the tolerance",
                                  signal=self.signal)

    def resolved_signal(self, args: Mapping[str, Any]) -> str:
        """Signal names may template arguments: ``"station.{station}.part"``."""
        if "{" not in self.signal:
            return self.signal
        try:
            return self.signal.format_map(dict(args))
        except (KeyError, IndexError, ValueError) as exc:
            raise SchemaError("condition signal references a missing argument",
                              signal=self.signal, error=str(exc)) from exc

    def label(self, args: Mapping[str, Any] | None = None) -> str:
        if self.description:
            return self.description
        sig = self.resolved_signal(args) if args is not None else self.signal
        target = sig + (f".{self.field}" if self.field else "")
        expected = resolve_arg(self.value, args or {}) if self.op is not Op.PRESENT else ""
        return f"{target} {self.op.value} {expected}".strip()

    def evaluate(
        self, observations: Mapping[str, Observation], args: Mapping[str, Any], now: float,
        *, require_authoritative: bool = False,
    ) -> ConditionResult:
        """Evaluate against ``observations``. With ``require_authoritative`` (used for
        postconditions) a model judgment or best-effort association never satisfies
        the condition; the result is then inconclusive (``detail`` is set)."""
        label = self.label(args)
        obs = observations.get(self.resolved_signal(args))
        if obs is None:
            return ConditionResult(label, False, detail="no observation for signal")
        age = obs.age(now)
        if obs.quality is Quality.INVALID:
            return ConditionResult(label, False, observation_age_s=age,
                                   detail="observation quality is invalid")
        if require_authoritative and not obs.authoritative:
            return ConditionResult(label, False, observation_age_s=age,
                                   detail=f"evidence is not authoritative "
                                          f"(origin={obs.origin.value}, association="
                                          f"{obs.association_quality.value})")
        observed: Any = obs.value
        if self.field:
            for part in self.field.split("."):
                if not isinstance(observed, Mapping) or part not in observed:
                    return ConditionResult(label, False, observation_age_s=age,
                                           detail=f"field {self.field!r} not present")
                observed = observed[part]
        if self.op is Op.PRESENT:
            return ConditionResult(label, observed is not None, observed, None, age)
        expected = resolve_arg(self.value, args)
        try:
            ok = _compare(self.op, observed, expected, self.tolerance)
            approx = (not ok and self.accept_within is not None
                      and _compare(self.op, observed, expected, self.accept_within))
        except (TypeError, ValueError):
            return ConditionResult(label, False, observed, expected, age,
                                   detail="values are not comparable")
        if approx:
            return ConditionResult(label, True, observed, expected, age,
                                   detail=f"within acceptance band {self.accept_within}, "
                                          f"outside tolerance {self.tolerance}",
                                   approximate=True)
        return ConditionResult(label, ok, observed, expected, age)


def _compare(op: Op, a: Any, b: Any, tol: float) -> bool:
    if op is Op.EQ:
        return bool(a == b)
    if op is Op.NE:
        return bool(a != b)
    if op is Op.LT:
        return bool(a < b)
    if op is Op.LE:
        return bool(a <= b)
    if op is Op.GT:
        return bool(a > b)
    if op is Op.GE:
        return bool(a >= b)
    if op is Op.APPROX:
        return math.isclose(float(a), float(b), abs_tol=tol)
    raise TypeError(op)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # "number" | "integer" | "string" | "boolean" | "object"
    required: bool = True
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.type not in _PARAM_TYPES:
            raise SchemaError("unknown parameter type", param=self.name, type=self.type)
        if self.unit is not None:
            dimension_of(self.unit)

    def check(self, value: Any) -> str | None:
        """Return an error description, or ``None`` if ``value`` is acceptable."""
        if not _PARAM_TYPES[self.type](value):
            return f"{self.name}: expected {self.type}"
        if self.type == "number" and not math.isfinite(value):
            return f"{self.name}: not a finite number"
        if self.minimum is not None and value < self.minimum:
            return f"{self.name}: below minimum {self.minimum}"
        if self.maximum is not None and value > self.maximum:
            return f"{self.name}: above maximum {self.maximum}"
        return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_PARAM_TYPES: dict[str, Callable[[Any], bool]] = {
    "number": _is_number,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
}


class Interruption(StrEnum):
    """How a running skill may be interrupted."""

    NONE = "none"          # cannot be interrupted once started
    CANCEL = "cancel"      # can be stopped; outcome must then be observed
    PAUSE = "pause"        # can be paused and resumed (and cancelled)


class Blocking(StrEnum):
    """What else may run on the same robot while this skill runs (VDA 5050
    ``blockingType``). ``hard``: nothing else; ``soft``: only ``none`` skills;
    ``none``: anything that is not ``hard``. ``occupies`` subsystems are exclusive
    regardless."""

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


class LimitAction(StrEnum):
    REJECT = "reject"
    CLAMP = "clamp"


@dataclass(frozen=True)
class WorkspaceBox:
    """Axis-aligned box the target arguments must lie in (RAI ``WorkspaceBounds``).

    ``bounds`` maps an argument name to ``[min, max]`` in ``frame``. Non-finite targets
    are always rejected. ``clamp`` moves an out-of-box target onto the box and reports
    the applied command; ``reject`` refuses it."""

    frame: str
    bounds: dict[str, tuple[float, ...]]
    action: LimitAction = LimitAction.REJECT

    def __post_init__(self) -> None:
        if not self.bounds:
            raise SchemaError("workspace box needs at least one bounded argument")
        for name, b in self.bounds.items():
            if len(b) != 2 or not all(math.isfinite(v) for v in b) or b[0] > b[1]:
                raise SchemaError("workspace bound must be a finite [min, max] pair",
                                  argument=name, bound=list(b))


@dataclass(frozen=True)
class RelativeTargetLimit:
    """Largest allowed change of a target argument from its currently observed value
    (LeRobot ``max_relative_target``). The current value is read from ``signal``
    (optionally ``field``) at dispatch."""

    arg: str
    signal: str
    max_delta: float
    field: str | None = None
    action: LimitAction = LimitAction.CLAMP

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_delta) and self.max_delta > 0):
            raise SchemaError("max_delta must be finite and positive", arg=self.arg)


@contract("skill_spec", version="1.1")
class SkillSpec(Contract):
    skill_id: str
    version: str
    description: str
    inputs: tuple[ParamSpec, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    preconditions: tuple[Condition, ...] = ()
    postconditions: tuple[Condition, ...] = ()
    exclusive_resources: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    interruption: Interruption = Interruption.CANCEL
    execution: ExecutionContract = field(default_factory=ExecutionContract)
    expected_evidence: tuple[str, ...] = ()
    on_uncertain_outcome: str = "reconcile"
    experimental: bool = False
    # -- 1.1 (ADR 0012) -----------------------------------------------------------------
    occupies: tuple[str, ...] = ()
    blocking: Blocking = Blocking.HARD
    skip_if: tuple[Condition, ...] = ()
    requires_approval: bool = False
    approval_timeout_s: float = 60.0
    retriable_failures: tuple[str, ...] = ()
    workspace: WorkspaceBox | None = None
    max_relative_target: tuple[RelativeTargetLimit, ...] = ()

    def __post_init__(self) -> None:
        if self.approval_timeout_s <= 0:
            raise SchemaError("approval_timeout_s must be positive", skill_id=self.skill_id)
        declared = {p.name for p in self.inputs}
        limited = [*(self.workspace.bounds if self.workspace else ()),
                   *(r.arg for r in self.max_relative_target)]
        unknown = sorted(set(limited) - declared)
        if unknown:
            raise SchemaError("motion limits name undeclared inputs", skill_id=self.skill_id,
                              arguments=unknown)

    def validate_args(self, args: Mapping[str, Any]) -> list[str]:
        problems = []
        declared = {p.name: p for p in self.inputs}
        for name in args:
            if name not in declared:
                problems.append(f"{name}: unknown argument")
        for p in self.inputs:
            if p.name not in args:
                if p.required:
                    problems.append(f"{p.name}: required")
                continue
            err = p.check(args[p.name])
            if err:
                problems.append(err)
        return problems

    def resources_for(self, args: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(str(resolve_arg(r, args)) for r in self.exclusive_resources)

    def tool_description(self) -> dict[str, Any]:
        """A compact, typed description an agent can use to choose this skill.

        This is information only. Describing a tool to a model grants no authority.
        """
        return {
            "skill_id": self.skill_id,
            "description": self.description,
            "inputs": {
                p.name: {k: v for k, v in {
                    "type": p.type, "unit": p.unit, "required": p.required,
                    "minimum": p.minimum, "maximum": p.maximum,
                    "description": p.description or None}.items() if v is not None}
                for p in self.inputs
            },
            "requires": list(self.required_capabilities),
            "side_effects": list(self.side_effects),
        }


@contract("skill_set", version="1.1")
class SkillSet(Contract):
    """1.1 carries ``skill_spec`` 1.1 entries."""

    skills: tuple[SkillSpec, ...]


@dataclass(frozen=True)
class TaskSpec:
    """One step of a plan: run ``skill_id`` on ``robot_id`` with ``args``."""

    task_id: str
    skill_id: str
    robot_id: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    rationale: str = ""


@contract("task_plan", version="1.1")
class TaskPlan(Contract):
    """1.1 adds ``trace_id``: the trace of the decision that produced the plan."""

    plan_id: str
    goal: str
    steps: tuple[TaskSpec, ...]
    produced_by: str = "unspecified"
    rationale: str = ""
    trace_id: str | None = None

    def __post_init__(self) -> None:
        ids = [s.task_id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise SchemaError("duplicate task ids in plan", plan_id=self.plan_id)
        seen: set[str] = set()
        for s in self.steps:
            missing = [d for d in s.depends_on if d not in seen]
            if missing:
                raise SchemaError("task depends on a later or unknown task",
                                  task_id=s.task_id, missing=missing)
            seen.add(s.task_id)
