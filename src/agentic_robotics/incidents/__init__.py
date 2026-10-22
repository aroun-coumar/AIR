"""Incidents: detection, novelty, hypotheses, bounded tests, recovery or handoff (ADR 0015)."""

from .model import (
    BoundedTest,
    Detection,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentState,
    RollbackStrategy,
    TestAction,
    TestBudget,
    TestOutcome,
    TestRun,
)
from .pipeline import IncidentError, IncidentPipeline, is_non_authoritative, signature

__requirements__ = ("REQ14",)

__all__ = [
    "BoundedTest", "Detection", "Hypothesis", "HypothesisStatus", "Incident", "IncidentError",
    "IncidentPipeline", "IncidentState", "RollbackStrategy", "TestAction", "TestBudget",
    "TestOutcome", "TestRun", "is_non_authoritative", "signature",
]
