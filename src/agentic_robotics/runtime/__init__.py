"""Local runtime: admission, authority, fenced resources, execution and verification."""

from .admission import (
    DEFAULT_CHECKS,
    DISPATCH_CHECKS,
    AdmissionCheck,
    AdmissionContext,
)
from .authority import AuthorityService
from .plans import PlanExecutor, PlanHandle, PlanResult, PlanState
from .resources import ResourceManager
from .runtime import CommandHandle, Runtime
from .state import TRANSITIONS, check_transition
from .trace import Trace, TraceEvent

__all__ = [
    "DEFAULT_CHECKS",
    "DISPATCH_CHECKS",
    "TRANSITIONS",
    "AdmissionCheck",
    "AdmissionContext",
    "AuthorityService",
    "CommandHandle",
    "PlanExecutor",
    "PlanHandle",
    "PlanResult",
    "PlanState",
    "ResourceManager",
    "Runtime",
    "Trace",
    "TraceEvent",
    "check_transition",
]
