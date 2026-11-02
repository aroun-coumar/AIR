import asyncio
import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts import ComponentManifest
from agentic_robotics.core.clock import ManualClock
from agentic_robotics.core.errors import AuthorityError, SchemaError
from agentic_robotics.core.versions import satisfies, valid_range
from agentic_robotics.hardware import Level, builtin_catalog
from agentic_robotics.library import (
    EvidenceDomain,
    EvidenceRef,
    HostFacts,
    PackageDependency,
    PackageManifest,
    Provide,
    ProviderState,
    ServiceError,
    ServiceOutcomeUnknown,
    ServiceRegistry,
    SkillLibrary,
    SkillLibraryError,
    SkillStage,
    builtin_interfaces,
    check_installation,
    check_package_on_robot,
    check_skill_on_robot,
    registry_name,
)
from agentic_robotics.provenance import Project
from agentic_robotics.registry import LocalRegistry
from agentic_robotics.runtime.trace import Trace

pytestmark = pytest.mark.req("REQ05", "REQ18", "G02")
ROOT = Path(__file__).resolve().parents[1]
PKGS = ROOT / "examples" / "packages"
ROBOTS = ROOT / "examples" / "robots"
LINUX = HostFacts(os="linux", arch="amd64", python="3.12.4")


def pkg(name: str) -> PackageManifest:
    return PackageManifest.load(PKGS / f"{name}.package.json")


def robot(name: str) -> ComponentManifest:
    return ComponentManifest.load(ROBOTS / f"{name}.manifest.json")


def errors(findings: list[Any]) -> list[str]:
    return [f.message for f in findings if f.level is Level.ERROR]


# -- versions ----------------------------------------------------------------------------
@pytest.mark.parametrize(("version", "spec", "ok"), [
    ("1.4.0", "^1.2", True), ("2.0.0", "^1.2", False), ("1.1.9", "^1.2", False),
    ("0.2.5", "^0.2.1", True), ("0.3.0", "^0.2.1", False),
    ("1.2.9", "~1.2.3", True), ("1.3.0", "~1.2.3", False),
    ("1.5", ">=1.0,<2", True), ("2.0", ">=1.0,<2", False), ("9.9.9", "*", True),
    ("1.3.0-rc.1", "^1.2", False), ("1.3.0-rc.1", ">=1.3.0-rc.1", True),
])
def test_version_ranges(version: str, spec: str, ok: bool) -> None:
    assert satisfies(version, spec) is ok


def test_invalid_ranges_are_rejected_by_contracts() -> None:
    assert not valid_range("^banana")
    doc = json.loads((PKGS / "acme_nav.package.json").read_text("utf-8"))
    doc["requires"][0]["range"] = "^banana"
    with pytest.raises(SchemaError):
        PackageManifest.from_dict(doc)
    doc = json.loads((PKGS / "acme_nav.package.json").read_text("utf-8"))
    doc["name"] = "acme_nav"                       # needs namespace/kind/name
    with pytest.raises(SchemaError):
        PackageManifest.from_dict(doc)


# -- interfaces --------------------------------------------------------------------------
def test_interface_catalog_versions() -> None:
    cat = builtin_interfaces()
    assert cat.versions("air/service/navigation/plan_path") == ["1.0.0", "1.1.0"]
    assert cat.best("air/service/navigation/plan_path", "^1.0").version == "1.1.0"  # type: ignore[union-attr]
    assert cat.best("air/service/navigation/plan_path", "~1.0.0").version == "1.0.0"  # type: ignore[union-attr]
    assert cat.best("air/service/navigation/plan_path", "^2") is None
    nav = cat.get("air/service/navigation/navigate")
    assert nav is not None and nav.effect.value == "physical" and not nav.model_callable


# -- compatibility -----------------------------------------------------------------------
def test_example_deployment_fits_the_quadruped() -> None:
    pkgs = [pkg("quad_chassis"), pkg("acme_nav"), pkg("inspect_station")]
    assert errors(check_installation(pkgs, builtin_interfaces())) == []
    quad = robot("quadruped")
    for p in pkgs:
        assert errors(check_package_on_robot(p, quad, builtin_catalog(), LINUX)) == []
    skill = pkg("inspect_station").skills[0]
    assert check_skill_on_robot(skill, quad) == []


