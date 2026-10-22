"""Execution contracts, commands and results (baseline sections 6 and 14)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.clock import SyncState
from ..core.errors import SchemaError
from ..core.schema import Contract, contract


class QueuePolicy(StrEnum):
    REJECT_IF_BUSY = "reject_if_busy"
    QUEUE_FIFO = "queue_fifo"
    REPLACE_LATEST = "replace_latest"


class DeadlineKind(StrEnum):
    HARD = "hard"
    FIRM = "firm"
    SOFT = "soft"


@contract("execution_contract", version="1.0")
class ExecutionContract(Contract):
    """Timing, freshness and fallback terms a feature or skill executes under.

    ``deadline_s`` here is enforced by the runtime as a timeout. It is *not* a hard
    real-time guarantee; such a claim needs evaluated evidence for a deployment profile.
    """

    trigger: str = "on_command"
    deadline_s: float | None = None
    deadline_kind: DeadlineKind = DeadlineKind.FIRM
    period_s: float | None = None
    max_input_age_s: float = 1.0
    result_validity_s: float | None = None
    jitter_s: float | None = None
    queue_policy: QueuePolicy = QueuePolicy.REJECT_IF_BUSY
    compute_budget: str | None = None
    memory_budget_mb: float | None = None
    allowed_clock_states: tuple[SyncState, ...] = (SyncState.LOCKED, SyncState.HOLDOVER)
    fallback: str = "stop"

    def __post_init__(self) -> None:
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise SchemaError("deadline_s must be positive")
        if self.max_input_age_s <= 0:
            raise SchemaError("max_input_age_s must be positive")


class CommandState(StrEnum):
    """Command lifecycle. Acceptance, start, reported completion and verification are
    separate events (baseline section 6, "Skill acceptance and outcome semantics")."""

    SUBMITTED = "submitted"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    EXECUTING = "executing"
    REPORTED_COMPLETE = "reported_complete"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    UNRESOLVED = "unresolved"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = {CommandState.REJECTED, CommandState.VERIFIED, CommandState.FAILED,
             CommandState.CANCELLED}


@contract("command", version="1.0")
class Command(Contract):
    command_id: str
    robot_id: str
    skill_id: str
    issued_by: str
    delegation_id: str
    authority_revision: int
    args: dict[str, Any] = field(default_factory=dict)
    capability_revision: int | None = None
    fencing: dict[str, int] = field(default_factory=dict)
    task_id: str | None = None
    plan_id: str | None = None

    def same_request(self, other: Command) -> bool:
        """True when ``other`` is a retransmission of this command (for deduplication)."""
        return (self.robot_id, self.skill_id, self.args, self.delegation_id, self.fencing) == (
            other.robot_id, other.skill_id, other.args, other.delegation_id, other.fencing)


@dataclass(frozen=True)
class ConditionResult:
    description: str
    satisfied: bool
    observed: Any = None
    expected: Any = None
    observation_age_s: float | None = None
    detail: str = ""


@contract("execution_result", version="1.0")
class ExecutionResult(Contract):
    command_id: str
    robot_id: str
    skill_id: str
    state: CommandState
    reason: str | None = None
    message: str = ""
    observed_effects: dict[str, Any] = field(default_factory=dict)
    verification: tuple[ConditionResult, ...] = ()
    authority_revision: int | None = None
    capability_revision: int | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def verified(self) -> bool:
        return self.state is CommandState.VERIFIED
