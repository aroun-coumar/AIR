"""Execution contracts, commands and results (baseline sections 6 and 14).

Facts and verdicts are kept apart: an adapter *reports* facts (``AdapterReport``), the
runtime *decides* the outcome from authoritative observations (ADR 0003, ADR 0012).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.clock import SyncState
from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ01", "REQ07", "REQ12", "REQ14")


class QueuePolicy(StrEnum):
    REJECT_IF_BUSY = "reject_if_busy"
    QUEUE_FIFO = "queue_fifo"
    REPLACE_LATEST = "replace_latest"


class DeadlineKind(StrEnum):
    HARD = "hard"
    FIRM = "firm"
    SOFT = "soft"


class ExitMode(StrEnum):
    """What the device does when the runtime stops a skill whose intent lapsed."""

    HOLD = "hold"    # actively hold the current position
    DAMP = "damp"    # go compliant / damped (vendor SDK "damping" mode)


@contract("execution_contract", version="1.1")
class ExecutionContract(Contract):
    """Timing, freshness and fallback terms a feature or skill executes under.

    ``deadline_s`` here is enforced by the runtime as a timeout. It is *not* a hard
    real-time guarantee; such a claim needs evaluated evidence for a deployment profile.

    1.1 adds the intent deadman for continuous skills: when ``keepalive_period_s`` is
    set the commanding principal must call ``Runtime.keepalive`` at that period; if no
    keepalive arrives for ``max_input_age_s`` the runtime stops the skill with
    ``exit_mode`` (Microduck ``safety.rs`` deadman).
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
    keepalive_period_s: float | None = None
    exit_mode: ExitMode = ExitMode.HOLD

    def __post_init__(self) -> None:
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise SchemaError("deadline_s must be positive")
        if self.max_input_age_s <= 0:
            raise SchemaError("max_input_age_s must be positive")
        if self.keepalive_period_s is not None and not (
                0 < self.keepalive_period_s < self.max_input_age_s):
            raise SchemaError("keepalive_period_s must be positive and below max_input_age_s",
                              keepalive_period_s=self.keepalive_period_s,
                              max_input_age_s=self.max_input_age_s)

    @property
    def continuous(self) -> bool:
        return self.keepalive_period_s is not None


class CommandState(StrEnum):
    """Command lifecycle. Acceptance, start, reported completion and verification are
    separate events (baseline section 6, "Skill acceptance and outcome semantics").

    ``AWAITING_APPROVAL`` waits for a human decision bound to this command id;
    ``PAUSED`` is a running command held by an instant action; ``KILLED`` is a forced
    stop (deadman, ``Runtime.kill``), distinct from a cooperative ``CANCELLED``;
    ``APPROXIMATE`` means every postcondition held, some only within their acceptance
    band (never counted as exact); ``SKIPPED`` means the skill did not apply (its
    ``skip_if`` held), which is not a failure and moved nothing.
    """

    SUBMITTED = "submitted"
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    ACCEPTED = "accepted"
    EXECUTING = "executing"
    PAUSED = "paused"
    REPORTED_COMPLETE = "reported_complete"
    VERIFIED = "verified"
    APPROXIMATE = "approximate"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    KILLED = "killed"
    UNRESOLVED = "unresolved"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = {CommandState.REJECTED, CommandState.VERIFIED, CommandState.FAILED,
             CommandState.CANCELLED, CommandState.KILLED, CommandState.APPROXIMATE,
             CommandState.SKIPPED}


class FailureKind(StrEnum):
    """Typed reason a command did not reach a verified outcome (after MoveIt error
    codes, OMPL ``PlannerStatus``, tf2 exceptions and Nav2 error codes)."""

    START_STATE = "start_state"          # the world was not in a state the skill starts from
    GOAL = "goal"                        # goal invalid, unreachable, or not reached
    FRAME_TRANSFORM = "frame_transform"  # a frame or transform was unavailable
    STALE_STATE = "stale_state"          # capability/authority/clock state too old
    STALE_SENSOR = "stale_sensor"        # an input observation was too old or invalid
    COMMUNICATION = "communication"      # contact lost; physical outcome unknown
    PREEMPTED = "preempted"              # stopped by a higher-priority actor
    TIMEOUT = "timeout"                  # deadline exceeded
    KEEPALIVE_LAPSED = "keepalive_lapsed"  # continuous-skill intent was not refreshed
    AUTHORITY = "authority"
    RESOURCE = "resource"
    CAPABILITY = "capability"
    INVALID_REQUEST = "invalid_request"
    APPROVAL = "approval"
    EVIDENCE = "evidence"                # verification was inconclusive
    HARDWARE = "hardware"
    UNKNOWN = "unknown"


