import json
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics.agents.context import ContextBuilder
from agentic_robotics.cli import main
from agentic_robotics.contracts import (
    ComponentManifest,
    DocumentRef,
    LimitOverride,
    Relation,
    RelationKind,
    Sensitivity,
)
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.hardware import (
    DeviceCatalog,
    builtin_catalog,
    check_manifest,
    load_files,
    telemetry_limits,
)
from agentic_robotics.hardware.context import build_context
from agentic_robotics.hardware.semantics import (
    bind_observation,
    equivalents,
    lwm2m_urn,
    manifest_graph,
    thing_model,
    unit_iri,
)
from agentic_robotics.provenance import Project
from agentic_robotics.provenance.project import file_digest

pytestmark = pytest.mark.req("REQ03", "REQ08", "D16")
ROBOTS = Path(__file__).resolve().parents[1] / "examples" / "robots"


@pytest.fixture(scope="module")
def cat() -> DeviceCatalog:
    return builtin_catalog()


@pytest.fixture()
def amr() -> ComponentManifest:
    return ComponentManifest.load(ROBOTS / "amr.manifest.json")


def _with(m: ComponentManifest, component_id: str, **changes: Any) -> ComponentManifest:
    import dataclasses
    return m.evolve(components=tuple(
        dataclasses.replace(c, **changes) if c.component_id == component_id else c
        for c in m.components))


# -- custom components -------------------------------------------------------------------
def _vendor_catalog(tmp_path: Path, **override: Any) -> DeviceCatalog:
    vendor = {"type_id": "actuator.motor.acme_m48", "name": "ACME M48 hub motor",
              "category": "actuator", "domains": ["D01"], "extends": "actuator.motor.hub_motor",
              "sensitivity": "restricted",
              "properties": [{"name": "acme_winding_code", "kind": "string", "required": True}],
              "telemetry": [{"name": "winding_temperature", "unit": "degC",
                             "warn_above": 110, "critical_above": 130}], **override}
    f = tmp_path / "vendor.json"
    f.write_text(json.dumps({"device_types": [vendor]}), encoding="utf-8")
    return builtin_catalog().merged(load_files([f]))


def test_extends_inherits_and_overrides(tmp_path: Path) -> None:
    cat = _vendor_catalog(tmp_path)
    t = cat.type("actuator.motor.acme_m48")
    base = cat.type("actuator.motor.hub_motor")
    assert t is not None and base is not None
    assert cat.lineage(t.type_id) == ["actuator.motor.acme_m48", "actuator.motor.hub_motor"]
    names = {p.name for p in t.properties}
    assert {p.name for p in base.properties} <= names and "acme_winding_code" in names
    assert t.signal("winding_temperature").warn_above == 110  # type: ignore[union-attr]
    assert set(base.failure_modes) <= set(t.failure_modes)
    assert t.sensitivity is Sensitivity.RESTRICTED
    assert cat.declared(t.type_id).properties != t.properties  # type: ignore[union-attr]
    assert cat.problems() == []
    assert t in cat.match("actuator.motor.*")


@pytest.mark.req("D14")
def test_specialization_may_not_weaken_safety(tmp_path: Path) -> None:
    base = "power.battery.lfp_pack"                      # safety-relevant in the catalog
    assert builtin_catalog().type(base).safety_relevant  # type: ignore[union-attr]
    signal = builtin_catalog().type(base).telemetry      # type: ignore[union-attr]
    limited = next(s for s in signal if s.warn_above is not None)
    assert limited.warn_above is not None
    cat = _vendor_catalog(tmp_path, type_id="power.battery.acme_pack", category="power",
                          extends=base, safety_relevant=False, properties=[],
                          telemetry=[{"name": limited.name, "unit": limited.unit,
                                      "warn_above": limited.warn_above + 50}])
    found = "\n".join(cat.problems())
    assert "cannot clear safety_relevant" in found and "loosens a limit" in found
    assert cat.type("power.battery.acme_pack").safety_relevant  # type: ignore[union-attr]


def test_inheritance_cycle_and_unknown_parent(tmp_path: Path) -> None:
    a = {"type_id": "sensor.x_a", "name": "a", "category": "sensor", "domains": ["D05"],
         "extends": "sensor.x_b"}
    b = {**a, "type_id": "sensor.x_b", "extends": "sensor.x_a"}
    with pytest.raises(SchemaError):
        DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0", "device_types": [a, b]})
    orphan = DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0",
                                      "device_types": [{**a, "extends": "sensor.nope"}]})
    assert any("extends unknown type" in p for p in orphan.problems())


