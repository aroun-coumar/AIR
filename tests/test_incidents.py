"""Incident pipeline: novelty, hypotheses, bounded tests through admission, recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts import ComponentSpec
from agentic_robotics.contracts.skill import Condition, Op
from agentic_robotics.core.errors import AdmissionRejected, AuthorityError
from agentic_robotics.hardware import builtin_catalog
from agentic_robotics.incidents import (
    BoundedTest,
    Detection,
    HypothesisStatus,
    Incident,
    IncidentError,
    IncidentPipeline,
    IncidentState,
    RollbackStrategy,
    TestAction,
    TestBudget,
    TestOutcome,
)
from agentic_robotics.provenance import Project
from agentic_robotics.triage import TriageBook
from agentic_robotics.triage.operations import OperationsTriage, operations_policy

pytestmark = pytest.mark.req("REQ14", "G11")
learning = pytest.mark.req("REQ11", "G13")

STEADY = (Condition("base.estop", Op.EQ, False),
          Condition("battery.charge", Op.GE, 79.0, description="battery at least 79%"))


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path, name="t")


def pipeline(env: Any, project: Project | None) -> IncidentPipeline:
    manifest = env.robot.manifest.evolve(components=(
        ComponentSpec("pack", "battery", "1", type_id="power.battery.li_ion_nmc_pack"),))
    return IncidentPipeline(project=project, runtime=env.runtime, catalog=builtin_catalog(),
                            manifests=[manifest],
                            robot_owned_recovery={"carrier-1": ("undock",)})


def detection(signal: str = "pack_temperature", **kw: Any) -> Detection:
    fields: dict[str, Any] = {"detector": "health", "robot_id": "carrier-1", "signal": signal,
                              "summary": f"{signal} out of limits", "observed_at": 1.0,
                              "component_id": "pack", "evidence": ("health:carrier-1@1",)}
    fields.update(kw)
    return Detection(**fields)


def bounded(hypothesis_id: str, method: tuple[TestAction, ...], **kw: Any) -> BoundedTest:
    fields: dict[str, Any] = {"test_id": "t1", "hypothesis_id": hypothesis_id,
                              "title": "driving drains the battery", "steady_state": STEADY,
                              "method": method,
                              "rollbacks": (TestAction("navigate_to", {"x": 0.0, "y": 0.0}),)}
    fields.update(kw)
    return BoundedTest(**fields)


def run(env: Any, pipe: IncidentPipeline, inc: Incident, t: BoundedTest) -> Any:
    return pipe.run_test(inc.incident_id, t, issued_by="agent:task",
                         delegation_id=env.delegation.delegation_id)


# -- detection and novelty --------------------------------------------------------------
def test_known_failure_classes_come_from_the_domain_packs(make_env: Callable[..., Any],
                                                          project: Project) -> None:
    pipe = pipeline(make_env(), project)
    inc = pipe.open(detection())
    assert not inc.novel
    # scoped to the battery's domains (D02, D14): not D19's temperature excursion
    assert inc.known_classes == ("D02.thermal_runaway",)
    novel = pipe.open(detection("squeak", component_id=None, summary="unknown squeak"))
    assert novel.novel and novel.known_classes == ()
    explicit = pipe.open(detection("x", failure_class="D02.over_discharge"))
    assert explicit.known_classes == ("D02.over_discharge",)
    assert pipe.open(detection("y", failure_class="D99.made_up")).novel
    assert project.ledger.query(kind="incident.opened")[0].data["novel"] is False


def test_recurrence_folds_into_the_active_incident(make_env: Callable[..., Any],
                                                   project: Project) -> None:
    pipe = pipeline(make_env(), project)
    a = pipe.open(detection())
    b = pipe.open(detection(evidence=("health:carrier-1@2",)))
    assert a.incident_id == b.incident_id and b.occurrences == 2
    assert b.evidence == ("health:carrier-1@1", "health:carrier-1@2")
    assert pipe.get(a.incident_id[:10]).incident_id == a.incident_id
    with pytest.raises(IncidentError):
        pipe.get("inc_nope")


@learning
def test_closing_learns_the_signature(make_env: Callable[..., Any], project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection("squeak", component_id=None))
    assert inc.novel
    with pytest.raises(AuthorityError):
        pipe.close(inc.incident_id, by="agent:diag", resolution="looks fine")
    with pytest.raises(IncidentError):
        pipe.close(inc.incident_id, by="person:ana", resolution=" ")
    pipe.close(inc.incident_id, by="person:ana", resolution="loose cover, tightened")
    again = pipe.open(detection("squeak", component_id=None))
    assert again.incident_id != inc.incident_id
    assert not again.novel and again.known_classes == (f"learned:{inc.incident_id}",)
    replayed = pipeline(env, project)                      # rebuilt from the ledger
    assert replayed.get(inc.incident_id).state is IncidentState.CLOSED
    assert not replayed.open(detection("squeak", component_id=None)).novel


def test_agent_hypotheses_are_non_authoritative(make_env: Callable[..., Any],
                                                project: Project) -> None:
    pipe = pipeline(make_env(), project)
    inc = pipe.open(detection())
    h1 = pipe.propose(inc.incident_id, "fan stalled", proposed_by="agent:diag")
    h2 = pipe.propose(inc.incident_id, "ambient too hot", proposed_by="model:gpt")
    h3 = pipe.propose(inc.incident_id, "cell defect", proposed_by="person:ana",
                      failure_class="D02.thermal_runaway")
    assert (h1.authoritative, h2.authoritative, h3.authoritative) == (False, False, True)
    current = pipe.get(inc.incident_id)
    assert current.state is IncidentState.INVESTIGATING
    assert all(h.status is HypothesisStatus.PROPOSED for h in current.hypotheses)
    with pytest.raises(AuthorityError):
        pipe.handoff(inc.incident_id, to="role:operator", by="agent:diag", reason="tired")


# -- bounded tests ------------------------------------------------------------------------
async def test_supported_hypothesis_with_rollback_through_admission(
        make_env: Callable[..., Any], project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection("state_of_charge", summary="battery drops fast"))
    h = pipe.propose(inc.incident_id, "driving drains the battery", proposed_by="agent:diag")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 4.0, "y": 0.0}, timeout_s=10),)))
    assert result.outcome is TestOutcome.SUPPORTED and result.deviated is True
    assert [c.split(":")[0] for c in result.commands] == ["method", "rollback"]
    assert all(c.endswith(":verified") for c in result.commands)
    submitted = [e.subject for e in env.runtime.trace if e.kind == "submitted"]
    assert [c.split(":")[1] for c in result.commands] == submitted   # all via Runtime.submit
    current = pipe.get(inc.incident_id)
    hyp = current.hypothesis(h.hypothesis_id)
    assert hyp.status is HypothesisStatus.SUPPORTED and not hyp.authoritative
    assert f"test:{result.test_id}" in hyp.evidence
    kinds = [e.kind for e in project.ledger.query(prefix="incident.")]
    assert kinds[-6:] == ["incident.test_started", "incident.steady_state",
                          "incident.test_command", "incident.steady_state",
                          "incident.rollback", "incident.test_finished"]
    assert "incident_test_finished" in env.runtime.trace.kinds()


async def test_refuted_without_deviation_skips_rollback(make_env: Callable[..., Any],
                                                        project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "any motion drains the battery", proposed_by="person:a")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 0.2, "y": 0.0}),)))
    assert result.outcome is TestOutcome.REFUTED and result.deviated is False
    assert len(result.commands) == 1                     # deviated strategy: no rollback
    assert pipe.get(inc.incident_id).hypothesis(h.hypothesis_id).status \
        is HypothesisStatus.REFUTED


async def test_method_never_runs_without_steady_state(make_env: Callable[..., Any],
                                                      project: Project) -> None:
    env = make_env(battery_pct=50.0)
    pipe = pipeline(env, project)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 1.0, "y": 0.0}),)))
    assert result.outcome is TestOutcome.NOT_RUN and "steady state" in result.reason
    assert env.adapter.executed == []
    assert pipe.get(inc.incident_id).hypothesis(h.hypothesis_id).status \
        is HypothesisStatus.PROPOSED


async def test_stale_steady_state_observations_do_not_count(make_env: Callable[..., Any],
                                                            project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    env.adapter.inject("freeze_sensors")
    await asyncio.sleep(0.01)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 1.0, "y": 0.0}),), max_input_age_s=0.001))
    assert result.outcome is TestOutcome.NOT_RUN
    assert any(r.detail == "observation too old" for r in result.before)
    assert env.adapter.executed == []


async def test_budget_limits_the_method(make_env: Callable[..., Any], project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    many = tuple(TestAction("navigate_to", {"x": float(i), "y": 0.0}) for i in range(3))
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, many,
                                            budget=TestBudget(max_commands=2)))
    assert result.outcome is TestOutcome.NOT_RUN and "budget" in result.reason
    assert env.adapter.executed == []


async def test_rejected_method_is_inconclusive_and_rolls_back(make_env: Callable[..., Any],
                                                              project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 999.0, "y": 0.0}),)))       # admission refuses it
    assert result.outcome is TestOutcome.INCONCLUSIVE and "rejected" in result.reason
    assert result.commands[0].endswith(":rejected")
    assert result.commands[1].startswith("rollback:")
    assert pipe.get(inc.incident_id).hypothesis(h.hypothesis_id).status \
        is HypothesisStatus.INCONCLUSIVE


async def test_unresolved_aborts_without_retry_and_hands_off(make_env: Callable[..., Any],
                                                             project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    env.adapter.inject("lose_link_after_motion")
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    result = await run(env, pipe, inc, bounded(h.hypothesis_id, (
        TestAction("navigate_to", {"x": 1.0, "y": 0.0}),
        TestAction("navigate_to", {"x": 2.0, "y": 0.0})),
        rollback_strategy=RollbackStrategy.ALWAYS))
    assert result.outcome is TestOutcome.ABORTED and "reconcile" in result.reason
    assert len(result.commands) == 1 and result.commands[0].endswith(":unresolved")
    assert len(env.adapter.executed) == 1                  # no retry, no blind rollback
    current = pipe.get(inc.incident_id)
    assert current.state is IncidentState.HANDED_OFF and current.handed_off_to == "role:operator"
    book = TriageBook(project, policy=operations_policy())
    OperationsTriage(book).incident(current)
    [item] = book.items()
    assert item.rule == "incident-aborted" and item.severity.value == "safety"
    with pytest.raises(IncidentError):
        await run(env, pipe, current, bounded(h.hypothesis_id, (
            TestAction("navigate_to", {"x": 1.0, "y": 0.0}),)))


async def test_tests_need_a_runtime(project: Project) -> None:
    pipe = IncidentPipeline(project=project)
    inc = pipe.open(detection())
    h = pipe.propose(inc.incident_id, "x", proposed_by="person:a")
    with pytest.raises(IncidentError, match=r"Runtime\.submit"):
        await pipe.run_test(inc.incident_id, bounded(h.hypothesis_id, (
            TestAction("navigate_to", {"x": 1.0, "y": 0.0}),)), issued_by="p", delegation_id="d")


# -- recovery -------------------------------------------------------------------------------
async def test_recovery_is_verified_from_observations(make_env: Callable[..., Any],
                                                      project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection())
    ok = await pipe.recover(inc.incident_id, TestAction("navigate_to", {"x": 1.0, "y": 0.0}),
                            issued_by="agent:task", delegation_id=env.delegation.delegation_id,
                            verify=(Condition("battery.charge", Op.GE, 99.0),))
    assert not ok                                          # command verified, condition not
    assert pipe.get(inc.incident_id).state is IncidentState.OPEN
    ok = await pipe.recover(inc.incident_id, TestAction("navigate_to", {"x": 0.0, "y": 0.0}),
                            issued_by="agent:task", delegation_id=env.delegation.delegation_id,
                            verify=(Condition("base.estop", Op.EQ, False),))
    assert ok and pipe.get(inc.incident_id).state is IncidentState.RECOVERED
    assert len(pipe.get(inc.incident_id).recovery) == 2


async def test_robot_owned_recovery_is_observed_never_commanded(
        make_env: Callable[..., Any], project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    inc = pipe.open(detection("fallen", component_id=None, summary="robot fell"))
    with pytest.raises(AdmissionRejected) as exc:
        await pipe.recover(inc.incident_id, TestAction("undock"), issued_by="agent:task",
                           delegation_id=env.delegation.delegation_id)
    assert exc.value.reason == "robot_owned_recovery"
    assert env.adapter.executed == []
    assert project.ledger.query(kind="incident.recovery_refused")
    assert not pipe.observe_recovery(inc.incident_id, (Condition("base.estop", Op.EQ, True),))
    assert pipe.observe_recovery(inc.incident_id, (Condition("base.estop", Op.EQ, False),))
    assert pipe.get(inc.incident_id).state is IncidentState.RECOVERED


async def test_unresolved_recovery_hands_off(make_env: Callable[..., Any],
                                             project: Project) -> None:
    env = make_env()
    pipe = pipeline(env, project)
    env.adapter.inject("lose_link_after_motion")
    inc = pipe.open(detection())
    assert not await pipe.recover(inc.incident_id, TestAction("navigate_to",
                                                              {"x": 1.0, "y": 0.0}),
                                  issued_by="agent:task",
                                  delegation_id=env.delegation.delegation_id)
    current = pipe.get(inc.incident_id)
    assert current.state is IncidentState.HANDED_OFF
    assert current.recovery[0].endswith(":unresolved")


def test_bounded_test_contract_is_strict() -> None:
    from agentic_robotics.core.errors import SchemaError
    with pytest.raises(SchemaError):
        BoundedTest(test_id="t", hypothesis_id="h", title="x", steady_state=(),
                    method=(TestAction("a"),))
    with pytest.raises(SchemaError):
        BoundedTest(test_id="t", hypothesis_id="h", title="x", steady_state=STEADY, method=())
    t = bounded("h", (TestAction("navigate_to", {"x": 1.0}),))
    assert BoundedTest.from_json(t.to_json()) == t
    with pytest.raises(SchemaError):
        BoundedTest.from_dict({**t.to_dict(), "surprise": 1})


# -- CLI ------------------------------------------------------------------------------------
def test_incident_cli(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(project.root)
    assert main(["incident", "open", "--project", root, "--robot", "carrier-1",
                 "--detector", "operator", "--signal", "squeak", "--summary", "odd squeak",
                 "--triage"]) == 0
    line = capsys.readouterr().out
    assert "NOVEL" in line
    iid = line.split()[0]
    assert main(["incident", "propose", iid, "--project", root, "--statement", "loose cover",
                 "--by", "agent:diag"]) == 0
    assert "non-authoritative" in capsys.readouterr().out
    assert main(["incident", "show", iid, "--project", root]) == 0
    assert "[non-authoritative]" in capsys.readouterr().out
    assert main(["incident", "handoff", iid, "--project", root, "--to", "person:ana",
                 "--by", "person:lead", "--reason", "needs eyes", "--triage"]) == 0
    capsys.readouterr()
    assert main(["incident", "list", "--project", root]) == 0
    assert iid not in capsys.readouterr().out             # handed off: not active
    assert main(["incident", "list", "--project", root, "--all", "--json"]) == 0
    assert iid in capsys.readouterr().out
    assert main(["incident", "close", iid, "--project", root, "--by", "person:ana",
                 "--resolution", "tightened"]) == 0
    assert main(["incident", "close", iid, "--project", root, "--by", "agent:x",
                 "--resolution", "x"]) == 2
    kinds = {e.data["item"]["kind"] for e in Project.open(root).ledger.query(
        kind="triage.opened")}
    assert kinds == {"incident.novel", "incident.handed_off"}
