"""Agent-performed research/dev/simulation/maintenance work in sandboxes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_robotics.agents.providers import Location, ModelRouter, ScriptedChat, call, say
from agentic_robotics.contracts import Delegation
from agentic_robotics.core import AuthorityError
from agentic_robotics.provenance import ActivityKind, ActivityStatus, Project
from agentic_robotics.provenance.traceability import baseline_requirements
from agentic_robotics.runtime import AuthorityService
from agentic_robotics.work import (
    ContainerSandbox,
    DataClass,
    Decision,
    LocalProcessSandbox,
    ModelConstraints,
    ProposalStatus,
    SandboxError,
    SandboxPolicy,
    WorkAgent,
    WorkBudget,
    WorkKind,
    WorkStatus,
    WorkTask,
    decide,
    proposals,
)

from .conftest import Env

pytestmark = pytest.mark.req("AIR01", "AIR03", "REQ02", "REQ04", "REQ17")
sandboxing = pytest.mark.req("G15", "REQ02")
MakeEnv = Callable[..., Env]


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path / "proj", requirements=baseline_requirements())


def router(*steps: object, location: Location = Location.LOCAL_HOST) -> ModelRouter:
    r = ModelRouter()
    r.register(ScriptedChat(list(steps), location=location))  # type: ignore[arg-type]
    return r


def local_sandbox(policy: SandboxPolicy) -> LocalProcessSandbox:
    return LocalProcessSandbox(policy)


def agent(project: Project, r: ModelRouter, **kw: object) -> WorkAgent:
    return WorkAgent(project, r, sandbox_factory=local_sandbox, **kw)  # type: ignore[arg-type]


# -- sandbox -------------------------------------------------------------------------------------
@sandboxing
async def test_process_sandbox_runs_python_with_a_scrubbed_environment(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
    sb = LocalProcessSandbox()
    try:
        sb.write_text("probe.py", "import os, json; print(json.dumps(sorted(os.environ)))")
        result = await sb.run(["python", "probe.py"])
        assert result.ok, result.stderr
        env_keys = json.loads(result.stdout)
        assert "ANTHROPIC_API_KEY" not in env_keys
        assert "network" in result.unenforced and "filesystem" in result.unenforced
    finally:
        sb.close()
    assert not sb.workspace.exists()


@sandboxing
async def test_sandbox_enforces_timeout_output_cap_and_command_allowlist() -> None:
    sb = LocalProcessSandbox(SandboxPolicy(max_wall_s=1.0, max_output_bytes=100))
    try:
        sb.write_text("loop.py", "while True: pass")
        slow = await sb.run(["python", "loop.py"])
        assert slow.timed_out and slow.exit_code is None
        sb.write_text("big.py", "print('x' * 10000)")
        big = await sb.run(["python", "big.py"])
        assert big.truncated and len(big.stdout) <= 100
        with pytest.raises(SandboxError, match="not allowed"):
            await sb.run(["cmd", "/c", "dir"] if os.name == "nt" else ["sh", "-c", "ls"])
    finally:
        sb.close()


@sandboxing
def test_sandbox_refuses_paths_outside_the_workspace() -> None:
    sb = LocalProcessSandbox()
    try:
        for bad in ("../escape.txt", str(Path.home() / "x.txt"), "a/../../b"):
            with pytest.raises(SandboxError):
                sb.write_text(bad, "nope")
    finally:
        sb.close()


@sandboxing
def test_policy_requiring_unenforceable_isolation_is_refused() -> None:
    with pytest.raises(SandboxError, match="network"):
        LocalProcessSandbox(SandboxPolicy(require_enforced=("network",)))


@sandboxing
def test_container_sandbox_arguments(tmp_path: Path) -> None:
    sb = ContainerSandbox(SandboxPolicy(max_memory_mb=512, max_cpus=1.0),
                          runtime=str(tmp_path / "docker"), root=tmp_path)
    try:
        argv = sb.container_argv(["python", "x.py"])
        joined = " ".join(argv)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--memory 512m",
                     "--cpus 1.0", "no-new-privileges"):
            assert flag in joined
        assert argv[-2:] == ["python", "x.py"]
        assert sb.capabilities.unenforced() == []
    finally:
        sb.close()


# -- work agents -----------------------------------------------------------------------------------
@pytest.mark.req("REQ02", "F01")
async def test_simulation_work_is_sandboxed_proposed_and_fully_recorded(
        project: Project) -> None:
    (project.root / "scenario.json").write_text('{"delay_ms": 200}')
    r = router(
        call("read_file", path="scenario.json"),
        call("write_file", path="sim.py",
             content="import json\nprint(json.dumps({'success_rate': 0.97}))\n"),
        call("run", argv=["python", "sim.py"]),
        call("propose", kind="experiment_result", title="Handoff under delay",
             summary="success 0.97 (simulation only)", files=["sim.py"]),
        say("Simulated handoff: 0.97 success; simulation only."))
    task = WorkTask(task_id="sim-1", kind=WorkKind.SIMULATION, goal="handoff under delay",
                    requirements=("REQ06",), inputs=("scenario.json",))
    result = await agent(project, r).run(task)

    assert result.status is WorkStatus.COMPLETED, result.problems
    assert result.tool_calls == 4 and result.model_calls == 5
    (proposal,) = result.proposals
    assert proposal.requirements == ("REQ06",) and proposal.artifacts
    act = project.activities(kind=ActivityKind.SIMULATION)[0]
    assert act.status is ActivityStatus.SUCCEEDED and "REQ06" in act.requirements
    assert any("scenario.json" in u for u in act.used)
    kinds = [e.kind for e in project.ledger]
    for k in ("work.sandbox", "work.model_turn", "work.tool_call", "work.proposed"):
        assert k in kinds
    calls = project.ledger.query(kind="work.tool_call")
    assert all(e.trace_id == result.trace_id for e in calls)
    assert calls[2].data["data"]["exit_code"] == 0
    assert project.ledger.verify().ok


async def test_tools_outside_the_task_and_bad_arguments_are_refused(project: Project) -> None:
    def check(transcript: list[dict[str, object]]) -> object:
        results = transcript[-1]["results"]  # type: ignore[index]
        assert "not available" in results[0]["content"] and results[0]["is_error"]
        return call("read_project", what="everything")

    def check2(transcript: list[dict[str, object]]) -> object:
        results = transcript[-1]["results"]  # type: ignore[index]
        assert "invalid arguments" in results[0]["content"]
        return say("stopping")

    r = router(call("run", argv=["python", "-c", "print(1)"]), check, check2)
    task = WorkTask(task_id="m-1", kind=WorkKind.MAINTENANCE, goal="review gearbox wear")
    result = await agent(project, r).run(task)
    assert result.status is WorkStatus.COMPLETED
    oks = [e.data["ok"] for e in project.ledger.query(kind="work.tool_call")]
    assert oks == [False, False]


async def test_robot_actions_need_approval_and_go_through_admission(
        project: Project, make_env: MakeEnv) -> None:
    env = make_env()
    robot_delegation = env.runtime.authority.grant(Delegation(
        delegation_id="dlg-work", grantor="operator:root", grantee="agent:work",
        robots=("carrier-1",), skills=("navigate_to",)))
    ask = call("request_robot_skill", robot_id="carrier-1", skill_id="navigate_to",
               args={"x": 0.5, "y": 0.0})
    task = WorkTask(task_id="d-1", kind=WorkKind.DIAGNOSIS, goal="check drive",
                    tools=("request_robot_skill",), robot_id="carrier-1")

    denied = await agent(project, router(ask, say("could not test")), runtime=env.runtime,
                         robot_delegation_id="dlg-work").run(task)
    assert denied.status is WorkStatus.COMPLETED
    assert env.adapter.executed == []  # default approver denies
    assert project.ledger.query(kind="work.approval")[0].data["approved"] is False

    async def operator(req: object) -> Decision:
        return Decision(True, "operator:alice", "bench test ok")

    approved = await agent(project, router(ask, say("drive verified")), runtime=env.runtime,
                           robot_delegation_id=robot_delegation.delegation_id,
                           approver=operator).run(task.evolve(task_id="d-2"))
    assert approved.status is WorkStatus.COMPLETED
    assert len(env.adapter.executed) == 1  # executed via Runtime admission
    last_call = project.ledger.query(kind="work.tool_call")[-1]
    assert last_call.data["ok"] is True


@pytest.mark.req("F07")
async def test_restricted_data_never_reaches_a_cloud_model(project: Project) -> None:
    r = router(say("hi"), location=Location.CLOUD)
    result = await agent(project, r).run(WorkTask(
        task_id="r-1", kind=WorkKind.RESEARCH, goal="analyse logs",
        data_classification=DataClass.RESTRICTED))
    assert result.status is WorkStatus.FAILED
    assert "no registered model satisfies the policy" in result.problems[0]

    on_robot = ModelRouter()
    on_robot.register(ScriptedChat([say("done on robot")], name="duck-llm",
                                   location=Location.ON_ROBOT, robot_id="duck-a"))
    ok = await agent(project, on_robot).run(WorkTask(
        task_id="r-2", kind=WorkKind.RESEARCH, goal="analyse logs", robot_id="duck-a",
        data_classification=DataClass.RESTRICTED,
        models=ModelConstraints(allowed_locations=(Location.ON_ROBOT,))))
    assert ok.status is WorkStatus.COMPLETED and ok.final_text == "done on robot"


async def test_budget_refusal_and_sandbox_refusal(project: Project) -> None:
    looping = [call("list_files", f"c{i}") for i in range(10)]
    r = await agent(project, router(*looping)).run(WorkTask(
        task_id="b-1", kind=WorkKind.RESEARCH, goal="x",
        budget=WorkBudget(max_model_calls=3)))
    assert r.status is WorkStatus.BUDGET_EXHAUSTED and r.model_calls == 3

    from agentic_robotics.agents.providers import StopReason, Turn
    refusal = Turn(text="", tool_calls=[], stop=StopReason.REFUSAL, model="m",
                   refusal_category="bio")
    r2 = await agent(project, router(refusal)).run(WorkTask(
        task_id="b-2", kind=WorkKind.RESEARCH, goal="x"))
    assert r2.status is WorkStatus.REFUSED and "bio" in r2.problems[0]

    r3 = await agent(project, router(say("x"))).run(WorkTask(
        task_id="b-3", kind=WorkKind.RESEARCH, goal="x",
        sandbox=SandboxPolicy(require_enforced=("network",))))
    assert r3.status is WorkStatus.SANDBOX_REFUSED
    failed = {a.params["task_id"]: a.status for a in project.activities()}
    assert all(failed[t] is ActivityStatus.FAILED for t in ("b-1", "b-2", "b-3"))


async def test_simulation_agent_runs_the_reference_scenario_in_the_sandbox(
        project: Project) -> None:
    def summarize(transcript: list[dict[str, object]]) -> object:
        report = transcript[-1]["results"][0]["content"]  # type: ignore[index]
        assert "mission inspect-part-7: verified" in report
        return say("handoff scenario verified in simulation")

    result = await agent(project, router(call("run_scenario", name="handoff"),
                                         summarize)).run(
        WorkTask(task_id="s-1", kind=WorkKind.SIMULATION, goal="run handoff",
                 sandbox=SandboxPolicy(max_wall_s=120)))
    assert result.status is WorkStatus.COMPLETED, result.problems


# -- review and promotion ----------------------------------------------------------------------
@pytest.mark.req("REQ17", "REQ16")
async def test_maintenance_plans_are_proposals_decided_by_someone_else(
        project: Project) -> None:
    r = router(call("propose_maintenance", robot_id="arm-1", components=["joint3-gearbox"],
                    reason="backlash trend", procedure="inspect and replace harmonic drive",
                    urgency="high", return_to_service_checks=["backlash", "full range"]),
               say("proposed gearbox service"))
    result = await agent(project, r).run(WorkTask(
        task_id="mt-1", kind=WorkKind.MAINTENANCE, goal="gearbox health", robot_id="arm-1"))
    (plan,) = result.proposals
    assert plan.kind.value == "maintenance_plan"
    assert project.activities(kind=ActivityKind.MAINTENANCE)[0].generated != ()
    # A plan is not a record of maintenance done.
    assert not project.ledger.query(kind="entity.replaced")

    with pytest.raises(AuthorityError, match="cannot be decided by the agent"):
        decide(project, plan.proposal_id, by="agent:work", approve=True)

    auth = AuthorityService(project_clock(), ["operator:root"])
    auth.grant(Delegation(delegation_id="dlg-review", grantor="operator:root",
                          grantee="engineer:sam", robots=("workbench",),
                          operations=("promote",)))
    with pytest.raises(AuthorityError):
        decide(project, plan.proposal_id, by="engineer:kim", approve=True,
               authority=auth, delegation_id="dlg-review")
    decided = decide(project, plan.proposal_id, by="engineer:sam", approve=True,
                     reason="schedule Thursday", authority=auth, delegation_id="dlg-review")
    assert decided.status is ProposalStatus.APPROVED
    assert [p.status for p in proposals(project)] == [ProposalStatus.APPROVED]
    assert project.activities(kind=ActivityKind.DECISION)[-1].outcome == "approved"


@pytest.mark.req("REQ18")
def test_cli_runs_work_against_a_catalog_model_and_reviews_proposals(
        project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    from agentic_robotics.cli import main as cli

    from .httpfake import FakeServer

    def reply(content: str | None, tool_calls: list[dict[str, object]] | None = None
              ) -> tuple[int, dict[str, object]]:
        msg: dict[str, object] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return 200, {"model": "qwen", "choices": [{"message": msg, "finish_reason":
                                                    "tool_calls" if tool_calls else "stop"}]}

    propose_args = json.dumps({"kind": "report", "title": "Gearbox review",
                               "summary": "wear within limits"})
    with FakeServer([reply(None, [{"id": "c1", "type": "function", "function": {
            "name": "propose", "arguments": propose_args}}]), reply("reviewed")]) as srv:
        catalog = project.air_dir / "models.json"
        catalog.write_text(json.dumps({"$schema": "ar.model_catalog/1.0", "models": [{
            "name": "local", "provider": "openai_compatible", "model": "qwen",
            "location": "local_host", "data_leaves_device": False,
            "endpoint": srv.url + "/v1"}]}))
        task = project.root / "task.json"
        WorkTask(task_id="cli-1", kind=WorkKind.MAINTENANCE, goal="review gearbox").save(task)
        proj = ["--project", str(project.root)]
        assert cli(["models", *proj]) == 0
        assert "data stays on device" in capsys.readouterr().out
        assert cli(["work", "run", str(task), *proj]) == 0
        out = capsys.readouterr().out
        assert "cli-1: completed" in out and "Gearbox review" in out
    proposal_id = proposals(project)[0].proposal_id
    assert cli(["work", "decide", proposal_id, "approve", "--by", "engineer:sam", *proj]) == 0
    assert cli(["work", "proposals", "--status", "approved", *proj]) == 0
    assert proposal_id in capsys.readouterr().out


def project_clock():  # type: ignore[no-untyped-def]
    from agentic_robotics import MonotonicClock
    return MonotonicClock()
