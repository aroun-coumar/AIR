import json
from pathlib import Path

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts import ComponentManifest
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.hardware import (
    DeviceCatalog,
    Level,
    builtin_catalog,
    check_manifest,
    evaluate,
    load_files,
    maintenance_plan,
    matches,
    project_catalog,
    telemetry_limits,
)
from agentic_robotics.hardware.catalog import DOMAINS, TYPE_PREFIXES

pytestmark = pytest.mark.req("REQ03", "D05", "D07", "D16")
ROBOTS = Path(__file__).resolve().parents[1] / "examples" / "robots"


@pytest.fixture(scope="module")
def cat() -> DeviceCatalog:
    return builtin_catalog()


def test_builtin_catalog_is_broad_and_consistent(cat: DeviceCatalog) -> None:
    assert cat.problems() == []
    st = cat.stats()
    assert st["device_types"] >= 600 and st["robot_classes"] >= 100 and st["protocols"] >= 100
    assert set(st["by_category"]) == set(TYPE_PREFIXES)
    # every domain-relevant layer of the baseline has catalog coverage
    covered = {d for t in cat.device_types for d in t.domains} | \
        {d for p in cat.protocols for d in p.domains}
    for d in ("D01", "D02", "D03", "D04", "D05", "D07", "D12", "D14", "D15", "D16", "D18"):
        assert d in covered
    assert covered <= set(DOMAINS)


@pytest.mark.req("D14")
def test_safety_categories_are_marked_safety_relevant(cat: DeviceCatalog) -> None:
    assert all(t.safety_relevant for t in cat.search(category="safety"))
    assert cat.type("sensor.lidar.safety_scanner").safety_relevant  # type: ignore[union-attr]
    batteries = cat.match("power.battery.*")
    assert batteries and all(t.safety_relevant for t in batteries)


def test_lookup_search_and_matching(cat: DeviceCatalog) -> None:
    assert matches("sensor.imu.mems_6axis", "sensor.imu")
    assert matches("sensor.imu.mems_6axis", "sensor.*")
    assert not matches("sensor.imuX", "sensor.imu")
    assert {t.category for t in cat.search(interface="fieldbus.ethercat")} >= {"drive", "compute"}
    assert cat.protocol("can.canopen") is not None
    rc = cat.robot_class("robot.legged.quadruped")
    assert rc is not None and any(s.min >= 12 for s in rc.slots)
    # a slot may hold a whole robot: mobile manipulators carry an arm
    assert cat.match_classes("robot.industrial.*")


def test_catalog_contract_rejects_duplicates_and_unknown_keys(tmp_path: Path) -> None:
    t = {"type_id": "sensor.x", "name": "x", "category": "sensor", "domains": ["D05"]}
    with pytest.raises(SchemaError):
        DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0",
                                 "device_types": [t, t]})
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"device_types": [t], "robots": []}), encoding="utf-8")
    with pytest.raises(SchemaError):
        load_files([bad])
    with pytest.raises(SchemaError):  # strict contract: unknown field inside a type
        DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0",
                                 "device_types": [{**t, "colour": "red"}]})


def test_problems_catch_bad_units_interfaces_and_slots() -> None:
    cat = DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0", "device_types": [
        {"type_id": "sensor.thing", "name": "t", "category": "actuator", "domains": ["D99"],
         "properties": [{"name": "range", "kind": "quantity", "unit": "furlong"},
                        {"name": "mode", "kind": "enum"}],
         "telemetry": [{"name": "temp", "unit": "degC", "warn_above": 90,
                        "critical_above": 80}],
         "interfaces": ["bus.nope"]}],
        "robot_classes": [{"class_id": "robot.x", "name": "x", "domains": ["D01"],
                           "slots": [{"slot": "s", "types": ["sensor.missing.*"], "min": 2,
                                      "max": 1}]}]})
    found = "\n".join(cat.problems())
    for needle in ("category actuator", "unknown domain D99", "furlong", "enum without values",
                   "critical_above below", "unknown interface bus.nope", "max below min",
                   "nothing matches sensor.missing.*"):
        assert needle in found


@pytest.mark.parametrize("name", ["amr", "quadruped", "quadcopter", "cobot"])
def test_example_manifests_check_clean(cat: DeviceCatalog, name: str) -> None:
    m = ComponentManifest.load(ROBOTS / f"{name}.manifest.json")
    assert m.robot_class is not None
    assert check_manifest(m, cat, strict=True) == []


def _manifest(**component: object) -> ComponentManifest:
    return ComponentManifest.from_dict({
        "$schema": "ar.component_manifest/1.1", "robot_id": "r1", "name": "r",
        "hardware_revision": "a", "adapter": {"kind": "fake"},
        "components": [{"component_id": "c1", "kind": "k", "revision": "r1", **component}]})


