"""Team coordination protocol (baseline section 11).

Conversation can carry rationale, but every executable decision travels as a typed
:class:`CoordinationMessage` bound to a task *revision*. Participants acknowledge only
the task revisions relevant to them; messages for a superseded revision are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import InvalidTransition, SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ06", "REQ07", "REQ09")


class MessageKind(StrEnum):
    ANNOUNCE = "announce"          # participant -> coordinator: identity, skills, capability
    PROPOSE = "propose"            # coordinator -> eligible participants: task offer
    BID = "bid"                    # participant: can do it, at this cost
    DECLINE = "decline"            # participant: cannot, with reason
    READY_CHECK = "ready_check"    # coordinator -> assignee: still able, for this revision?
    READY = "ready"
    NOT_READY = "not_ready"
    COMMIT = "commit"              # coordinator -> assignee: committed, execute with fencing
    ACK = "ack"                    # assignee: commitment received
    STARTED = "started"
    RESULT = "result"              # assignee: execution result (+ occupancy observations)
    QUERY = "query"                # coordinator: what happened to this task? (reconcile)
    STATUS = "status"              # assignee: answer to QUERY
    CANCEL = "cancel"


@contract("coordination_message", version="1.0")
class CoordinationMessage(Contract):
    message_id: str
    conversation_id: str
    task_id: str
    task_revision: int
    sender: str
    recipients: tuple[str, ...]
    kind: MessageKind
    sent_at: float
    causal_ref: str | None = None
    expires_at: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    trace_id: str | None = None


class TaskState(StrEnum):
    """Team task states. Not a single line: cancellation, failure, reallocation and
    reconciliation are branches with their own rules."""

    PENDING = "pending"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"        # assigned to a participant (allocation decided)
    RESERVED = "reserved"        # exclusive resources held with fencing tokens
    READY = "ready"              # assignee confirmed readiness for this revision
    COMMITTED = "committed"      # commitment recorded in the ledger and sent
    EXECUTING = "executing"
    COMPLETED = "completed"      # assignee reported a result
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNRESOLVED = "unresolved"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.VERIFIED, TaskState.CANCELLED, TaskState.FAILED)


_T = TaskState
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    _T.PENDING: frozenset({_T.PROPOSED, _T.CANCELLED, _T.FAILED}),
    _T.PROPOSED: frozenset({_T.ACCEPTED, _T.FAILED, _T.CANCELLED}),
    _T.ACCEPTED: frozenset({_T.RESERVED, _T.PROPOSED, _T.FAILED, _T.CANCELLED}),
    _T.RESERVED: frozenset({_T.READY, _T.PROPOSED, _T.FAILED, _T.CANCELLED}),
    _T.READY: frozenset({_T.COMMITTED, _T.PROPOSED, _T.FAILED, _T.CANCELLED}),
    _T.COMMITTED: frozenset({_T.EXECUTING, _T.COMPLETED, _T.UNRESOLVED, _T.FAILED}),
    _T.EXECUTING: frozenset({_T.COMPLETED, _T.UNRESOLVED}),
    _T.COMPLETED: frozenset({_T.VERIFIED, _T.FAILED, _T.CANCELLED, _T.UNRESOLVED}),
    _T.UNRESOLVED: frozenset({_T.VERIFIED, _T.FAILED, _T.CANCELLED}),
    _T.VERIFIED: frozenset(),
    _T.CANCELLED: frozenset(),
    _T.FAILED: frozenset(),
}


def check_task_transition(current: TaskState, new: TaskState) -> None:
    if new not in TASK_TRANSITIONS[current]:
        raise InvalidTransition("illegal team task transition", current=current.value,
                                new=new.value)


@dataclass(frozen=True)
class MissionTask:
    """A task in a mission, before allocation. ``colocated_with`` forces the same
    participant as another task (e.g. it holds the part); ``assign_to`` pins one."""

    task_id: str
    skill_id: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    colocated_with: str | None = None
    assign_to: str | None = None
    priority: int = 0
    description: str = ""


@contract("mission", version="1.0")
class Mission(Contract):
    mission_id: str
    goal: str
    tasks: tuple[MissionTask, ...]

    def __post_init__(self) -> None:
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise SchemaError("duplicate task ids in mission", mission_id=self.mission_id)
        seen: set[str] = set()
        for t in self.tasks:
            refs = list(t.depends_on) + ([t.colocated_with] if t.colocated_with else [])
            missing = [d for d in refs if d not in seen]
            if missing:
                raise SchemaError("task references a later or unknown task",
                                  task_id=t.task_id, missing=missing)
            seen.add(t.task_id)

    def task(self, task_id: str) -> MissionTask:
        return next(t for t in self.tasks if t.task_id == task_id)


@dataclass(frozen=True)
class Assignment:
    task_id: str
    participant: str
    cost: float
    capability_revision: int
    bids: tuple[tuple[str, float], ...] = ()
    rationale: str = ""


@dataclass(frozen=True)
class Unassigned:
    task_id: str
    reason: str
    declines: dict[str, str] = field(default_factory=dict)


@contract("team_plan", version="1.0")
class TeamPlan(Contract):
    plan_id: str
    mission: Mission
    assignments: tuple[Assignment, ...] = ()
    unassigned: tuple[Unassigned, ...] = ()
    trace_id: str | None = None

    @property
    def complete(self) -> bool:
        return not self.unassigned

    def assignee(self, task_id: str) -> str | None:
        return next((a.participant for a in self.assignments if a.task_id == task_id), None)
