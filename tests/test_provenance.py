"""Evidence spine: ledger integrity, causal tracing, project provenance, maintenance
lineage, requirements traceability, debugging explanations and context packs."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_robotics import PlanExecutor, ScriptedModel, TaskAgent
from agentic_robotics.cli import main as cli
from agentic_robotics.core import PlanError, SchemaError
from agentic_robotics.provenance import (
    ActivityStatus,
    ExperimentRecord,
    Ledger,
    MaintenanceRecord,
    Project,
    Replacement,
    RequirementStatus,
    span,
)
from agentic_robotics.provenance.context_pack import build_context_pack
from agentic_robotics.provenance.explain import explain, from_ledger, from_trace_dicts
from agentic_robotics.provenance.traceability import (
    Computed,
    TestEvidence,
    baseline_requirements,
    build_matrix,
    check,
    scan_code,
)

from .conftest import AGENT, Env

MakeEnv = Callable[..., Env]
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.req("REQ02", "REQ10", "F01")
debugging = pytest.mark.req("REQ14", "REQ15")
maintenance = pytest.mark.req("REQ16", "G13")
reproducible = pytest.mark.req("F05", "REQ02")


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path, name="demo", requirements=baseline_requirements())


# -- ledger ------------------------------------------------------------------------------
def test_ledger_chain_verifies_and_detects_edits(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    for i in range(5):
        ledger.append("thing.happened", f"s{i}", {"i": i})
    assert ledger.verify().ok

    lines = path.read_text().splitlines()
    record = json.loads(lines[2])
    record["data"]["i"] = 99
    lines[2] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    report = Ledger(path).verify()
    assert not report.ok and report.first_bad_seq == 2 and "hash" in report.problem


def test_ledger_detects_deletion_and_reordering(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    for i in range(4):
        ledger.append("k", f"s{i}")
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n")
    assert not Ledger(path).verify().ok
    path.write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n")
    assert not Ledger(path).verify().ok


def test_ledger_picks_up_appends_from_another_writer(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    a, b = Ledger(path), Ledger(path)
    a.append("k", "from-a")
    b.append("k", "from-b")  # must chain onto a's entry, not fork
    a.append("k", "from-a-again")
    assert Ledger(path).verify().ok
    assert [e.subject for e in Ledger(path)] == ["from-a", "from-b", "from-a-again"]


def test_ledger_entries_carry_current_trace() -> None:
    ledger = Ledger()
    with span("outer") as ctx:
        e = ledger.append("k", "s")
    assert e.trace_id == ctx.trace_id and e.span_id == ctx.span_id


# -- causal tracing through agent, plan and runtime ----------------------------------------
async def test_decision_plan_and_commands_share_one_trace(make_env: MakeEnv,
                                                          project: Project) -> None:
    env = make_env()
    env.runtime.trace.add_sink(project.runtime_sink())
    plan_json = json.dumps({"steps": [
        {"skill_id": "dock_at", "robot_id": "carrier-1", "args": {"station": "station_a"}},
        {"skill_id": "capture_image", "robot_id": "carrier-1", "args": {}}]})
    agent = TaskAgent(ScriptedModel([plan_json]), env.runtime.skills, env.runtime.clock,
                      trace=env.runtime.trace)
    plan = await agent.plan("inspect", [env.robot])
    assert plan.trace_id
    result = await PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id).wait(5)
    assert result.verified

    same_trace = env.runtime.trace.for_trace(plan.trace_id)
    kinds = [e.kind for e in same_trace]
    assert kinds[0] == "agent_decision"
    assert kinds.count("verified") == 2
    assert "reservation_held" in kinds and "reservation_released" in kinds
    decision = same_trace[0].data
    assert decision["outcome"] == "proposed" and decision["context_digest"].startswith("sha256")

    ledgered = project.ledger.query(trace_id=plan.trace_id)
    assert {"runtime.agent_decision", "runtime.verified"} <= {e.kind for e in ledgered}
    assert not project.ledger.query(kind="runtime.progress")  # high-rate kinds stay out
    assert project.ledger.verify().ok


async def test_rejected_agent_decisions_are_recorded(make_env: MakeEnv) -> None:
    env = make_env()
    agent = TaskAgent(ScriptedModel(["not json"]), env.runtime.skills, env.runtime.clock,
                      trace=env.runtime.trace)
    with pytest.raises(PlanError):
        await agent.plan("g", [env.robot])
    decision = env.runtime.trace.for_subject("agent:task")[-1]
    assert decision.data["outcome"] == "rejected"
    assert decision.data["error"]["code"] == "plan_error"


async def test_authority_changes_are_traced(make_env: MakeEnv) -> None:
    env = make_env()
    env.runtime.authority.revoke(env.delegation.delegation_id)
    kinds = env.runtime.trace.kinds()
    assert "authority_granted" in kinds and "authority_revoked" in kinds


# -- project activities --------------------------------------------------------------------
def test_project_is_found_from_subdirectories(project: Project) -> None:
    sub = project.root / "a" / "b"
    sub.mkdir(parents=True)
    assert Project.open(sub).root == project.root
    with pytest.raises(SchemaError):
        Project.init(project.root)


@reproducible
def test_activity_records_inputs_outputs_environment_and_lineage(project: Project) -> None:
    (project.root / "scenario.json").write_text('{"delay_ms": 200}')
    (project.root / "result.json").write_text('{"success": 0.97}')
    with project.activity("simulation", "handoff under delay", requirements=["REQ12"],
                          params={"seed": 7}) as act:
        scen = act.use("scenario.json", kind="scenario")
        res = act.generate("result.json", kind="report", derived_from=[scen])
        act.metric("success_rate", 0.97)
    a = act.result
    assert a is not None and a.status is ActivityStatus.SUCCEEDED
    assert a.used == (scen.entity_id,) and a.generated == (res.entity_id,)
    assert a.environment["python"] and a.trace_id
    assert project.artifacts.has(res.digest or "")

    tree = project.lineage(res.entity_id)
    gen = tree["generated_by"][0]
    assert gen["kind"] == "simulation" and gen["used"][0]["entity"] == scen.entity_id
    impact = project.impact(scen.entity_id)
    assert impact["used_in"][0]["generated"][0]["entity"] == res.entity_id
    assert project.activities(requirement="REQ12")[0].activity_id == a.activity_id


def test_activity_failure_is_recorded_and_reraised(project: Project) -> None:
    with pytest.raises(RuntimeError), project.activity("research", "flaky idea"):
        raise RuntimeError("solver diverged")
    a = project.activities(kind="research")[0]
    assert a.status is ActivityStatus.FAILED and "solver diverged" in (a.error or "")


def test_activity_rejects_unknown_requirements(project: Project) -> None:
    with pytest.raises(SchemaError, match="unknown requirements"), \
            project.activity("development", "x", requirements=["REQ99"]):
        pass


@maintenance
def test_maintenance_preserves_component_lineage(project: Project) -> None:
    record = MaintenanceRecord(
        record_id="mnt-0001", robot_id="arm-1", components=("arm-1/joint3-gearbox",),
        reason="backlash above limit", maintenance_type="condition",
        performed_by=("tech:sam",), findings=("worn harmonic drive",),
        replacements=(Replacement("hd-serial-1001", "hd-serial-2002", "wear"),),
        calibration_invalidated=("arm-1/joint3-zero",),
        return_to_service_checks={"joint3_backlash": True, "full_range_motion": True})
    act = project.record_maintenance(record)
    assert act.outcome == "returned to service"
    installed = "component:hd-serial-2002@unversioned"
    tree = project.lineage(installed)
    assert tree["replaces"]["entity"] == "component:hd-serial-1001@unversioned"
    assert project.impact("component:hd-serial-1001@unversioned")["replaced_by"] == installed
    assert project.ledger.query(kind="calibration.invalidated")[0].subject == \
        "arm-1/joint3-zero"


@maintenance
def test_maintenance_without_passing_checks_is_not_returned_to_service(
        project: Project) -> None:
    act = project.record_maintenance(MaintenanceRecord(
        record_id="mnt-2", robot_id="r", components=("c",), reason="r",
        return_to_service_checks={"torque_test": False}))
    assert act.outcome == "not returned to service"


@reproducible
def test_experiment_record(project: Project) -> None:
    act = project.record_experiment(ExperimentRecord(
        experiment_id="exp-1", hypothesis="lower inference resolution keeps accuracy",
        requirement_ids=("REQ04",), scenario="inspection-v2", backend="mujoco",
        backend_version="3.2.0", seed=3, results={"accuracy": 0.95}, accepted=True))
    assert act.outcome == "accepted" and act.metrics["accuracy"] == 0.95
    assert any(e.startswith("experiment_record:exp-1") for e in act.generated)


# -- requirements traceability ---------------------------------------------------------------
def test_code_scan_finds_framework_claims() -> None:
    code = scan_code([ROOT / "src"], ROOT)
    assert "agentic_robotics.runtime.runtime" in code["REQ12"]
    assert "agentic_robotics.provenance.ledger" in code["REQ10"]


def test_matrix_and_check_enforce_claims() -> None:
    reqs = baseline_requirements()
    reqs = reqs.evolve(requirements=tuple(
        r if r.req_id != "REQ10" else dataclasses.replace(r, status=RequirementStatus.VERIFIED)
        for r in reqs.requirements))
    code = {"REQ10": ["m.ledger"], "REQ12": ["m.rt"], "REQ77": ["m.x"]}
    evidence = TestEvidence(tests=[
        {"nodeid": "t::a", "outcome": "passed", "requirements": ["REQ12", "G05"]},
        {"nodeid": "t::b", "outcome": "failed", "requirements": ["REQ12"]},
    ])
    rows = {r.req_id: r for r in build_matrix(reqs, code, evidence)}
    assert rows["REQ12"].computed == Computed.FAILING
    assert rows["REQ10"].computed == Computed.IMPLEMENTED
    assert rows["G05"].computed == Computed.VERIFIED
    assert rows["REQ04"].computed == Computed.UNTRACED
    problems = check(list(rows.values()), reqs, code, evidence)
    assert any("REQ10: claimed verified" in p for p in problems)
    assert any("REQ12: failing" in p for p in problems)
    assert any("REQ77" in p for p in problems)


@pytest.mark.req("REQ18")
def test_pytest_plugin_records_evidence_and_activity(tmp_path: Path) -> None:
    Project.init(tmp_path, requirements=baseline_requirements())
    (tmp_path / "test_x.py").write_text(
        "import pytest\n"
        "@pytest.mark.req('REQ12')\n"
        "def test_ok():\n    assert True\n"
        "@pytest.mark.req('REQ13')\n"
        "def test_bad():\n    assert False\n")
    evidence = tmp_path / ".air" / "test-evidence.json"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"--air-evidence={evidence}", "--air-record", str(tmp_path)],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    doc = json.loads(evidence.read_text())
    outcomes = {t["nodeid"].split("::")[-1]: (t["outcome"], t["requirements"])
                for t in doc["tests"]}
    assert outcomes == {"test_ok": ("passed", ["REQ12"]), "test_bad": ("failed", ["REQ13"])}
    act = Project.open(tmp_path).activities(kind="evaluation")[0]
    assert act.status is ActivityStatus.FAILED and set(act.requirements) == {"REQ12", "REQ13"}


# -- debugging -------------------------------------------------------------------------------
@debugging
async def test_explain_rejection_with_debug_snapshot(make_env: MakeEnv,
                                                     project: Project) -> None:
    env = make_env()
    env.runtime.debug = True
    env.runtime.trace.add_sink(project.runtime_sink())
    env.adapter.inject("camera_offline")
    h = env.runtime.submit(env.command("capture_image"))
    await h.wait()
    text = "\n".join(explain(from_ledger(project.ledger), h.command_id))
    assert "outcome: rejected (capability_unavailable)" in text
    assert "camera_offline" in text
    assert "next step:" in text
    assert "admission snapshot" in text


@debugging
async def test_explain_unresolved_gives_reconcile_guidance(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("lose_link_after_motion")
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    await h.wait(5)
    rows = [json.loads(ln) for ln in env.runtime.trace.to_jsonl().splitlines()]
    text = "\n".join(explain(from_trace_dicts(rows), h.command_id))
    assert "outcome: unresolved (outcome_unknown)" in text
    assert "reconcile" in text


# -- CLI and context feeder ------------------------------------------------------------------
@pytest.mark.req("REQ18", "REQ16")
def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proj = ["--project", str(tmp_path)]
    assert cli(["init", "--baseline", *proj]) == 0
    (tmp_path / "cad.step").write_text("solid")
    assert cli(["activity", "record", "--kind", "development", "--title", "redesign mount",
                "--req", "REQ04", "--generated", str(tmp_path / "cad.step"),
                "--param", "material=\"al6061\"", *proj]) == 0
    assert cli(["maintenance", "record", "--robot", "arm-1", "--component", "gearbox",
                "--reason", "noise", "--replace", "gb-1=gb-2", "--check", "noise_test=pass",
                *proj]) == 0
    assert cli(["lineage", str(tmp_path / "cad.step"), *proj]) == 0
    assert "redesign mount" in capsys.readouterr().out
    assert cli(["impact", "gb-1", *proj]) == 0
    assert "replaced by component:gb-2" in capsys.readouterr().out
    assert cli(["ledger", "verify", *proj]) == 0
    assert cli(["activity", "list", "--req", "REQ04", *proj]) == 0
    assert "redesign mount" in capsys.readouterr().out
    # No test evidence yet: the check must fail rather than pass vacuously.
    assert cli(["requirements", "matrix", "--check", *proj]) == 1


@pytest.mark.req("REQ08", "REQ18")
def test_context_pack_for_this_repository() -> None:
    text = build_context_pack(ROOT)
    assert "# Context pack: agentic-robotics" in text
    assert "## Package map" in text and "runtime/runtime.py" in text
    assert "`command/1.1`" in text
