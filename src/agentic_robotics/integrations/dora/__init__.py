"""dora-rs bridge (W6.1): AIR skills as a dora operator, and static dataflow checks.
A mapping layer; ``dora`` is imported only to return ``DoraStatus`` when installed."""

from .dataflow import load_dataflow, validate_dataflow
from .operator import INPUTS, OUTPUTS, SkillOperatorCore, goal_status, make_operator

__requirements__ = ("REQ05", "REQ18")

__all__ = ["INPUTS", "OUTPUTS", "SkillOperatorCore", "goal_status", "load_dataflow",
           "make_operator", "validate_dataflow"]