def test_per_unit_limit_overrides_win_and_say_where_from(cat: DeviceCatalog,
                                                         amr: ComponentManifest) -> None:
    m = _with(amr, "wheel-left", limits=(LimitOverride(
        "winding_temperature", warn_above=100, critical_above=125,
        reason="datasheet rev C table 4"),))
    lims = {(x.component_id, x.signal.name): x for x in telemetry_limits(m, cat)}
    left, right = lims[("wheel-left", "winding_temperature")], \
        lims[("wheel-right", "winding_temperature")]
    assert left.signal.warn_above == 100 and "datasheet rev C" in left.source
    assert right.source == "catalog:actuator.motor.hub_motor"
    assert bind_observation(m, cat, "wheel-left.winding_temperature", 110).status == "warn"  # type: ignore[union-attr]
    assert bind_observation(m, cat, "wheel-right.winding_temperature", 110).status == "ok"  # type: ignore[union-attr]
    bad = _with(amr, "wheel-left", limits=(LimitOverride("no_such_signal", warn_above=1),))
    assert any("does not define" in str(f) for f in check_manifest(bad, cat))


def test_documents_are_pinned_by_digest(tmp_path: Path, cat: DeviceCatalog,
                                        amr: ComponentManifest) -> None:
    sheet = tmp_path / "docs" / "motor.pdf"
    sheet.parent.mkdir()
    sheet.write_bytes(b"%PDF datasheet v1")
    m = _with(amr, "wheel-left", documents=(DocumentRef("docs/motor.pdf", file_digest(sheet),
                                                        title="Hub motor datasheet"),))
    assert check_manifest(m, cat, root=tmp_path) == []
    sheet.write_bytes(b"%PDF datasheet v2")
    assert any("changed since it was attached" in str(f)
               for f in check_manifest(m, cat, root=tmp_path))
    sheet.unlink()
    assert any("document missing" in str(f) for f in check_manifest(m, cat, root=tmp_path))


def test_relations_are_validated_and_walkable(amr: ComponentManifest) -> None:
    m = _with(amr, "scanner-front",
              relations=(Relation(RelationKind.PROTECTS, "chassis"),))
    assert ("protects", "chassis") in m.neighbours("scanner-front")
    assert ("protected_by", "scanner-front") in m.neighbours("chassis")
    assert ("part_of", "safety-plc") in m.neighbours("scanner-front")
    with pytest.raises(SchemaError):
        _with(amr, "scanner-front", relations=(Relation(RelationKind.PROTECTS, "nope"),))


# -- semantics ---------------------------------------------------------------------------
def test_vocabulary_links(cat: DeviceCatalog) -> None:
    assert unit_iri("V") == "unit:V" and unit_iri("degC") == "unit:DEG_C"
    assert unit_iri("TOPS") is None              # no confident QUDT unit: never guessed
    assert lwm2m_urn(3) == "urn:oma:lwm2m:oma:3"
    assert lwm2m_urn(3303) == "urn:oma:lwm2m:ext:3303"
    eq = equivalents(cat.type("iot.sensor.temperature"))  # type: ignore[arg-type]
    assert {"scheme": "lwm2m.object", "id": "3303", "urn": "urn:oma:lwm2m:ext:3303"}.items() \
        <= eq[0].items()
    assert any(e["scheme"] == "matter.device_type" and e["id"] == "0x0302" for e in eq)


def test_thing_model_export(cat: DeviceCatalog) -> None:
    tm = thing_model(cat.type("power.battery.lfp_pack"))  # type: ignore[arg-type]
    assert tm["@context"][0] == "https://www.w3.org/2022/wot/td/v1.1"
    assert "tm:ThingModel" in tm["@type"] and tm["air:safetyRelevant"] is True
    soc = tm["properties"]["state_of_charge"]
    assert soc["observable"] and soc["readOnly"] and soc["unit"] == "unit:PERCENT"
    assert soc["air:criticalBelow"] < soc["air:warnBelow"]
    sensor = thing_model(cat.type("sensor.imu.mems_6axis"))  # type: ignore[arg-type]
    assert "sosa:Sensor" in sensor["@type"]


