"""Health: expected items, STALE, rollup, monitors, PSI, gating, readiness, diagnosis."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts import (
    AdapterRef,
    CapabilityProfile,
    CapabilityReport,
    ComponentManifest,
    ComponentSpec,
    ProfileStatus,
)
from agentic_robotics.contracts.observation import Observation
from agentic_robotics.core.clock import ManualClock
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.hardware import builtin_catalog
from agentic_robotics.hardware.domains import builtin_domains
from agentic_robotics.health import (
    ALL_MODES,
    CapabilityCheck,
    CapabilityGate,
    CheckResult,
    Diagnoser,
    DiagnosisCheck,
    DiagnosisEntry,
    DiagnosisTable,
    DiagSeverity,
    ExpectedItem,
    FaultPhase,
    FrequencyMonitor,
    FreshnessCheck,
    HealthAggregator,
    HealthCheck,
    HealthGate,
    HealthLevel,
    HealthManifest,
    HealthReport,
    HealthStatus,
    LatencyMonitor,
    ManifestCheck,
    NotReady,
    PsiProbe,
    ReadinessContext,
    ReadinessGate,
    ReadinessReport,
    builtin_table,
    derive_manifest,
    exercise,
    gate_capability,
    gated_checks,
)
from agentic_robotics.provenance import Project

pytestmark = pytest.mark.req("REQ13", "REQ14")
gate_g10 = pytest.mark.req("G10", "G11")


def robot_manifest() -> ComponentManifest:
    return ComponentManifest(
        robot_id="amr-1", name="test amr", hardware_revision="r1",
        adapter=AdapterRef(kind="fake"), nominal_capabilities=("mobility", "compute"),
        components=(
            ComponentSpec("base", "chassis", "1"),
            ComponentSpec("pack", "battery", "1", parent="base",
                          type_id="power.battery.li_ion_nmc_pack"),
            ComponentSpec("sbc", "computer", "1", parent="base", type_id="compute.sbc"),
        ))


@pytest.fixture()
def hm() -> HealthManifest:
    return derive_manifest(robot_manifest(), builtin_catalog(), timeout_s=2.0)


# -- derived manifest ---------------------------------------------------------------------
def test_expected_items_derive_from_catalog_telemetry_limits(hm: HealthManifest) -> None:
    temp = hm.item("pack.pack_temperature")
    assert temp is not None
    assert temp.path == "amr-1/base/pack/pack_temperature"
    assert (temp.warn_above, temp.critical_above, temp.unit) == (45.0, 60.0, "degC")
    assert temp.critical, "safety-relevant battery items take the fast path"
    assert temp.limit_source == "catalog:power.battery.li_ion_nmc_pack"
    cpu = hm.item("sbc.cpu_temperature")
    assert cpu is not None and not cpu.critical and cpu.timeout_s == 2.0
    gates = {(g.capability, g.match) for g in hm.gates}
    assert ("compute", ("sbc.*",)) in gates            # compute.sbc provides compute
    assert ("*", ("pack.*",)) in gates                 # safety-relevant: every capability
    assert HealthManifest.from_json(hm.to_json()) == hm


def test_component_limit_override_wins(hm: HealthManifest) -> None:
    from agentic_robotics.contracts import LimitOverride
    m = robot_manifest()
    pack = m.component("pack")
    assert pack is not None
    m = m.evolve(components=tuple(
        c.__class__(**{**c.__dict__, "limits": (LimitOverride(
            "pack_temperature", warn_above=40.0, reason="datasheet rev C"),)})
        if c.component_id == "pack" else c for c in m.components))
    item = derive_manifest(m, builtin_catalog()).item("pack.pack_temperature")
    assert item is not None and item.warn_above == 40.0
    assert item.limit_source.startswith("manifest:pack")


# -- aggregation --------------------------------------------------------------------------
def test_missing_is_error_and_silence_is_stale_never_ok(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    r = agg.report(100.0)
    assert r.item("pack.pack_temperature").level is HealthLevel.ERROR  # type: ignore[union-attr]
    assert "missing" in r.item("pack.pack_temperature").message  # type: ignore[union-attr]
    for item in hm.expected:
        agg.observe(item.name, 30.0 if item.unit != "V" else 3.7, at=100.0)
    agg.observe("pack.state_of_charge", 80.0, at=100.0)
    agg.observe("pack.state_of_health", 95.0, at=100.0)
    agg.observe("sbc.storage_free", 60.0, at=100.0)
    agg.observe("pack.cell_voltage_delta", 10.0, at=100.0)
    fresh = agg.report(101.0)
    assert fresh.level is HealthLevel.OK, fresh.render()
    stale = agg.report(103.0)                          # 3 s > 2 s timeout
    assert stale.level is HealthLevel.STALE
    assert all(s.level is HealthLevel.STALE for s in stale.items)
    assert stale.level is not HealthLevel.OK


def test_worst_of_rollup_follows_the_component_tree(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    for item in hm.expected:
        agg.report_status(HealthStatus(item.name, HealthLevel.OK, observed_at=10.0))
    agg.observe("sbc.cpu_temperature", 85.0, at=10.0, unit="degC")      # warn
    r = agg.report(10.5)
    assert r.group("amr-1/base/sbc").level is HealthLevel.WARN  # type: ignore[union-attr]
    assert r.group("amr-1/base/pack").level is HealthLevel.OK  # type: ignore[union-attr]
    assert r.group("amr-1/base").level is HealthLevel.WARN  # type: ignore[union-attr]
    assert r.level is HealthLevel.WARN
    agg.observe("pack.cell_voltage_min", 2400, at=10.5, unit="mV")      # 2.4 V: critical
    r = agg.report(10.6)
    assert r.item("pack.cell_voltage_min").level is HealthLevel.ERROR  # type: ignore[union-attr]
    assert r.item("pack.cell_voltage_min").values == {"value": 2.4}  # type: ignore[union-attr]
    assert r.group("amr-1").level is HealthLevel.ERROR  # type: ignore[union-attr]
    assert r.critical == ("pack.cell_voltage_min",)
    assert "critical" in r.render()
    assert HealthReport.from_json(r.to_json()) == r


def test_unit_mismatch_is_an_error_not_a_guess(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    s = agg.observe("pack.pack_temperature", 4.0, at=1.0, unit="V")
    assert s.level is HealthLevel.ERROR and "cannot compare" in s.message
    s = agg.observe("pack.pack_temperature", 300.0, at=1.0, unit="K")   # not linear: refuse
    assert s.level is HealthLevel.ERROR


def test_unexpected_reporters_go_to_other(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    agg.report_status(HealthStatus("lidar.dropouts", HealthLevel.ERROR, "dropouts",
                                   observed_at=5.0))
    r = agg.report(5.5)
    assert r.item("lidar.dropouts") in r.other
    assert r.group("amr-1/other").level is HealthLevel.ERROR  # type: ignore[union-attr]
    later = agg.report(50.0)
    assert later.item("lidar.dropouts").level is HealthLevel.STALE  # type: ignore[union-attr]
    # an unexpected reporter going quiet is listed, but does not roll up
    assert later.group("amr-1/other").level is HealthLevel.OK  # type: ignore[union-attr]


def test_critical_fast_path_publishes_immediately_once(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    seen: list[HealthStatus] = []
    agg.on_critical(seen.append)
    agg.observe("sbc.cpu_temperature", 99.0, at=1.0)           # not critical: no fast path
    assert seen == []
    agg.observe("pack.pack_temperature", 70.0, at=1.0)
    agg.observe("pack.pack_temperature", 71.0, at=1.1)         # still bad: not re-published
    assert [s.name for s in seen] == ["pack.pack_temperature"]
    agg.observe("pack.pack_temperature", 30.0, at=1.2)         # cleared
    stale = {s.name: s.level for s in agg.check_stale(5.0)}   # silence: published again
    assert stale["pack.pack_temperature"] is HealthLevel.STALE
    assert stale["pack.cell_voltage_min"] is HealthLevel.ERROR  # never reported: missing
    assert [s.name for s in seen].count("pack.pack_temperature") == 2


def test_transport_loss_makes_items_stale_until_they_report(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    for item in hm.for_component("sbc"):
        agg.observe(item.name, 10.0, at=1.0)
    lost = agg.lost("sbc", at=1.2, reason="liveliness token lost")
    assert set(lost) == {i.name for i in hm.for_component("sbc")}
    r = agg.report(1.3)
    assert r.item("sbc.cpu_load").level is HealthLevel.STALE  # type: ignore[union-attr]
    assert "liveliness" in r.item("sbc.cpu_load").message  # type: ignore[union-attr]
    agg.observe("sbc.cpu_load", 10.0, at=1.4)
    assert agg.report(1.5).item("sbc.cpu_load").level is HealthLevel.OK  # type: ignore[union-attr]


def test_observation_input_uses_acquisition_time(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    obs = Observation(source="amr-1/bms", signal="pack.pack_temperature", clock_domain="sim",
                      acquired_at=1.0, received_at=1.5, value=50.0, unit="degC")
    assert agg.observe_observation(obs).level is HealthLevel.WARN
    with pytest.raises(SchemaError):
        agg.observe_observation(obs.evolve(value="hot"))


# -- monitors and probes ----------------------------------------------------------------
def test_frequency_monitor_window_and_tolerance() -> None:
    mon = FrequencyMonitor("loop", min_hz=45, max_hz=55, tolerance=0.1, window_s=1.0,
                           start=0.0)
    assert mon.status(0.5).level is HealthLevel.ERROR          # no events
    for i in range(50):
        mon.tick(i * 0.02)
    assert mon.status(1.0).level is HealthLevel.OK
    slow = FrequencyMonitor("loop", min_hz=45, max_hz=55, window_s=1.0, start=0.0)
    for i in range(30):
        slow.tick(i / 30)
    assert slow.status(1.0).level is HealthLevel.WARN and "low" in slow.status(1.0).message
    fast = FrequencyMonitor("loop", min_hz=45, max_hz=55, window_s=1.0, start=0.0)
    for i in range(80):
        fast.tick(i / 80)
    assert "high" in fast.status(1.0).message
    assert mon.status(5.0).level is HealthLevel.ERROR          # events aged out
    with pytest.raises(SchemaError):
        FrequencyMonitor("x", min_hz=10, max_hz=5)


def test_latency_monitor() -> None:
    mon = LatencyMonitor("cam", warn_s=0.05, error_s=0.2, window=20)
    assert mon.status(0).level is HealthLevel.WARN             # no samples
    for i in range(20):
        mon.record(i, i + 0.01)
    assert mon.status(20).level is HealthLevel.OK
    for i in range(5):
        mon.record(i, i + 0.1)
    assert mon.status(20).level is HealthLevel.WARN
    mon.record(0, 0.5)
    assert mon.status(20).level is HealthLevel.ERROR
    ok = LatencyMonitor("cam", warn_s=0.05, error_s=0.2)
    ok.record(10.0, 9.0)                                       # stamped in the future
    assert "future" in ok.status(10).message


def write_psi(root: Path, cpu: float, mem_full: float) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cpu").write_text(f"some avg10={cpu:.2f} avg60=1.00 avg300=0.50 total=1234\n"
                              "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    (root / "memory").write_text("some avg10=0.10 avg60=0.10 avg300=0.10 total=10\n"
                                 f"full avg10={mem_full:.2f} avg60=0.00 avg300=0.00 total=5\n")
    (root / "io").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                             "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")


@gate_g10
def test_psi_probe_reads_pressure_files(tmp_path: Path) -> None:
    write_psi(tmp_path / "pressure", cpu=25.0, mem_full=30.0)
    probe = PsiProbe(tmp_path / "pressure")
    assert probe.supported
    by = {s.name: s for s in probe.statuses(1.0)}
    assert by["host.psi.cpu"].level is HealthLevel.WARN
    assert by["host.psi.cpu"].values["some_avg10"] == 25.0
    assert by["host.psi.memory"].level is HealthLevel.ERROR
    assert by["host.psi.io"].level is HealthLevel.OK


@gate_g10
def test_psi_unsupported_is_reported_stale(tmp_path: Path) -> None:
    probe = PsiProbe(tmp_path / "absent")
    assert not probe.supported
    [s] = probe.statuses(1.0)
    assert s.level is HealthLevel.STALE and "unsupported" in s.message


def test_psi_on_this_host() -> None:
    probe = PsiProbe()
    if not probe.supported:
        pytest.skip("no /proc/pressure on this host")
    assert {s.name for s in probe.statuses(0.0)} <= {"host.psi.cpu", "host.psi.memory",
                                                     "host.psi.io"}


# -- report vs gate -------------------------------------------------------------------------
def cap_report(*profiles: tuple[str, ProfileStatus], rev: int = 3) -> CapabilityReport:
    return CapabilityReport(robot_id="amr-1", capability_revision=rev, observed_at=0.0,
                            clock_domain="sim",
                            profiles=tuple(CapabilityProfile(n, s) for n, s in profiles))


@gate_g10
def test_gates_only_downgrade(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    for item in hm.expected:
        agg.report_status(HealthStatus(item.name, HealthLevel.OK, observed_at=0.0))
    base = cap_report(("mobility", ProfileStatus.AVAILABLE), ("compute", ProfileStatus.AVAILABLE))
    assert gate_capability(base, agg.report(0.1), hm) == base
    agg.observe("pack.state_of_charge", 15.0, at=0.2)                  # warn
    gated = gate_capability(base, agg.report(0.3), hm)
    assert gated.get("mobility").status is ProfileStatus.RESTRICTED  # type: ignore[union-attr]
    assert "health:pack.state_of_charge:warn" in gated.get("mobility").limiting_factors  # type: ignore[union-attr]
    agg.observe("sbc.cpu_temperature", 99.0, at=0.2)                   # critical on compute
    gated = gate_capability(base, agg.report(0.3), hm)
    assert gated.get("compute").status is ProfileStatus.UNAVAILABLE  # type: ignore[union-attr]
    down = cap_report(("mobility", ProfileStatus.UNAVAILABLE))
    assert gate_capability(down, HealthReport(robot_id="amr-1", observed_at=0,
                                              level=HealthLevel.OK), hm) == down


def health_env(make_env: Callable[..., Any]
               ) -> tuple[Any, HealthAggregator, HealthGate, ManualClock]:
    clock = ManualClock(start=10.0)
    env = make_env(clock=clock)
    hm = HealthManifest(robot_id="carrier-1", expected=(
        ExpectedItem("battery.cell_voltage_min", "carrier-1/battery/cell_voltage_min",
                     component_id="battery", unit="V", warn_below=3.2, critical_below=3.0,
                     timeout_s=1.0, critical=True),
        ExpectedItem("imu.fallen", "carrier-1/imu/fallen", component_id="imu",
                     critical_above=0.5, timeout_s=1.0)),
        gates=(CapabilityGate("mobility", ("battery.*",)),
               CapabilityGate("mobility", ("imu.fallen",),
                              on_error=ProfileStatus.RESTRICTED, reason="fallen")),
        robot_owned_recovery=("undock",))
    agg = HealthAggregator(hm)
    gate = HealthGate(trace=env.runtime.trace)
    gate.attach_health(hm, agg)
    env.runtime.checks = gated_checks(gate, recovery=[hm])
    return env, agg, gate, clock


@gate_g10
async def test_health_gates_the_report_the_runtime_admits_against(
        make_env: Callable[..., Any]) -> None:
    env, agg, _, clock = health_env(make_env)
    agg.observe("battery.cell_voltage_min", 3.7, at=clock.now())
    agg.observe("imu.fallen", 0.0, at=clock.now())
    ok = await env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0})).wait(5)
    assert ok.verified

    agg.observe("battery.cell_voltage_min", 2.9, at=clock.now())
    h = env.runtime.submit(env.command("navigate_to", {"x": 0.0, "y": 0.0}))
    res = await h.wait(1)
    assert res.state.value == "rejected" and res.reason == "capability_unavailable"
    assert "health:battery.cell_voltage_min:error" in h.rejection.details["limiting_factors"]
    assert "capability_gated" in env.runtime.trace.kinds()

    # silence is not health: once the report times out, the gate still holds
    agg.observe("battery.cell_voltage_min", 3.7, at=clock.now())
    clock.advance(2.0)
    res = await env.runtime.submit(env.command("navigate_to", {"x": 0.0, "y": 0.0})).wait(1)
    assert res.reason == "capability_unavailable"


@gate_g10
async def test_fall_reports_restricted_and_robot_owned_recovery_is_not_commanded(
        make_env: Callable[..., Any]) -> None:
    env, agg, gate, clock = health_env(make_env)
    agg.observe("battery.cell_voltage_min", 3.7, at=clock.now())
    agg.observe("imu.fallen", 1.0, at=clock.now())
    gated = gate.report(env.robot.capability_report(), clock.now())
    mob = gated.get("mobility")
    assert mob is not None and mob.status is ProfileStatus.RESTRICTED
    assert "health:imu.fallen:error" in mob.limiting_factors

    h = env.runtime.submit(env.command("undock"))
    res = await h.wait(1)
    assert res.reason == "robot_owned_recovery"
    assert env.adapter.executed == []                         # nothing reached the robot


@gate_g10
async def test_gated_revision_changes_with_the_gating_outcome(
        make_env: Callable[..., Any]) -> None:
    env, agg, gate, clock = health_env(make_env)
    agg.observe("battery.cell_voltage_min", 3.7, at=clock.now())
    agg.observe("imu.fallen", 0.0, at=clock.now())
    r1 = gate.report(env.robot.capability_report(), clock.now()).capability_revision
    agg.observe("imu.fallen", 1.0, at=clock.now())
    r2 = gate.report(env.robot.capability_report(), clock.now()).capability_revision
    assert r2 == r1 + 1
    res = await env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0},
                                               capability_revision=r1)).wait(1)
    assert res.reason == "capability_revision_changed"


def test_gated_checks_place_the_gate_before_the_capability_check() -> None:
    from agentic_robotics.runtime.admission import DEFAULT_CHECKS, check_capability
    gate = HealthGate()
    checks = gated_checks(gate)
    assert checks.index(gate) == checks.index(check_capability) - 1
    assert len(checks) == len(DEFAULT_CHECKS) + 1
    assert gated_checks(gate, base=())[-1] is gate


# -- readiness ------------------------------------------------------------------------------
class Probe:
    def __init__(self, check_id: str, order: int, results: list[CheckResult] | None = None,
                 crash: bool = False, log: list[str] | None = None) -> None:
        self.check_id, self.order, self.results = check_id, order, results or []
        self.crash, self.log = crash, log if log is not None else []

    def run(self, ctx: ReadinessContext) -> list[CheckResult]:
        self.log.append(self.check_id)
        if self.crash:
            raise RuntimeError("sensor driver missing")
        return self.results


def test_readiness_runs_checks_in_a_defined_order_and_flags_per_mode() -> None:
    log: list[str] = []
    gate = ReadinessGate([
        Probe("z", 5, log=log),
        Probe("battery", 1, [CheckResult("battery", HealthLevel.WARN, "low", blocks=("auto",),
                                         failsafe=("battery_low",))], log=log),
        Probe("a", 5, log=log)], modes=("manual", "auto"))
    r = gate.evaluate(ReadinessContext("amr-1", 1.0))
    assert log == ["battery", "a", "z"] and r.checks_run == ("battery", "a", "z")
    assert r.can_run == {"manual": True, "auto": False}
    assert r.failsafe == {"battery_low": True} and r.level is HealthLevel.WARN
    assert [x.check_id for x in r.blocking("auto")] == ["battery"]
    assert ReadinessReport.from_json(r.to_json()) == r


def test_a_crashing_check_fails_closed() -> None:
    r = ReadinessGate([Probe("driver", 0, crash=True)], modes=("manual",)).evaluate(
        ReadinessContext("amr-1", 0.0))
    assert not r.ready("manual") and "crashed" in r.results[0].message
    assert r.results[0].blocks == (ALL_MODES,)


def test_require_raises_before_activation_and_records(tmp_path: Path,
                                                       hm: HealthManifest) -> None:
    project = Project.init(tmp_path, name="t")
    agg = HealthAggregator(hm)
    gate = ReadinessGate([HealthCheck()], modes=("manual", "autonomous"))
    with pytest.raises(NotReady) as exc:
        gate.require("autonomous", ReadinessContext("amr-1", 1.0, health=agg.report(1.0)),
                     project=project)
    assert any("missing" in b for b in exc.value.details["blocking"])
    [entry] = project.ledger.query(kind="readiness.evaluated")
    assert entry.data["ready"] is False
    for item in hm.expected:
        agg.report_status(HealthStatus(item.name, HealthLevel.OK, observed_at=2.0))
    agg.observe("sbc.cpu_load", 90.0, at=2.0)                          # warn only
    report = gate.require("autonomous", ReadinessContext("amr-1", 2.5, health=agg.report(2.5)))
    assert report.ready("autonomous") and report.level is HealthLevel.WARN
    with pytest.raises(NotReady):
        gate.require("flying", ReadinessContext("amr-1", 2.5))


def test_health_check_raises_failsafe_for_critical_items(hm: HealthManifest) -> None:
    agg = HealthAggregator(hm)
    for item in hm.expected:
        agg.report_status(HealthStatus(item.name, HealthLevel.OK, observed_at=0.0))
    agg.observe("pack.pack_temperature", 65.0, at=0.0)
    r = ReadinessGate([HealthCheck(warn_blocks=("autonomous",))],
                      modes=("manual", "autonomous")).evaluate(
        ReadinessContext("amr-1", 0.5, health=agg.report(0.5)))
    assert r.failsafe == {"health:pack.pack_temperature": True}
    assert not r.ready("manual")
    assert not ReadinessGate([HealthCheck()], modes=("m",)).evaluate(
        ReadinessContext("amr-1", 0.0)).ready("m")


def test_capability_freshness_and_manifest_checks() -> None:
    now = 10.0
    cap = CapabilityReport(robot_id="amr-1", capability_revision=1, observed_at=9.5,
                           clock_domain="sim", profiles=(
                               CapabilityProfile("mobility", ProfileStatus.RESTRICTED,
                                                 limiting_factors=("battery_low",)),
                               CapabilityProfile("imaging", ProfileStatus.UNAVAILABLE)))
    obs = {"base.pose": Observation(source="s", signal="base.pose", clock_domain="sim",
                                    acquired_at=8.0, received_at=8.0, value={})}
    bad = robot_manifest().evolve(components=(
        ComponentSpec("x", "thing", "1", type_id="sensor.does_not_exist"),))
    gate = ReadinessGate([
        CapabilityCheck({"drive": ("mobility",), "inspect": ("imaging",)}),
        CapabilityCheck({"precise": ("mobility",)}, check_id="strict", allow_restricted=False),
        FreshnessCheck({"drive": ("base.pose",), "inspect": ("camera.frame",)}),
        ManifestCheck(robot_manifest(), builtin_catalog())],
        modes=("drive", "inspect", "precise"))
    r = gate.evaluate(ReadinessContext("amr-1", now, capability=cap, observations=obs))
    assert r.can_run == {"drive": False, "inspect": False, "precise": False}
    msgs = {(x.check_id, x.subject): x for x in r.results}
    assert msgs[("freshness", "base.pose")].level is HealthLevel.STALE
    assert ("strict", "mobility") in msgs and ("capability", "imaging") in msgs
    assert ("capability", "mobility") not in msgs                    # restricted is allowed
    errs = list(ManifestCheck(bad, builtin_catalog()).run(ReadinessContext("amr-1", now)))
    assert errs and errs[0].blocks == (ALL_MODES,)
    old = list(CapabilityCheck({"drive": ("mobility",)}, max_age_s=0.1).run(
        ReadinessContext("amr-1", now, capability=cap)))
    assert old[0].level is HealthLevel.STALE


# -- diagnosis table ------------------------------------------------------------------------
@pytest.mark.parametrize("entry", builtin_table().entries, ids=lambda e: e.fault_id)
def test_every_diagnosis_entry_behaves_as_declared(entry: DiagnosisEntry) -> None:
    assert exercise(entry) == []


def test_diagnosis_failure_classes_exist_in_the_domain_packs() -> None:
    known = [f.failure_id for d in builtin_domains().domains for f in d.failure_taxonomy]
    assert builtin_table().problems(known) == []
    broken = DiagnosisTable(entries=(DiagnosisEntry("x", "x", failure_class="D99.nothing"),))
    assert broken.problems(known)


def test_debounce_hysteresis_and_delay() -> None:
    diag = Diagnoser(DiagnosisTable(entries=(
        DiagnosisEntry("hot", "too hot", threshold=3, severity=DiagSeverity.ERROR,
                       delay_s=2.0),)))
    assert diag.report("hot", ok=False, at=0).phase is FaultPhase.DEBOUNCING
    assert diag.report("hot", ok=True, at=1).phase is FaultPhase.OK    # flap: back to zero
    for t in (2, 3, 4):
        diag.report("hot", ok=False, at=t)
    assert diag.state("hot").phase is FaultPhase.PENDING
    assert diag.statuses(5)[0].level is HealthLevel.WARN               # pending: warn
    assert [s.fault_id for s in diag.poll(6)] == ["hot"]
    assert diag.statuses(6)[0].level is HealthLevel.ERROR
    diag.report("hot", ok=True, at=7)
    assert diag.state("hot").phase is FaultPhase.ACTIVE                # needs 3 passes
    with pytest.raises(SchemaError):
        diag.report("cold", ok=False, at=0)
    with pytest.raises(SchemaError):
        DiagnosisEntry("bad", "bad", threshold=0)
    with pytest.raises(SchemaError):
        DiagnosisTable(entries=(DiagnosisEntry("a", "a"), DiagnosisEntry("a", "b")))


def test_diagnosis_check_blocks_and_fatal_raises_failsafe() -> None:
    diag = Diagnoser(builtin_table())
    diag.report("safety_channel_discrepancy", ok=False, at=0)
    for _ in range(5):
        diag.report("rate_low", ok=False, at=0)
    r = ReadinessGate([DiagnosisCheck(diag)], modes=("manual",)).evaluate(
        ReadinessContext("amr-1", 2.0))
    assert not r.ready("manual")
    assert r.failsafe == {"safety_channel_discrepancy": True}
    assert {x.subject: x.level for x in r.results}["rate_low"] is HealthLevel.WARN


def test_diagnosis_table_round_trips() -> None:
    t = builtin_table()
    assert DiagnosisTable.from_json(t.to_json()) == t


# -- CLI ------------------------------------------------------------------------------------
def test_cli_derive_check_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    Project.init(tmp_path, name="t")
    cm = tmp_path / "robot.json"
    robot_manifest().save(cm)
    out = tmp_path / "health.json"
    assert main(["health", "derive", str(cm), "--project", str(tmp_path), "--timeout", "2",
                 "--recovery", "self_right", "--out", str(out)]) == 0
    hm = HealthManifest.load(out)
    assert hm.robot_owned_recovery == ("self_right",)
    readings = {i.name: {"value": 3.7 if i.unit == "V" else 30.0, "at": 100.0}
                for i in hm.expected}
    readings.update({"pack.state_of_charge": 80.0, "pack.state_of_health": 95.0,
                     "sbc.storage_free": 60.0, "pack.cell_voltage_delta": 10.0})
    rfile = tmp_path / "readings.json"
    rfile.write_text(json.dumps(readings))
    args = ["--readings", str(rfile), "--project", str(tmp_path), "--now", "100.5"]
    assert main(["health", "check", str(out), *args]) == 0
    readings["pack.pack_temperature"] = {"value": 70.0, "at": 100.0}
    rfile.write_text(json.dumps(readings))
    capsys.readouterr()
    assert main(["health", "check", str(cm), *args, "--record", "--triage"]) == 1
    assert "pack_temperature: error" in capsys.readouterr().out
    project = Project.open(tmp_path)
    assert project.ledger.query(kind="health.report")
    assert any(e.data["item"]["kind"] == "health.error"
               for e in project.ledger.query(kind="triage.opened"))
    assert main(["health", "ready", str(cm), *args, "--mode", "manual", "--record"]) == 1
    assert main(["health", "check", str(out), "--project", str(tmp_path), "--now",
                 "200", "--readings", str(rfile)]) == 1               # stale


def test_cli_psi_and_diagnosis(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_psi(tmp_path / "p", cpu=1.0, mem_full=0.0)
    assert main(["health", "psi", "--root", str(tmp_path / "p"), "--json"]) == 0
    assert "host.psi.cpu" in capsys.readouterr().out
    assert main(["health", "psi", "--root", str(tmp_path / "none")]) == 0
    assert "unsupported" in capsys.readouterr().out
    assert main(["health", "diagnosis", "--exercise"]) == 0
    assert "link_lost" in capsys.readouterr().out