_REASON_KIND: dict[str, FailureKind] = {
    "deadline_exceeded": FailureKind.TIMEOUT,
    "outcome_unknown": FailureKind.COMMUNICATION,
    "runtime_stopped": FailureKind.PREEMPTED,
    "runtime_restarted": FailureKind.COMMUNICATION,
    "postcondition_failed": FailureKind.GOAL,
    "verification_inconclusive": FailureKind.EVIDENCE,
    "judgment_disputes_report": FailureKind.EVIDENCE,
    "keepalive_lapsed": FailureKind.KEEPALIVE_LAPSED,
    "kill_unconfirmed": FailureKind.COMMUNICATION,
    "stale_input": FailureKind.STALE_SENSOR,
    "invalid_input": FailureKind.STALE_SENSOR,
    "stale_capability": FailureKind.STALE_STATE,
    "capability_revision_changed": FailureKind.STALE_STATE,
    "clock_quality": FailureKind.STALE_STATE,
    "precondition_failed": FailureKind.START_STATE,
    "capability_unavailable": FailureKind.CAPABILITY,
    "unsupported_skill": FailureKind.INVALID_REQUEST,
    "unknown_skill": FailureKind.INVALID_REQUEST,
    "unknown_robot": FailureKind.INVALID_REQUEST,
    "invalid_arguments": FailureKind.INVALID_REQUEST,
    "command_id_reused": FailureKind.INVALID_REQUEST,
    "outside_workspace": FailureKind.GOAL,
    "relative_target_exceeded": FailureKind.GOAL,
    "non_finite_target": FailureKind.INVALID_REQUEST,
    "robot_busy": FailureKind.RESOURCE,
    "subsystem_occupied": FailureKind.RESOURCE,
    "subsystem_held_unresolved": FailureKind.RESOURCE,
    "missing_fencing_token": FailureKind.RESOURCE,
    "stale_fencing_token": FailureKind.RESOURCE,
    "approval_rejected": FailureKind.APPROVAL,
    "approval_timeout": FailureKind.APPROVAL,
    "approval_unavailable_unattended": FailureKind.APPROVAL,
    "interrupted": FailureKind.PREEMPTED,
    "killed": FailureKind.PREEMPTED,
    "relative_target_unobservable": FailureKind.STALE_SENSOR,
}


def failure_kind_for(reason: str | None) -> FailureKind | None:
    """Map a stable runtime reason to the failure taxonomy (``None`` for no reason)."""
    if reason is None:
        return None
    if reason.startswith("authority_"):
        return FailureKind.AUTHORITY
    return _REASON_KIND.get(reason, FailureKind.UNKNOWN)


@contract("command", version="1.2")
class Command(Contract):
    """1.1 adds ``trace_id`` for causal tracing across participants (compatible).
    1.2 adds ``retry_of``: the failed command an explicit retry decision replaces."""

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
    trace_id: str | None = None
    retry_of: str | None = None

    def same_request(self, other: Command) -> bool:
        """True when ``other`` is a retransmission of this command (for deduplication)."""
        return (self.robot_id, self.skill_id, self.args, self.delegation_id, self.fencing) == (
            other.robot_id, other.skill_id, other.args, other.delegation_id, other.fencing)

    def request_digest(self) -> str:
        """Digest of exactly what this command asks for. An approval binds to it, so an
        approval for one set of arguments can never authorize another."""
        body = {"command_id": self.command_id, "robot_id": self.robot_id,
                "skill_id": self.skill_id, "issued_by": self.issued_by,
                "delegation_id": self.delegation_id, "args": self.args,
                "fencing": self.fencing}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          default=str)
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class EvidencePhase(StrEnum):
    """When evidence was acquired relative to the command's execution window."""

    BEFORE = "before"
    DURING = "during"
    AFTER = "after"


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence the runtime weighed. Only authoritative evidence acquired
    ``after`` the execution window can satisfy a postcondition."""

    signal: str
    source: str
    phase: EvidencePhase
    association_quality: str = "authoritative"
    origin: str = "measured"
    authoritative: bool = True
    acquired_at: float | None = None
    value: Any = None
    verdict: str | None = None     # set for judgments (e.g. a model's "success")
    detail: str = ""


@dataclass(frozen=True)
class ConditionResult:
    description: str
    satisfied: bool
    observed: Any = None
    expected: Any = None
    observation_age_s: float | None = None
    detail: str = ""
    approximate: bool = False


@contract("execution_result", version="1.1")
class ExecutionResult(Contract):
    """1.1 adds the failure taxonomy (``failure``), ``retriable``, the applied command
    (``applied_args`` and ``limited_by`` when limits clamped the request), the
    ``evidence`` weighed, and ``retry_of``."""

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
    failure: FailureKind | None = None
    retriable: bool = False
    applied_args: dict[str, Any] | None = None
    limited_by: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    retry_of: str | None = None

    @property
    def verified(self) -> bool:
        return self.state is CommandState.VERIFIED

    @property
    def approximate(self) -> bool:
        return self.state is CommandState.APPROXIMATE

    @property
    def as_requested(self) -> bool:
        """Verified *and* executed exactly as requested (no limit clamped it)."""
        return self.verified and not self.limited_by


class CancelCode(StrEnum):
    """Answer to a cancel request (ROS 2 ``CancelGoal`` return codes)."""

    ACCEPTED = "accepted"              # cancel is in progress or done
    REJECTED = "rejected"              # the skill cannot be interrupted
    UNKNOWN_COMMAND = "unknown_command"
    ALREADY_TERMINAL = "already_terminal"


@contract("cancel_response", version="1.0")
class CancelResponse(Contract):
    """The cancel request's own answer. It says whether the cancel was accepted and
    whether the stop is physically confirmed; it never says the command succeeded."""

    command_id: str
    code: CancelCode
    state: CommandState | None = None
    started: bool = False
    stop_confirmed: bool | None = None
    message: str = ""


class RetryDecision(StrEnum):
    """An explicit decision on a RETRIABLE failure. The runtime never resends a physical
    action by itself."""

    RETRY = "retry"
    SKIP = "skip"
