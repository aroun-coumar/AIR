"""Agentic Robotics Framework.

A reusable library and framework for building agentic robotic products: typed
execution contracts, a local runtime with admission, authority and verification, a
model-agnostic task agent, pluggable robot adapters, and a local asset registry.

    from agentic_robotics import Robot, Runtime, SkillRegistry, TaskAgent
"""

from .agents import AgentBudget, ContextBuilder, ModelProvider, ScriptedModel, TaskAgent
from .contracts import (
    CapabilityReport,
    Command,
    CommandState,
    ComponentManifest,
    Delegation,
    ExecutionContract,
    ExecutionResult,
    Observation,
    SkillSpec,
    TaskPlan,
    TaskSpec,
)
from .core import (
    AdmissionRejected,
    FrameworkError,
    ManualClock,
    MonotonicClock,
    OutcomeNotVerified,
    UnsupportedOperation,
)
from .plugins import (
    AdapterReport,
    ExecutionContext,
    InterruptReport,
    RobotAdapter,
    register_adapter,
)
from .registry import LocalRegistry
from .robot import Robot
from .runtime import AuthorityService, PlanExecutor, ResourceManager, Runtime, Trace
from .skills import SkillRegistry

__version__ = "0.1.0.dev0"

__all__ = [
    "AdapterReport",
    "AdmissionRejected",
    "AgentBudget",
    "AuthorityService",
    "CapabilityReport",
    "Command",
    "CommandState",
    "ComponentManifest",
    "ContextBuilder",
    "Delegation",
    "ExecutionContext",
    "ExecutionContract",
    "ExecutionResult",
    "FrameworkError",
    "InterruptReport",
    "LocalRegistry",
    "ManualClock",
    "ModelProvider",
    "MonotonicClock",
    "Observation",
    "OutcomeNotVerified",
    "PlanExecutor",
    "ResourceManager",
    "Robot",
    "RobotAdapter",
    "Runtime",
    "ScriptedModel",
    "SkillRegistry",
    "SkillSpec",
    "TaskAgent",
    "TaskPlan",
    "TaskSpec",
    "Trace",
    "UnsupportedOperation",
    "__version__",
    "register_adapter",
]
