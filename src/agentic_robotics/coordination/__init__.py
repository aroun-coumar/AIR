"""Multi-robot coordination: typed commitment protocol, allocation, fenced reservations,
selective acknowledgements and reconciliation (baseline section 11)."""

from .allocation import Allocator, Bid, LowestCostAllocator
from .coordinator import (
    CoordinatorTimeouts,
    MissionResult,
    MissionState,
    ParticipantInfo,
    TaskRecord,
    TeamCoordinator,
)
from .participant import Participant
from .protocol import (
    TASK_TRANSITIONS,
    Assignment,
    CoordinationMessage,
    MessageKind,
    Mission,
    MissionTask,
    TaskState,
    TeamPlan,
    Unassigned,
    check_task_transition,
)
from .team import TEAM_TRACE_KINDS, Team
from .transport import LinkFaults, LocalTransport, Transport, TransportStats

__all__ = [
    "TASK_TRANSITIONS",
    "TEAM_TRACE_KINDS",
    "Allocator",
    "Assignment",
    "Bid",
    "CoordinationMessage",
    "CoordinatorTimeouts",
    "LinkFaults",
    "LocalTransport",
    "LowestCostAllocator",
    "MessageKind",
    "Mission",
    "MissionResult",
    "MissionState",
    "MissionTask",
    "Participant",
    "ParticipantInfo",
    "TaskRecord",
    "TaskState",
    "Team",
    "TeamCoordinator",
    "TeamPlan",
    "Transport",
    "TransportStats",
    "Unassigned",
    "check_task_transition",
]
