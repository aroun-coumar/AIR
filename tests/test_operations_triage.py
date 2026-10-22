"""Operations collector: health, readiness, incidents and prognostics become triage items."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_robotics.health import (
    CheckResult,
    ExpectedItem,
    HealthAggregator,
    HealthLevel,
    HealthManifest,
    ReadinessContext,
    ReadinessGate,
)
from agentic_robotics.maintenance import Interval, PrognosticEstimate, TimeOfEvent
from agentic_robotics.provenance import Project
from agentic_robotics.triage import Priority, Severity, TriageBook, TriagePolicy, TriageState
from agentic_robotics.triage.operations import (
    OPERATIONS_RULES,
    OperationsTriage,
    health_signals,
    operations_policy,
)
from agentic_robotics.triage.policy import TriageRule

pytestmark = pytest.mark.req("REQ14", "REQ15", "G11")


@pytest.fixture()
def book(tmp_path: Path) -> TriageBook:
    return TriageBook(Project.init(tmp_path, name="t"), policy=operations_policy(),
                      now=lambda: 1000.0)


def manifest() -> HealthManifest:
    return HealthManifest(robot_id="amr-1", expected=(
        ExpectedItem("pack.cell_voltage_min", "amr-1/pack/cell_voltage_min",
                     component_id="pack", critical_below=3.0, timeout_s=1.0, critical=True),
        ExpectedItem("sbc.cpu_load", "amr-1/sbc/cpu_load", component_id="sbc",
                     critical_above=95.0, timeout_s=1.0)))


def test_health_errors_and_stale_items_route_by_criticality(book: TriageBook) -> None:
    agg = HealthAggregator(manifest())
    agg.observe("pack.cell_voltage_min", 2.8, at=10.0)
    agg.observe("sbc.cpu_load", 99.0, at=10.0)
    sink = OperationsTriage(book)
    report = agg.report(10.5)
    assert {r.action for r in sink.health(report)} == {"opened"}
    by_rule = {i.rule: i for i in book.items()}
    assert by_rule["health-critical"].severity is Severity.SAFETY
    assert by_rule["health-critical"].priority is Priority.P1
    assert by_rule["health-error"].severity is Severity.MISSION
    assert {r.action for r in sink.health(report)} == {"unchanged"}     # idempotent

    agg.observe("sbc.cpu_load", 20.0, at=11.0)
    sink.health(agg.report(11.2))
    states = {i.rule: i.state for i in book.items(include_closed=True)}
    assert states["health-error"] is TriageState.RESOLVED              # condition cleared
    assert states["health-critical"].open                              # safety: needs a person

    stale = agg.report(30.0)
    assert {s.kind for s in health_signals(stale)} == {"health.stale"}
    sink.health(stale)
    assert any(i.rule == "health-stale" for i in book.items())


def test_readiness_blocks_and_failsafes(book: TriageBook) -> None:
    class Low:
        check_id, order = "battery", 0

        def run(self, ctx: ReadinessContext) -> list[CheckResult]:
            return [CheckResult("battery", HealthLevel.ERROR, "critical", subject="pack",
                                blocks=("*",), failsafe=("battery_critical",)),
                    CheckResult("battery", HealthLevel.WARN, "aging", subject="cells")]

    report = ReadinessGate([Low()], modes=("manual",)).evaluate(ReadinessContext("amr-1", 5.0))
    OperationsTriage(book).readiness(report)
    rules = sorted(i.rule for i in book.items())
    assert rules == ["readiness-blocked", "readiness-failsafe"]        # the warning blocks nothing
    assert next(i for i in book.items() if i.rule == "readiness-failsafe").severity \
        is Severity.SAFETY


def test_prognostic_threshold_crossings(book: TriageBook) -> None:
    def est(p5: float, state: float = 0.5) -> PrognosticEstimate:
        return PrognosticEstimate(
            estimate_id=f"prog_{p5:g}_{state:g}", robot_id="amr-1", component_id="pack",
            event="soh < 80", signal="soh", threshold=80.0, direction="below",
            estimated_at=0.0, event_state=Interval(state, state, state),
            time_of_event=TimeOfEvent({"p5": p5, "p50": p5 * 2, "p95": p5 * 3}),
            horizon_s=1e7)

    sink = OperationsTriage(book)
    sink.prognostics([est(3 * 86400.0), est(90 * 86400.0)], lead_s=7 * 86400.0,
                     robot_id="amr-1")
    [item] = book.items()
    assert item.rule == "prognostic-threshold" and item.severity is Severity.QUALITY
    sink.prognostics([est(0.0, state=0.0)], lead_s=7 * 86400.0)
    assert {i.rule for i in book.items()} == {"prognostic-threshold", "prognostic-reached"}


def test_operations_policy_keeps_project_rules() -> None:
    mine = TriageRule("health-error", Severity.QUALITY, Priority.P4, kinds=("health.error",))
    policy = operations_policy(TriagePolicy(rules=(mine,)))
    assert [r for r in policy.rules if r.name == "health-error"] == [mine]
    assert len(policy.rules) == len(OPERATIONS_RULES)