def test_manifest_graph(cat: DeviceCatalog, amr: ComponentManifest) -> None:
    g = manifest_graph(amr, cat)
    root, *nodes = g["@graph"]
    assert root["@type"][0] == "sosa:Platform"
    assert {n["@id"] for n in nodes} == {f"urn:air:robot:amr-1/{c.component_id}"
                                         for c in amr.components}
    battery = next(n for n in nodes if n["@id"].endswith("/battery"))
    nominal = next(v for v in battery["schema:additionalProperty"]
                   if v["schema:name"] == "nominal_voltage")
    assert nominal["unit"] == "unit:V" and nominal["qudt:hasQuantityKind"] == \
        "quantitykind:Voltage"


def test_observation_binding_checks_units(cat: DeviceCatalog, amr: ComponentManifest) -> None:
    b = bind_observation(amr, cat, "battery.pack_temperature", 333.15, "K")
    assert b is not None and b.problem is not None    # temperatures are never converted
    ok = bind_observation(amr, cat, "battery.pack_voltage", 48000, "mV")
    assert ok is not None and ok.value == pytest.approx(48)
    assert bind_observation(amr, cat, "nope.signal", 1) is None


# -- task context ------------------------------------------------------------------------
def test_context_focuses_on_the_named_part(cat: DeviceCatalog, amr: ComponentManifest) -> None:
    ctx = build_context(amr, cat, text="left wheel overheating",
                        observations={"wheel-left.winding_temperature": (128, "degC")},
                        budget_chars=2500)
    subjects = [f.subject for f in ctx.facts]
    assert subjects and "wheel-left" in subjects
    first_kinds = {f.kind for f in ctx.facts if f.subject == "wheel-left"}
    assert {"reading", "limit", "failure_modes"} <= first_kinds
    assert "wheel-left" in ctx.components[:3]          # its drive and tyre rank alongside
    assert "compute" not in subjects                  # unrelated parts do not crowd in
    assert sum(len(f.text) for f in ctx.facts) <= 2500
    assert ctx.omitted > 0
    reading = next(f for f in ctx.facts if f.kind == "reading")
    assert "warn" in reading.text


def test_context_withholds_restricted_facts(cat: DeviceCatalog, amr: ComponentManifest) -> None:
    secret = _with(amr, "wheel-left", sensitivity=Sensitivity.RESTRICTED)
    open_ctx = build_context(secret, cat, focus=["wheel-left"])
    closed = build_context(secret, cat, focus=["wheel-left"], allow_restricted=False)
    assert any(f.sensitivity is Sensitivity.RESTRICTED for f in open_ctx.facts)
    assert not any(f.sensitivity is Sensitivity.RESTRICTED for f in closed.facts)
    assert closed.withheld_restricted > 0
    assert "restricted facts withheld" in closed.render()


@pytest.mark.req("REQ16")
def test_context_includes_ledger_history_and_reuses_fingerprint(
        tmp_path: Path, cat: DeviceCatalog, amr: ComponentManifest) -> None:
    from agentic_robotics.provenance.model import MaintenanceRecord, Replacement

    project = Project.init(tmp_path, name="t")
    a = build_context(amr, cat, focus=["wheel-left"], project=project)
    b = build_context(amr, cat, focus=["wheel-left"], project=project)
    assert a.fingerprint == b.fingerprint
    project.record_maintenance(MaintenanceRecord(
        record_id="mr-1", robot_id="amr-1", components=("wheel-left",),
        reason="bearing noise", performed_by=("tech:ana",),
        replacements=(Replacement(removed="wheel-left", installed="wheel-left-sn42",
                                  reason="bearing noise"),)))
    c = build_context(amr, cat, focus=["wheel-left"], project=project)
    assert c.fingerprint != a.fingerprint            # the ledger moved
    history = [f for f in c.facts if f.kind == "history"]
    assert history and "bearing noise" in history[0].text


def test_work_tool_respects_egress(tmp_path: Path) -> None:
    from agentic_robotics.work.contracts import DataClass, ModelConstraints, WorkKind, WorkTask
    from agentic_robotics.work.tools import ALL_TOOLS, DEFAULT_TOOLS, may_see_restricted

    assert "hardware_context" in DEFAULT_TOOLS[WorkKind.DIAGNOSIS]
    cloud = WorkTask(task_id="t", kind=WorkKind.DIAGNOSIS, goal="g")
    local = WorkTask(task_id="t", kind=WorkKind.DIAGNOSIS, goal="g",
                     models=ModelConstraints(allow_data_egress=False))
    secret = WorkTask(task_id="t", kind=WorkKind.DIAGNOSIS, goal="g",
                      data_classification=DataClass.RESTRICTED)
    assert not may_see_restricted(cloud)
    assert may_see_restricted(local) and may_see_restricted(secret)
    assert ALL_TOOLS["hardware_context"].side_effect.value == "read"


