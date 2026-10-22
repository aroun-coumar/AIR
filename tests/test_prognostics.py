"""Prognostics: time-of-event distributions, censoring, gating on p5, clean-room estimator."""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import agentic_robotics.maintenance.prognostics as prognostics_module
from agentic_robotics.cli import main
from agentic_robotics.contracts import ProfileStatus
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.health import HealthGate, ReadinessContext, ReadinessGate, gated_checks
from agentic_robotics.maintenance import (
    Interval,
    LinearDegradationEstimator,
    PrognosticCheck,
    PrognosticEstimate,
    TimeOfEvent,
    estimates_from_ledger,
    prediction_error,
    prognostic_gater,
    record_estimate,
    threshold_crossings,
)
from agentic_robotics.provenance import Project

pytestmark = pytest.mark.req("REQ16", "G13")

DAY = 86400.0


def soh_series(days: int = 10, noise: float = 0.3, seed: int = 7) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    return [(d * DAY, 100.0 - d + rng.uniform(-noise, noise)) for d in range(days + 1)]


def estimate(series: list[tuple[float, float]] | None = None, **kw: Any) -> PrognosticEstimate:
    est = LinearDegradationEstimator(**{"threshold": 80.0, "horizon_s": 60 * DAY, **kw})
    return est.estimate(series or soh_series(), robot_id="carrier-1", component_id="pack",
                        signal="state_of_health")


def manual(p5: float, p50: float, p95: float, *, mean: float | None = None,
           censored: tuple[str, ...] = (), state: float = 0.5,
           horizon: float = 30 * DAY) -> PrognosticEstimate:
    return PrognosticEstimate(
        estimate_id="prog_x", robot_id="carrier-1", component_id="pack", event="soh < 80",
        signal="state_of_health", threshold=80.0, direction="below", estimated_at=0.0,
        event_state=Interval(state - 0.1, state, state + 0.1),
        time_of_event=TimeOfEvent({"p5": p5, "p50": p50, "p95": p95}, censored,
                                  0.5 if censored else 0.0, mean, 100),
        horizon_s=horizon)


def test_linear_degradation_gives_a_distribution_not_a_point() -> None:
    e = estimate()
    q = e.time_of_event.quantiles
    assert q["p5"] < q["p50"] < q["p95"]
    assert q["p50"] == pytest.approx(10 * DAY, rel=0.1)
    assert not e.censored and e.time_of_event.mean_s is not None
    assert e.event_state.low <= e.event_state.mean <= e.event_state.high
    assert e.event_state.mean == pytest.approx(0.5, abs=0.05)
    assert e.loading.kind == "observed_trend" and "slope_per_s" in e.loading.parameters
    assert e.inputs_n == 11 and e.method == "air.linear_bootstrap/1"
    assert PrognosticEstimate.from_json(e.to_json()) == e


def test_estimator_is_deterministic_for_a_seed() -> None:
    a, b = estimate(seed=3), estimate(seed=3)
    assert a.time_of_event == b.time_of_event and a.event_state == b.event_state
    assert estimate(seed=4).time_of_event.quantiles != a.time_of_event.quantiles


def test_no_trend_towards_the_threshold_is_censored() -> None:
    flat = [(d * DAY, 95.0 + (0.1 if d % 2 else -0.1)) for d in range(10)]
    e = estimate(flat, horizon_s=30 * DAY)
    assert e.time_of_event.censored_fraction > 0.3
    if "p50" in e.time_of_event.censored_quantiles:
        assert e.time_of_event.quantiles["p50"] == 30 * DAY
    assert e.time_of_event.mean_s is None
    rising = estimate([(d * DAY, 90.0 + d) for d in range(5)])
    assert rising.time_of_event.censored_quantiles == ("p5", "p50", "p95")
    assert rising.gate_status(10 * DAY)[0] is ProfileStatus.AVAILABLE
    status, why = rising.gate_status(90 * DAY)
    assert status is ProfileStatus.RESTRICTED and "horizon" in why


def test_gate_on_p5_not_the_mean() -> None:
    e = manual(p5=3600.0, p50=36000.0, p95=72000.0, mean=40000.0)
    status, why = e.gate_status(7200.0)                     # the mean would say "fine"
    assert status is ProfileStatus.RESTRICTED and "p5" in why
    assert e.gate_status(1800.0)[0] is ProfileStatus.AVAILABLE
    reached = manual(0.0, 0.0, 0.0, state=0.0)
    assert reached.gate_status(1.0)[0] is ProfileStatus.UNAVAILABLE
    already = estimate([(0, 82.0), (DAY, 81.0), (2 * DAY, 79.0)])
    assert already.reached() and already.gate_status(1.0)[0] is ProfileStatus.UNAVAILABLE


def test_rising_signal_direction_above() -> None:
    wear = [(d * DAY, 10.0 + 2 * d) for d in range(6)]
    e = LinearDegradationEstimator(threshold=40.0, direction="above", horizon_s=60 * DAY
                                   ).estimate(wear, robot_id="arm-1", component_id="gear",
                                              signal="backlash")
    assert e.time_of_event.quantiles["p50"] == pytest.approx(10 * DAY, rel=0.01)
    assert e.event == "backlash > 40"