@pytest.mark.req("REQ03")
def test_package_hardware_and_host_needs_are_checked() -> None:
    nav = pkg("acme_nav")
    found = errors(check_package_on_robot(nav, robot("quadcopter"), builtin_catalog(), LINUX))
    assert any("declared incompatible" in m for m in found)
    assert any("needs a robot class" in m for m in found)
    assert any("sensor.lidar.*" in m for m in found)
    framed = nav.evolve(hardware=dataclasses.replace(nav.hardware, frames=("map", "world")))
    assert any("needs frame 'world'" in m
               for m in errors(check_package_on_robot(framed, robot("quadruped"))))
    hosted = nav.evolve(host=dataclasses.replace(nav.host, os=("linux",), arch=("arm64",),
                                                 python=">=3.13"))
    found = errors(check_package_on_robot(hosted, robot("quadruped"), host=LINUX))
    assert any("arch" in m for m in found) and any("python" in m for m in found)
    assert any("inspection" in m for m in errors(check_skill_on_robot(
        pkg("inspect_station").skills[0], robot("amr"))))


def test_installation_checks_requires_depends_conflicts_and_cycles() -> None:
    cat = builtin_interfaces()
    chassis, nav, inspect = pkg("quad_chassis"), pkg("acme_nav"), pkg("inspect_station")
    old_nav = nav.evolve(version="2.0.0", provides=(
        Provide("air/service/navigation/plan_path", "1.0.0"),))
    found = errors(check_installation([chassis, old_nav, inspect], cat))
    assert any("requires air/service/navigation/plan_path ^1.1; provided only at ['1.0.0']"
               in m for m in found)
    assert any("depends on air/skill" not in m for m in found)
    v3 = nav.evolve(version="3.0.0")
    assert any("depends on air/service/acme_nav >=2.0,<3, installed 3.0.0" in m
               for m in errors(check_installation([chassis, v3, inspect], cat)))
    assert any("not installed" in m for m in errors(check_installation([inspect], cat)))
    clash = chassis.evolve(conflicts=(PackageDependency("air/service/acme_nav", "^2"),))
    assert any("conflicts with installed" in m
               for m in errors(check_installation([clash, nav, inspect], cat)))
    a = chassis.evolve(depends=(PackageDependency("air/service/acme_nav"),))
    b = nav.evolve(depends=(PackageDependency("air/primitive/quad_chassis"),))
    assert any("dependency cycle" in m for m in errors(check_installation([a, b], cat)))
    fake = chassis.evolve(provides=(Provide("air/primitive/chassis/odometry", "7.0.0"),))
    assert any("not a catalog version" in m for m in errors(check_installation([fake], cat)))
    optional = [f for f in check_installation([chassis, nav, inspect], cat)
                if "mapping/get_map" in f.message]
    assert optional and optional[0].level is Level.WARNING