def test_context_builder_hardware_section(cat: DeviceCatalog) -> None:
    from agentic_robotics.core.clock import ManualClock
    from agentic_robotics.robot import Robot

    robot = Robot.from_manifest(ROBOTS / "carrier.manifest.json")
    robot.adapter.bind_clock(ManualClock())
    view = ContextBuilder(catalog=cat).robot_view(robot, now=0.0)
    assert "hardware" in view and "safety_components" in view["hardware"]
    assert "hardware" not in ContextBuilder().robot_view(robot, now=0.0)


def test_cli_context_attach_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    Project.init(tmp_path, name="t")
    manifest = tmp_path / "amr.manifest.json"
    manifest.write_text((ROBOTS / "amr.manifest.json").read_text("utf-8"), encoding="utf-8")
    sheet = tmp_path / "motor.pdf"
    sheet.write_bytes(b"%PDF sheet")
    proj = ["--project", str(tmp_path)]
    assert main(["hardware", "attach", str(manifest), "wheel-left", str(sheet), *proj,
                 "--sensitivity", "restricted"]) == 0
    m = ComponentManifest.load(manifest)
    doc = m.component("wheel-left").documents[0]  # type: ignore[union-attr]
    assert doc.path == "motor.pdf" and doc.sensitivity is Sensitivity.RESTRICTED
    assert main(["hardware", "check", str(manifest), *proj]) == 0
    capsys.readouterr()
    assert main(["hardware", "context", str(manifest), *proj, "--focus", "wheel-left",
                 "--no-restricted", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["withheld_restricted"] >= 1
    assert main(["hardware", "export", "sensor.imu.mems_6axis"]) == 0
    assert "tm:ThingModel" in capsys.readouterr().out
    assert main(["hardware", "export", str(manifest), *proj]) == 0
    assert "sosa:Platform" in capsys.readouterr().out


# -- domain packs ------------------------------------------------------------------------
def test_domain_library_is_complete_and_consistent(cat: DeviceCatalog) -> None:
    from agentic_robotics.hardware.domains import builtin_domains

    lib = builtin_domains()
    assert [d.domain_id for d in lib.domains] == [f"D{n:02d}" for n in range(1, 20)]
    assert lib.problems(cat) == []
    assert all(d.invariants and d.failure_taxonomy for d in lib.domains)
    brief = lib.brief(["D02", "D14"])
    assert "## D02" in brief and "## D14" in brief and "Invariants" in brief


@pytest.mark.req("D14")
def test_domain_rules_are_advice_unless_strict(cat: DeviceCatalog, amr: ComponentManifest) -> None:
    from agentic_robotics.hardware import Level
    from agentic_robotics.hardware.domains import (
        DomainLibrary,
        DomainPack,
        DomainRule,
        RuleKind,
    )

    lib = DomainLibrary(domains=(DomainPack("D02", "Power", rules=(
        DomainRule("D02.r1", RuleKind.REQUIRES_COMPONENT, ("power.battery.*",),
                   "needs a fuel cell", Level.ERROR, any_of=("power.fuel_cell*",)),
        DomainRule("D02.r2", RuleKind.REQUIRES_PROPERTY, ("power.battery.*",),
                   "state the cycle life", Level.ERROR, property="cycle_life"),
        DomainRule("D02.r3", RuleKind.REQUIRES_RELATION, ("sensor.lidar.safety_scanner",),
                   "scanner on safety logic", Level.ERROR, any_of=("compute.safety_plc",)),
        DomainRule("D02.r4", RuleKind.REQUIRES_INTERFACE, ("power.bms",),
                   "BMS on CAN", Level.ERROR, any_of=("can.*",)),
        DomainRule("D02.r5", RuleKind.MAX_COUNT, ("sensor.lidar.safety_scanner",),
                   "one scanner", Level.ERROR, max=1),
    )),))
    advice = lib.check(amr, cat)
    hit = {f.message.rsplit("[", 1)[1].rstrip("]") for f in advice}
    assert hit == {"D02.r1", "D02.r2", "D02.r4", "D02.r5"}   # r3 holds: scanners sit on the PLC
    assert all(f.level is Level.WARNING for f in advice)
    assert all(f.level is Level.ERROR for f in lib.check(amr, cat, strict=True))
