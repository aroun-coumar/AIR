"""Team coordination protocol (baseline section 11).

Conversation can carry rationale, but every executable decision travels as a typed
:class:`CoordinationMessage` bound to a task *revision*. Participants acknowledge only
the task revisions relevant to them; messages for a superseded revision are ignored.

Version 1.1 of the envelope adds a per-sender sequence number ``seq`` (VDA 5050
``headerId``: +1 per message on one sender->recipient channel, so gaps and duplicates
are detectable), an ``update_id`` for order updates (VDA 5050 ``orderUpdateId``
acceptance rules, see :mod:`.orders`) and a hybrid logical clock timestamp ``hlc`` (see
:mod:`.hlc`). Instant actions (pause, resume, cancel, state_request) are answered at any
revision, and a cancel has its own response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import InvalidTransition, SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ06", "REQ07", "REQ09", "REQ17")


class MessageKind(StrEnum):
    ANNOUNCE = "announce"          # participant -> coordinator: identity, skills, capability
    PROPOSE = "propose"            # coordinator -> eligible participants: task offer
    BID = "bid"                    # participant: can do it, at this cost
    DECLINE = "decline"            # participant: cannot, with reason
    READY_CHECK = "ready_check"    # coordinator -> assignee: still able, for this revision?
    READY = "ready"
    NOT_READY = "not_ready"
    COMMIT = "commit"              # coordinator -> assignee: order update (released steps)
    ACK = "ack"                    # assignee: order update accepted (or identical, ignored)
    STARTED = "started"
    RESULT = "result"              # assignee: execution result (+ occupancy observations)
    QUERY = "query"                # coordinator: what happened to this task? (reconcile)
    STATUS = "status"              # assignee: answer to QUERY
    CANCEL = "cancel"              # coordinator -> assignee: cancel or kill these commands
    # -- 1.1 ----------------------------------------------------------------------------
    CANCEL_RESPONSE = "cancel_response"  # assignee: per-command accept/reject of a cancel
    NACK = "nack"                  # assignee: order update rejected; it keeps current work
    NOT_EXECUTED = "not_executed"  # assignee: a released step is confirmed never started
    INSTANT = "instant"            # pause / resume / state_request / cancel, any revision
    INSTANT_RESPONSE = "instant_response"
    HEARTBEAT = "heartbeat"        # liveness only; never carries work


#: Replies matched to their request by ``causal_ref`` rather than by task revision.
RESPONSE_KINDS = frozenset({MessageKind.CANCEL_RESPONSE, MessageKind.INSTANT_RESPONSE})


class InstantAction(StrEnum):
    """VDA 5050-style instant actions: valid at any task revision."""

    PAUSE = "pause"                # start nothing new; a command already running continues
    RESUME = "resume"
    CANCEL = "cancel"
    STATE_REQUEST = "state_request"


class CancelMode(StrEnum):
    """``cancel`` is graceful: work that cannot be interrupted is allowed to finish and is
    verified normally. ``kill`` stops waiting: whatever is not confirmed stopped becomes
    UNRESOLVED (its holds are kept) instead of finishing under coordination. Neither is
    ever reported as success."""

    CANCEL = "cancel"
    KILL = "kill"


class TrustLevel(StrEnum):
    """How far the coordinator integrates a robot (openTCS ``Vehicle.IntegrationLevel``).

    - ``ignored``: no work; its reports are discarded.
    - ``noticed``: no work; its reports are recorded and may only make occupancy *more*
      conservative (they can never free a resource).
    - ``respected``: no new work (e.g. manual takeover); its holds stay reserved and its
      occupancy reports are applied.
    - ``utilized``: full participant.
    """

    IGNORED = "ignored"
    NOTICED = "noticed"
    RESPECTED = "respected"
    UTILIZED = "utilized"

    @property
    def receives_work(self) -> bool:
        return self is TrustLevel.UTILIZED

    @property
    def reports_applied(self) -> bool:
        return self in (TrustLevel.RESPECTED, TrustLevel.UTILIZED)


@contract("coordination_message", version="1.1")
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
    # -- 1.1 ------------------------------------------------------------------------------
    seq: int = 0                   # per sender->recipient channel; 0 means unstamped
    update_id: int = 0             # order update id (COMMIT, ACK, NACK)
    hlc: str | None = None         # sender's hybrid logical clock timestamp


class TaskState(StrEnum):
    """Team task states. Not a single line: cancellation, failure, reallocation and
    reconciliation are branches with their own rules."""

    PENDING = "pending"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"        # assigned to a participant (allocation decided)
    RESERVED = "reserved"        # exclusive resources held with fencing tokens
    READY = "ready"              # assignee confirmed readiness for this revision
    COMMITTED = "committed"      # commitment ledgered and released to the robot
    EXECUTING = "executing"
    CANCELLING = "cancelling"    # cancel requested; the robot has not confirmed a stop
    COMPLETED = "completed"      # assignee reported a result
    VERIFIED = "verified"
    CANCELLED = "cancelled"      # stopped, or confirmed never started
    KILLED = "killed"            # killed with the stop confirmed (not a graceful cancel)
    FAILED = "failed"
    UNRESOLVED = "unresolved"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.VERIFIED, TaskState.CANCELLED, TaskState.KILLED,
                        TaskState.FAILED)

    @property
    def stopped(self) -> bool:
        """Ended without success (never reported as success)."""
        return self in (TaskState.CANCELLED, TaskState.KILLED, TaskState.FAILED)


_T = TaskState
_STOP = (_T.CANCELLED, _T.KILLED)
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    _T.PENDING: frozenset({_T.PROPOSED, _T.FAILED, *_STOP}),
    _T.PROPOSED: frozenset({_T.ACCEPTED, _T.FAILED, *_STOP}),
    _T.ACCEPTED: frozenset({_T.RESERVED, _T.PROPOSED, _T.FAILED, *_STOP}),
    _T.RESERVED: frozenset({_T.READY, _T.PROPOSED, _T.FAILED, *_STOP}),
    _T.READY: frozenset({_T.COMMITTED, _T.PROPOSED, _T.FAILED, *_STOP}),
    # COMMITTED -> CANCELLED/FAILED only when the robot confirms the step never started.
    _T.COMMITTED: frozenset({_T.EXECUTING, _T.COMPLETED, _T.UNRESOLVED, _T.FAILED,
                             _T.CANCELLING, _T.CANCELLED}),
    _T.EXECUTING: frozenset({_T.COMPLETED, _T.UNRESOLVED, _T.CANCELLING}),
    # A cancel is a request: it can be rejected (back to EXECUTING), lose the race
    # (COMPLETED), be confirmed (CANCELLED/KILLED) or leave the stop unknown (UNRESOLVED).
    _T.CANCELLING: frozenset({_T.EXECUTING, _T.COMPLETED, _T.UNRESOLVED, _T.FAILED,
                              *_STOP}),
    _T.COMPLETED: frozenset({_T.VERIFIED, _T.FAILED, _T.UNRESOLVED, *_STOP}),
    _T.UNRESOLVED: frozenset({_T.VERIFIED, _T.FAILED, *_STOP}),
    _T.VERIFIED: frozenset(),
    _T.CANCELLED: frozenset(),
    _T.KILLED: frozenset(),
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


@contract("team_plan", version="1.1")
class TeamPlan(Contract):
    """Allocation of a mission.

    ``release_window`` (1.1) is how many consecutive steps of one participant's sequence
    are *released* together. A released step may be executed by the robot without
    further contact, so its resources are held before release; the rest of the sequence
    is the *horizon*: planned, still changeable, and never executed by a robot that loses
    its connection (VDA 5050 base vs horizon). ``1`` releases one step at a time.
    """

    plan_id: str
    mission: Mission
    assignments: tuple[Assignment, ...] = ()
    unassigned: tuple[Unassigned, ...] = ()
    trace_id: str | None = None
    release_window: int = 1

    def __post_init__(self) -> None:
        if self.release_window < 1:
            raise SchemaError("release_window must be at least 1",
                              release_window=self.release_window)

    @property
    def complete(self) -> bool:
        return not self.unassigned

    def assignee(self, task_id: str) -> str | None:
        return next((a.participant for a in self.assignments if a.task_id == task_id), None)

    def sequence(self, participant: str) -> tuple[str, ...]:
        """The participant's tasks in mission order (released prefix plus horizon)."""
        mine = {a.task_id for a in self.assignments if a.participant == participant}
        return tuple(t.task_id for t in self.mission.tasks if t.task_id in mine)