# -- services ----------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def handler(result: Mapping[str, Any] | Exception, calls: list[str], name: str) -> Any:
    async def run(interface: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(name)
        if isinstance(result, Exception):
            raise result
        return result
    return run


PATH_OK = {"path": {"points": []}, "length_m": 4.2}
PLAN = "air/service/navigation/plan_path"
PLAN_ARGS = {"start": {}, "goal": {}, "frame": "map"}


@pytest.mark.req("REQ15", "G15")
def test_service_registry_authorizes_and_routes() -> None:
    trace = Trace(ManualClock())
    reg = ServiceRegistry(builtin_interfaces(), trace=trace,
                          allowed_principals={"pkg:acme_nav", "pkg:cloud_nav"})
    calls: list[str] = []
    with pytest.raises(ServiceError):
        reg.register("rogue", "someone", [Provide(PLAN, "1.1.0")],
                     handler(PATH_OK, calls, "rogue"))
    with pytest.raises(ServiceError):
        reg.register("acme", "pkg:acme_nav", [Provide(PLAN, "9.0.0")],
                     handler(PATH_OK, calls, "acme"))
    reg.register("cloud", "pkg:cloud_nav", [Provide(PLAN, "1.1.0")],
                 handler(PATH_OK, calls, "cloud"), local=False)
    reg.register("acme", "pkg:acme_nav", [Provide(PLAN, "1.0.0")],
                 handler(PATH_OK, calls, "acme"))
    assert reg.resolve(PLAN).provider_id == "acme"            # local first
    assert reg.resolve(PLAN, "^1.1").provider_id == "cloud"   # version range wins
    assert reg.resolve(PLAN, prefer=["cloud"]).provider_id == "cloud"
    kinds = {e.kind for e in trace}
    assert {"service_registration_refused", "service_registered"} <= kinds


def test_service_calls_validate_fall_back_and_never_retry_state_changes() -> None:
    clock = Clock()
    reg = ServiceRegistry(builtin_interfaces(), clock=clock)
    calls: list[str] = []
    reg.register("flaky", "p", [Provide(PLAN, "1.1.0")],
                 handler(RuntimeError("down"), calls, "flaky"), priority=5)
    reg.register("steady", "p", [Provide(PLAN, "1.1.0")], handler(PATH_OK, calls, "steady"))
    result = asyncio.run(reg.call(PLAN, PLAN_ARGS))
    assert result.provider_id == "steady" and calls == ["flaky", "steady"]
    assert [a["outcome"] for a in result.attempts] == ["error", "ok"]
    with pytest.raises(ServiceError):
        asyncio.run(reg.call(PLAN, {"start": {}, "goal": {}}))           # missing frame
    with pytest.raises(ServiceError):
        asyncio.run(reg.call(PLAN, {**PLAN_ARGS, "speed": 3}))           # unknown input

    reg.register("map1", "p", [Provide("air/service/mapping/annotate", "1.0.0")],
                 handler(TimeoutError(), calls, "map1"), priority=5)
    reg.register("map2", "p", [Provide("air/service/mapping/annotate", "1.0.0")],
                 handler({"revision": 2}, calls, "map2"))
    calls.clear()
    with pytest.raises(ServiceOutcomeUnknown):
        asyncio.run(reg.call("air/service/mapping/annotate", {"label": "door", "pose": {}}))
    assert calls == ["map1"]                                 # not retried on map2

    reg.register("nav", "p", [Provide("air/service/navigation/navigate", "1.0.0")],
                 handler({"reached": True, "final_pose": {}}, calls, "nav"))
    with pytest.raises(ServiceError, match="admission"):
        asyncio.run(reg.call("air/service/navigation/navigate", {"goal": {}, "frame": "map"}))


def test_contract_violations_and_heartbeats_change_provider_state() -> None:
    clock = Clock()
    reg = ServiceRegistry(builtin_interfaces(), clock=clock)
    calls: list[str] = []
    reg.register("liar", "p", [Provide(PLAN, "1.1.0")],
                 handler({"path": {}}, calls, "liar"))          # no length_m
    with pytest.raises(ServiceError):
        asyncio.run(reg.call(PLAN, PLAN_ARGS))
    assert {p.provider_id: p.state for p in reg.providers()}["liar"] is ProviderState.ERROR
    reg.register("beat", "p", [Provide(PLAN, "1.1.0")], handler(PATH_OK, calls, "beat"),
                 heartbeat_s=2.0)
    clock.t = 5.0
    assert {p.provider_id: p.state for p in reg.providers()}["beat"] is ProviderState.STALE
    with pytest.raises(ServiceError):
        reg.resolve(PLAN)
    reg.heartbeat("beat")
    assert reg.resolve(PLAN).provider_id == "beat"


# -- skill library -----------------------------------------------------------------------
@pytest.fixture
def lib(tmp_path: Path) -> SkillLibrary:
    project = Project.init(tmp_path, name="t")
    library = SkillLibrary(project, LocalRegistry(tmp_path / ".ar-registry"),
                           builtin_catalog())
    for name in ("quad_chassis", "acme_nav", "inspect_station"):
        library.publish(pkg(name), by="engineer:sam")
    return library


def evaluation(lib: SkillLibrary, domain: EvidenceDomain, passed: bool = True,
               held_out: bool = True) -> EvidenceRef:
    with lib.project.activity("evaluation", f"inspect_station {domain.value}") as act:
        act.metric("success_rate", 0.95 if passed else 0.4)
    assert act.result is not None
    return EvidenceRef(act.result.activity_id, domain, passed, {"success_rate": 0.95},
                       held_out=held_out)


REF = "inspect_station@1.2.0"


@pytest.mark.req("REQ11", "REQ16", "G03", "G15")
def test_promotion_is_evidence_bound_and_by_another_person(lib: SkillLibrary) -> None:
    assert lib.get(REF).stage is SkillStage.DRAFT
    lib.promote(REF, "candidate", by="engineer:sam")
    sim = evaluation(lib, EvidenceDomain.SIMULATION)
    with pytest.raises(AuthorityError):
        lib.promote(REF, "validated_sim", by="engineer:sam", evidence=[sim])   # producer
    with pytest.raises(AuthorityError):
        lib.promote(REF, "validated_sim", by="agent:tuner", evidence=[sim])    # an agent
    with pytest.raises(SkillLibraryError):
        lib.promote(REF, "validated_sim", by="lead:kim",
                    evidence=[EvidenceRef("act_nope", EvidenceDomain.SIMULATION, True,
                                          held_out=True)])
    with pytest.raises(SkillLibraryError):                                   # not held out
        lib.promote(REF, "validated_sim", by="lead:kim",
                    evidence=[evaluation(lib, EvidenceDomain.SIMULATION, held_out=False)])
    lib.promote(REF, "validated_sim", by="lead:kim", evidence=[sim])
    with pytest.raises(SkillLibraryError, match="simulation never authorizes hardware"):
        lib.promote(REF, "validated_real", by="lead:kim",
                    evidence=[evaluation(lib, EvidenceDomain.SIMULATION)])
    with pytest.raises(SkillLibraryError, match="failed hardware"):
        lib.promote(REF, "validated_real", by="lead:kim",
                    evidence=[evaluation(lib, EvidenceDomain.HARDWARE),
                              evaluation(lib, EvidenceDomain.HARDWARE, passed=False)])
    with pytest.raises(SkillLibraryError, match="re-verification"):
        lib.promote(REF, "validated_real", by="lead:kim",
                    evidence=[evaluation(lib, EvidenceDomain.HARDWARE)],
                    reverify=lambda rel: EvidenceRef("x", EvidenceDomain.HARDWARE, False))
    rel = lib.promote(REF, "validated_real", by="lead:kim",
                      evidence=[evaluation(lib, EvidenceDomain.HARDWARE)])
    assert rel.stage is SkillStage.VALIDATED_REAL and rel.promoted_by == "lead:kim"
    kinds = [e["kind"] for e in lib.lineage("inspect_station")]
    assert kinds[0] == "skill.released" and kinds.count("skill.stage_changed") == 3


def test_tampered_packages_are_never_promoted_or_selected(lib: SkillLibrary) -> None:
    lib.promote(REF, "candidate", by="engineer:sam")
    stored = next((lib.registry.root).rglob("*.json"))
    for path in lib.registry.root.rglob("*.json"):
        if "inspect_station" in str(path):
            stored = path
    record = json.loads(stored.read_text("utf-8"))
    record["document"]["skills"][0]["required_capabilities"] = []
    stored.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(SchemaError):
        lib.promote(REF, "validated_sim", by="lead:kim",
                    evidence=[evaluation(lib, EvidenceDomain.SIMULATION)])
    assert lib.resolve("inspect_station", robot("quadruped"), "dev").release is None


def test_resolution_champions_and_rollback(lib: SkillLibrary) -> None:
    quad = robot("quadruped")
    assert lib.resolve("inspect_station", quad, "real").release is None
    lib.promote(REF, "candidate", by="engineer:sam")
    lib.promote(REF, "validated_real", by="lead:kim",
                evidence=[evaluation(lib, EvidenceDomain.HARDWARE)])
    newer = pkg("inspect_station").evolve(version="1.3.0", skills=(
        pkg("inspect_station").skills[0].evolve(version="1.3.0"),))
    lib.publish(newer, by="agent:tuner", parents={"inspect_station": REF})
    lib.promote("inspect_station@1.3.0", "candidate", by="agent:tuner")
    lib.promote("inspect_station@1.3.0", "validated_sim", by="lead:kim",
                evidence=[evaluation(lib, EvidenceDomain.SIMULATION)])
    assert lib.resolve("inspect_station", quad, "real").release.ref == REF  # type: ignore[union-attr]
    assert lib.resolve("inspect_station", quad, "sim").release.version == "1.3.0"  # type: ignore[union-attr]
    lib.set_champion(REF, "sim", by="lead:kim", reason="1.3 overshoots in sim replay")
    res = lib.resolve("inspect_station", quad, "sim")
    assert res.release.ref == REF and "champion" in res.reasons  # type: ignore[union-attr]
    with pytest.raises(AuthorityError):
        lib.set_champion("inspect_station@1.3.0", "sim", by="agent:tuner")
    lib.rollback("inspect_station", "sim", "inspect_station@1.3.0", by="lead:kim",
                 reason="regression fixed upstream")
    assert lib.champion("inspect_station", "sim") == "inspect_station@1.3.0"
    off = lib.resolve("inspect_station", robot("amr"), "real")
    assert off.release is None and any("inspection" in why for _, why in off.rejected)
    registry, findings = lib.registry_for(quad, ["inspect_station"], "real")
    assert "inspect_station" in registry and findings == []
    lib.promote(REF, "revoked", by="lead:kim", reason="unsafe near stairs")
    assert lib.resolve("inspect_station", quad, "real").release is None
    with pytest.raises(SkillLibraryError):
        lib.promote(REF, "candidate", by="lead:kim")             # revoked is terminal


def test_skill_versions_are_immutable(lib: SkillLibrary) -> None:
    changed = pkg("inspect_station").evolve(version="1.2.1")
    with pytest.raises(SkillLibraryError, match="immutable"):
        lib.publish(changed, by="engineer:sam")                 # same skill version, new bytes
    assert lib.registry.versions(registry_name("air/skill/inspect_station"))


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    Project.init(tmp_path, name="t")
    proj = ["--project", str(tmp_path)]
    assert main(["interfaces", "list"]) == 0
    assert "air/service/navigation/plan_path" in capsys.readouterr().out
    for name in ("quad_chassis", "acme_nav", "inspect_station"):
        assert main(["pkg", "publish", str(PKGS / f"{name}.package.json"),
                     "--by", "engineer:sam", *proj]) == 0
    capsys.readouterr()
    assert main(["pkg", "deploy-check", str(PKGS / "quadruped.deployment.json"),
                 "--robot", str(ROBOTS / "quadruped.manifest.json"), "--no-host", *proj]) == 0
    assert main(["pkg", "check", str(PKGS / "acme_nav.package.json"),
                 "--robot", str(ROBOTS / "quadcopter.manifest.json"), "--no-host"]) == 1
    assert main(["skills", "promote", REF, "candidate", "--by", "engineer:sam", *proj]) == 0
    assert main(["skills", "resolve", "inspect_station", "--robot",
                 str(ROBOTS / "quadruped.manifest.json"), "--mode", "dev", "--no-host",
                 *proj]) == 0
    assert '"selected": "inspect_station@1.2.0"' in capsys.readouterr().out
    dep = json.loads((PKGS / "quadruped.deployment.json").read_text("utf-8"))
    dep["packages"][1]["config"] = {"max_speed": 9}
    bad = tmp_path / "bad.deployment.json"
    bad.write_text(json.dumps(dep), encoding="utf-8")
    assert main(["pkg", "deploy-check", str(bad), "--robot",
                 str(ROBOTS / "quadruped.manifest.json"), "--no-host", *proj]) == 1
    out = capsys.readouterr().out
    assert "missing required config inflation_radius" in out and "above maximum" in out
    assert main(["skills", "lineage", "inspect_station", *proj]) == 0
