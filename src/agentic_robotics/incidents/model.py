"""Incident contracts: detections, incidents, hypotheses and bounded tests (ADR 0015).

A :class:`BoundedTest` has the shape of a Chaos Toolkit experiment (Apache-2.0 spec,
re-implemented): a **steady-state hypothesis** checked before and after, a **method**
that runs only if the steady state held, and **rollbacks** run by strategy (``always``,
``never``, ``deviated``). AIR adds an admission **budget** and runs every robot action
through ``Runtime.submit``, so a test can do nothing an ordinary command could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts.execution import ConditionResult
from ..contracts.skill import Condition
from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ14", "REQ11", "G11", "G13")


class IncidentState(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RECOVERED = "recovered"        # recovery verified from observations
    HANDED_OFF = "handed_off"      # a person owns it now
    CLOSED = "closed"

    @property
    def active(self) -> bool:
        return self in (IncidentState.OPEN, IncidentState.INVESTIGATING)


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"        # a bounded test's observations agreed
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Detection:
    """What a detector saw (a health item going bad, a diagnosis fault, a failed check)."""

    detector: str
    robot_id: str
    signal: str
    summary: str
    observed_at: float
    component_id: str | None = None
    failure_class: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    statement: str
    proposed_by: str
    #: False for agent or model proposals: they never grant authority or settle anything.
    authoritative: bool
    failure_class: str | None = None
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestAction:
    __test__ = False                     # not a pytest test class

    skill_id: str
    args: dict[str, Any] = field(default_factory=dict)
    robot_id: str | None = None          # default: the incident's robot
    timeout_s: float = 30.0


@dataclass(frozen=True)
class TestBudget:
    __test__ = False                     # not a pytest test class

    max_commands: int = 3                # method actions (rollbacks are always allowed)
    max_duration_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_commands < 1 or self.max_duration_s <= 0:
            raise SchemaError("budget needs max_commands >= 1 and max_duration_s > 0")


class RollbackStrategy(StrEnum):
    ALWAYS = "always"
    NEVER = "never"
    DEVIATED = "deviated"                # only if the steady state broke or the method failed


@contract("bounded_test", version="1.0")
class BoundedTest(Contract):
    test_id: str
    hypothesis_id: str
    title: str
    steady_state: tuple[Condition, ...]
    method: tuple[TestAction, ...]
    rollbacks: tuple[TestAction, ...] = ()
    rollback_strategy: RollbackStrategy = RollbackStrategy.DEVIATED
    budget: TestBudget = field(default_factory=TestBudget)
    #: If the hypothesis is true, the method breaks the steady state.
    predicts_deviation: bool = True
    max_input_age_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.steady_state:
            raise SchemaError("a bounded test needs a steady-state hypothesis",
                              test_id=self.test_id)
        if not self.method:
            raise SchemaError("a bounded test needs a method", test_id=self.test_id)


class TestOutcome(StrEnum):
    __test__ = False

    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"        # the method failed or was rejected
    NOT_RUN = "not_run"                  # steady state not met, or over budget
    ABORTED = "aborted"                  # physical outcome unknown: handed off


@dataclass(frozen=True)
class TestRun:
    __test__ = False                     # not a pytest test class

    test_id: str
    hypothesis_id: str
    outcome: TestOutcome
    reason: str = ""
    before: tuple[ConditionResult, ...] = ()
    after: tuple[ConditionResult, ...] = ()
    deviated: bool | None = None
    commands: tuple[str, ...] = ()       # "<phase>:<command_id>:<state>"
    started_at: float | None = None
    finished_at: float | None = None


@contract("incident", version="1.0")
class Incident(Contract):
    incident_id: str
    robot_id: str
    detected_at: float
    detector: str
    signal: str
    summary: str
    signature: str
    novel: bool
    known_classes: tuple[str, ...] = ()
    component_id: str | None = None
    state: IncidentState = IncidentState.OPEN
    occurrences: int = 1
    hypotheses: tuple[Hypothesis, ...] = ()
    tests: tuple[TestRun, ...] = ()
    recovery: tuple[str, ...] = ()       # "<command_id>:<state>" or "observed:<detail>"
    handed_off_to: str | None = None
    resolution: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    def hypothesis(self, hypothesis_id: str) -> Hypothesis:
        for h in self.hypotheses:
            if h.hypothesis_id == hypothesis_id:
                return h
        raise SchemaError("unknown hypothesis", incident=self.incident_id,
                          hypothesis=hypothesis_id)
