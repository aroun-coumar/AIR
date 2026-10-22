"""Delegation and human intervention contracts (baseline sections 6 and 17)."""

from __future__ import annotations

from dataclasses import field
from enum import StrEnum

from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ01", "REQ17")

ANY = "*"


@contract("delegation", version="1.0")
class Delegation(Contract):
    """Scoped authority granted by ``grantor`` to ``grantee``.

    A child delegation can never be broader than, or outlive, its parent.
    """

    delegation_id: str
    grantor: str
    grantee: str
    robots: tuple[str, ...] = (ANY,)
    skills: tuple[str, ...] = (ANY,)
    operations: tuple[str, ...] = ("execute",)
    parent_id: str | None = None
    valid_from: float = 0.0
    valid_until: float | None = None
    may_redelegate: bool = False
    limits: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise SchemaError("delegation validity window is empty",
                              delegation_id=self.delegation_id)
        if not self.robots or not self.skills or not self.operations:
            raise SchemaError("delegation scope cannot be empty",
                              delegation_id=self.delegation_id)

    def covers(self, robot_id: str, skill_id: str, operation: str = "execute") -> bool:
        return (_in(robot_id, self.robots) and _in(skill_id, self.skills)
                and _in(operation, self.operations))

    def valid_at(self, t: float) -> bool:
        return t >= self.valid_from and (self.valid_until is None or t < self.valid_until)

    def within(self, parent: Delegation) -> list[str]:
        """Return the ways this delegation would exceed ``parent`` (empty if it fits)."""
        problems = []
        for attr in ("robots", "skills", "operations"):
            mine, theirs = getattr(self, attr), getattr(parent, attr)
            if ANY in theirs:
                continue
            if ANY in mine or not set(mine) <= set(theirs):
                problems.append(f"{attr} broader than parent")
        if self.valid_from < parent.valid_from:
            problems.append("starts before parent")
        if parent.valid_until is not None and (
            self.valid_until is None or self.valid_until > parent.valid_until
        ):
            problems.append("outlives parent")
        for k, v in parent.limits.items():
            if k not in self.limits or self.limits[k] > v:
                problems.append(f"limit {k} broader than parent")
        return problems


def _in(value: str, allowed: tuple[str, ...]) -> bool:
    return ANY in allowed or value in allowed


class InterventionKind(StrEnum):
    PRIORITY_CHANGE = "priority_change"
    MISSION_HOLD = "mission_hold"
    TASK_CANCEL = "task_cancel"
    MANUAL_TAKEOVER = "manual_takeover"
    MAINTENANCE_MODE = "maintenance_mode"
    PROTECTIVE_STOP = "protective_stop"
    HANDBACK = "handback"


class InterventionState(StrEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    TRANSITIONING = "transitioning"
    ACTIVE = "active"
    UNCONFIRMED = "unconfirmed"
    COMPLETED = "completed"


@contract("human_intervention", version="1.0")
class HumanIntervention(Contract):
    intervention_id: str
    operator: str
    kind: InterventionKind
    robots: tuple[str, ...]
    state: InterventionState = InterventionState.REQUESTED
    revoked_delegations: tuple[str, ...] = ()
    command_owner: str | None = None
    physically_confirmed: bool = False
    handback_conditions: tuple[str, ...] = ()