def test_manifest_findings(cat: DeviceCatalog) -> None:
    m = _manifest(type_id="power.battery.lfp_pack",
                  properties={"nominal_voltage": {"value": 48, "unit": "A"},
                              "capacity": "lots", "colour": "blue"},
                  interfaces=[{"name": "x", "kind": "fieldbus.profinet"}])
    msgs = [str(f) for f in check_manifest(m, cat)]
    joined = "\n".join(msgs)
    assert "missing required property max_discharge_current" in joined
    assert "nominal_voltage: unit A is not a voltage" in joined
    assert "capacity: expected a number" in joined
    assert "warning: c1: property colour is not defined" in joined
    assert "not known to speak fieldbus.profinet" in joined
    ok = _manifest(type_id="power.battery.lfp_pack",
                   properties={"nominal_voltage": {"value": 48000, "unit": "mV"},
                               "capacity": 40, "max_discharge_current": 80})
    assert check_manifest(ok, cat) == []
    untyped = _manifest()
    assert check_manifest(untyped, cat)[0].level is Level.WARNING
    assert check_manifest(untyped, cat, strict=True)[0].level is Level.ERROR
    assert "unknown device type" in str(check_manifest(_manifest(type_id="sensor.nope"),
                                                       cat)[0])
    assert check_manifest(_manifest(type_id="robot.industrial.cobot"), cat) == []


def test_robot_class_slot_counts(cat: DeviceCatalog) -> None:
    m = ComponentManifest.load(ROBOTS / "quadcopter.manifest.json")
    fewer = m.evolve(components=tuple(c for c in m.components if c.component_id != "prop-4"
                                      and c.component_id != "esc-4"))
    # composition is advice, never a hardware requirement: a warning unless strict
    advice = [f for f in check_manifest(fewer, cat) if "at least 4" in f.message]
    assert advice and all(f.level is Level.WARNING for f in advice)
    strict = [f for f in check_manifest(fewer, cat, strict=True) if "at least 4" in f.message]
    assert strict and all(f.level is Level.ERROR for f in strict)
    unknown = m.evolve(robot_class="robot.nope")
    assert any("unknown robot class" in str(f) for f in check_manifest(unknown, cat))


@pytest.mark.req("REQ13", "REQ16")
def test_monitoring_and_maintenance_come_from_the_catalog(cat: DeviceCatalog) -> None:
    m = ComponentManifest.load(ROBOTS / "amr.manifest.json")
    limits = telemetry_limits(m, cat)
    soc = next(lim for lim in limits if lim.component_id == "battery"
               and lim.signal.name == "state_of_charge")
    assert evaluate(soc.signal, 50) == "ok"
    assert evaluate(soc.signal, 1) == "critical"
    plan = maintenance_plan(m, cat)
    assert plan and plan[0].safety_relevant           # safety-relevant items first
    assert {p.component_id for p in plan} >= {"battery", "scanner-front"}


def test_project_extension_adds_but_never_overrides(tmp_path: Path) -> None:
    hw = tmp_path / ".air" / "hardware"
    hw.mkdir(parents=True)
    new = {"type_id": "sensor.custom.widget", "name": "Widget", "category": "sensor",
           "domains": ["D05"]}
    (hw / "ours.json").write_text(json.dumps({"device_types": [new]}), encoding="utf-8")
    assert project_catalog(tmp_path).type("sensor.custom.widget") is not None
    clash = {**new, "type_id": "sensor.imu.mems_6axis"}
    (hw / "clash.json").write_text(json.dumps({"device_types": [clash]}), encoding="utf-8")
    with pytest.raises(SchemaError):
        project_catalog(tmp_path)


def test_cli(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["hardware", "types", "--category", "safety"]) == 0
    assert "safety.emergency_stop_button" in capsys.readouterr().out
    assert main(["hardware", "show", "fieldbus.ethercat"]) == 0
    assert '"realtime": "hard"' in capsys.readouterr().out
    assert main(["hardware", "check", str(ROBOTS / "amr.manifest.json")]) == 0
    assert main(["hardware", "plan", str(ROBOTS / "amr.manifest.json")]) == 0
    assert "critical below" in capsys.readouterr().out
    assert main(["hardware", "template", "robot.mobile.amr", "--robot-id", "amr-9"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["robot_class"] == "robot.mobile.amr" and doc["components"]
    assert main(["hardware", "stats", "--strict"]) == 0
    bad = tmp_path / "bad.manifest.json"
    bad.write_text(json.dumps({**doc, "components": [
        {"component_id": "x", "kind": "k", "revision": "r", "type_id": "sensor.nope"}]}),
        encoding="utf-8")
    assert main(["hardware", "check", str(bad)]) == 1
