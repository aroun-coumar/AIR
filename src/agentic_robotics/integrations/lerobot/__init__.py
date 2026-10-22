"""LeRobot inference-contract mapping and policy client (no lerobot/torch import)."""

from .client import LeRobotPolicyClient, PolicyTransport, PolicyUnavailable, StaleChunk
from .contracts import (
    ActionChunk,
    ActionProvenance,
    ExecutionMode,
    FeatureSpec,
    PolicyCapabilities,
    RobotInterface,
    compatibility_problems,
)

__requirements__ = ("REQ11",)

__all__ = ["ActionChunk", "ActionProvenance", "ExecutionMode", "FeatureSpec",
           "LeRobotPolicyClient", "PolicyCapabilities", "PolicyTransport", "PolicyUnavailable",
           "RobotInterface", "StaleChunk", "compatibility_problems"]