def test_estimator_input_errors() -> None:
    with pytest.raises(SchemaError):
        estimate([(0, 1.0), (1, 0.5)])
    with pytest.raises(SchemaError):
        estimate([(0, 1.0), (0, 0.5), (0, 0.2)])
    with pytest.raises(SchemaError):
        LinearDegradationEstimator(threshold=1, n_boot=5)
    with pytest.raises(SchemaError):
        LinearDegradationEstimator(threshold=1, quantiles=(0.5,))
    with pytest.raises(SchemaError):
        LinearDegradationEstimator(threshold=1, direction="sideways")  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        manual(1, 2, 3).quantile("p99")


def test_prediction_error_after_service() -> None:
    e = manual(p5=5 * DAY, p50=10 * DAY, p95=15 * DAY)
    err = prediction_error(e, observed_event_at=12 * DAY)
    assert err["error_p50_s"] == -2 * DAY and err["within_p5_p95"] == 1.0
    assert prediction_error(e, observed_event_at=20 * DAY)["within_p5_p95"] == 0.0


def test_clean_room_no_progpy() -> None:
    source = Path(prognostics_module.__file__).read_text("utf-8")
    assert "import progpy" not in source and "from progpy" not in source
    assert "progpy" not in sys.modules


async def test_prognostic_gate_restricts_then_blocks(make_env: Callable[..., Any]) -> None:
    env = make_env()
    soon = manual(p5=600.0, p50=3600.0, p95=7200.0)
    gate = HealthGate(trace=env.runtime.trace)
    gate.attach("carrier-1", prognostic_gater([soon], {"mobility": ["pack"]}, mission_s=3600))
    env.runtime.checks = gated_checks(gate)
    gated = gate.report(env.robot.capability_report(), env.runtime.clock.now())
    mob = gated.get("mobility")
    assert mob is not None and mob.status is ProfileStatus.RESTRICTED
    assert mob.evidence[-1] == "prognostic:prog_x"
    assert (await env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0})).wait(5)
            ).verified                                     # restricted still admits
    gate2 = HealthGate()
    gate2.attach("carrier-1", prognostic_gater([manual(0, 0, 0, state=0.0)],
                                               {"mobility": ["pack"]}, mission_s=3600))
    env.runtime.checks = gated_checks(gate2)
    res = await env.runtime.submit(env.command("navigate_to", {"x": 0.0, "y": 0.0})).wait(5)
    assert res.reason == "capability_unavailable"


def test_prognostic_readiness_check() -> None:
    gate = ReadinessGate([PrognosticCheck([manual(600.0, 3600.0, 7200.0)], mission_s=3600,
                                          modes=("long_mission",))],
                         modes=("short", "long_mission"))
    r = gate.evaluate(ReadinessContext("carrier-1", 0.0))
    assert r.can_run == {"short": True, "long_mission": False}
    dead = gate.__class__([PrognosticCheck([manual(0, 0, 0, state=0.0)], mission_s=1)],
                          modes=("short",)).evaluate(ReadinessContext("carrier-1", 0.0))
    assert not dead.ready("short") and dead.failsafe == {"prognostic:pack": True}


def test_threshold_crossings_and_ledger(tmp_path: Path) -> None:
    project = Project.init(tmp_path, name="t")
    near, far = manual(DAY, 2 * DAY, 3 * DAY), manual(20 * DAY, 25 * DAY, 30 * DAY)
    censored = manual(30 * DAY, 30 * DAY, 30 * DAY, censored=("p5", "p50", "p95"))
    assert threshold_crossings([near, far, censored], lead_s=7 * DAY) == [near]
    e = estimate()
    record_estimate(project, e)
    record_estimate(project, e.evolve(estimate_id="prog_newer"))
    [latest] = estimates_from_ledger(project)
    assert latest.estimate_id == "prog_newer"


def test_prognose_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    Project.init(tmp_path, name="t")
    csv_path = tmp_path / "soh.csv"
    csv_path.write_text("t,soh\n" + "".join(f"{t},{x}\n" for t, x in soh_series()))
    args = ["prognose", "estimate", "--series", str(csv_path), "--threshold", "80",
            "--robot", "carrier-1", "--component", "pack", "--signal", "state_of_health",
            "--horizon", "60d", "--project", str(tmp_path)]
    assert main([*args, "--mission", "1d", "--record"]) == 0
    out = capsys.readouterr().out
    assert "p5" in out and "available" in out
    assert main([*args, "--mission", "20d"]) == 1
    assert "restricted" in capsys.readouterr().out
    json_path = tmp_path / "soh.json"
    json_path.write_text(json.dumps(soh_series()))
    assert main(["prognose", "estimate", "--series", str(json_path), "--threshold", "80",
                 "--robot", "carrier-1", "--component", "pack", "--signal", "soh",
                 "--json"]) == 0
    assert '"ar.prognostic_estimate/1.0"' in capsys.readouterr().out
    assert main(["prognose", "list", "--project", str(tmp_path), "--mission", "1d",
                 "--triage", "--lead", "30d"]) == 0
    assert "carrier-1/pack" in capsys.readouterr().out
    opened = Project.open(tmp_path).ledger.query(kind="triage.opened")
    assert [e.data["item"]["kind"] for e in opened] == ["prognostic.threshold"]
