"""Stage 3 evidence: two-robot coordination.

Exit evidence (baseline section 20, stage 3): contention, duplicates, delay,
disconnection, uncertain outcome and handoff scenarios.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics import Command, SkillRegistry
from agentic_robotics.coordination import (
    CoordinationMessage,
    CoordinatorTimeouts,
    LocalTransport,
    MessageKind,
    Mission,
    MissionState,
    MissionTask,
    TaskState,
    Team,
    check_task_transition,
)
from agentic_robotics.core import InvalidTransition, PlanError, new_id
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set
from agentic_robotics.integrations.fake_arm import FakeArmAdapter
from agentic_robotics.integrations.world import FakeWorld
from agentic_robotics.provenance import Project
from agentic_robotics.provenance.explain import explain, from_ledger
from agentic_robotics.provenance.traceability import baseline_requirements
from agentic_robotics.scenarios import handoff_mission, handoff_robots

pytestmark = pytest.mark.req("REQ06", "REQ09", "G06", "G07")
commitments = pytest.mark.req("REQ07", "REQ10")
uncertain = pytest.mark.req("REQ14", "G14")

TIMEOUTS = CoordinatorTimeouts(bid_s=0.3, ready_s=0.3, ack_s=0.3, result_margin_s=1.0,
                               query_s=0.3, query_retries=2, commit_retries=2,
                               reservation_wait_s=5.0)
MakeTeam = Callable[..., Team]


@pytest.fixture
async def make_team() -> AsyncIterator[MakeTeam]:
    teams: list[Team] = []
    worlds: list[str] = []

    def build(carriers: int = 1, transport: LocalTransport | None = None,
              project: Project | None = None, **carrier_config: Any) -> Team:
        world = new_id("w")
        worlds.append(world)
        team = Team(handoff_robots(world, carriers=carriers, tick_s=0.001, sim_dt_s=0.5,
                                   carrier_config=carrier_config),
                    SkillRegistry(fake_skill_set().skills),
                    transport=transport or LocalTransport(), project=project,
                    timeouts=TIMEOUTS)
        teams.append(team)
        return team

    yield build
    for t in teams:
        await t.stop()
    for w in worlds:
        FakeWorld.reset(w)


def carrier(team: Team, n: int = 1) -> FakeRobotAdapter:
    a = team.robot(f"carrier-{n}").adapter
    assert isinstance(a, FakeRobotAdapter)
    return a


def arm(team: Team) -> FakeArmAdapter:
    a = team.robot("arm-1").adapter
    assert isinstance(a, FakeArmAdapter)
    return a


def kinds(team: Team, prefix: str = "coordination.") -> list[tuple[str, str]]:
    return [(e.kind, e.subject) for e in team.coordinator.ledger if e.kind.startswith(prefix)]


def transport(team: Team) -> LocalTransport:
    assert isinstance(team.transport, LocalTransport)
    return team.transport


# -- handoff ---------------------------------------------------------------------------------
@commitments
async def test_handoff_mission_verified_end_to_end(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    plan = await team.plan(handoff_mission())
    assert {a.task_id: a.participant for a in plan.assignments} == {
        "dock_intake": "carrier-1", "load": "carrier-1", "dock_handoff": "carrier-1",
        "unload": "carrier-1", "clear": "carrier-1", "pick": "arm-1", "inspect": "arm-1"}
    result = await team.execute(plan)
    assert result.state is MissionState.VERIFIED, result.summary()

    world = arm(team).world
    assert world.part_location["part-7"] == "arm-1"
    assert arm(team).last_inspection is not None and arm(team).last_inspection["passed"]
    assert len(carrier(team).executed) == 5 and len(arm(team).executed) == 2

    # Commitments and outcomes are ledgered; the chain verifies.
    ledger = team.coordinator.ledger
    assert ledger.verify().ok
    assert [k for k, _ in kinds(team)].count("coordination.committed") == 7
    assert [k for k, _ in kinds(team)].count("coordination.verified") == 7

    # The arm reserved the handoff station only after the carrier physically left it.
    order = kinds(team)
    released = order.index(("coordination.released", "handoff"))
    pick_commit = order.index(("coordination.committed", "inspect-part-7/pick"))
    assert released < pick_commit
    commits = {e.subject: e.data for e in ledger.query(kind="coordination.committed")}
    assert commits["inspect-part-7/unload"]["fencing"] == {"handoff": 1}
    assert commits["inspect-part-7/pick"]["fencing"] == {"handoff": 2}


async def test_acknowledgements_are_selective(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    await team.execute(await team.plan(handoff_mission()))
    to_arm = [m for m in transport(team).log if "arm-1" in m.recipients]
    assert {m.task_id for m in to_arm} == {"pick", "inspect"}
    to_carrier = [m for m in transport(team).log if "carrier-1" in m.recipients]
    assert "pick" not in {m.task_id for m in to_carrier}


async def test_robot_commands_join_the_mission_trace(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    plan = await team.plan(handoff_mission())
    result = await team.execute(plan)
    assert result.trace_id == plan.trace_id
    in_trace = team.trace.for_trace(plan.trace_id or "")
    assert sum(1 for e in in_trace if e.kind == "verified") == 7  # robot-local runtimes
    assert any(e.kind == "team_task_committed" for e in in_trace)


async def test_unsupported_task_is_explicit(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    mission = Mission(mission_id="m", goal="weld", tasks=(
        MissionTask("weld", "weld_seam", {}),))
    plan = await team.plan(mission)
    assert plan.unassigned[0].reason == "no_participant_supports_skill"
    with pytest.raises(PlanError, match="unassigned"):
        await team.execute(plan)


async def test_allocation_prefers_lowest_cost_participant(make_team: MakeTeam) -> None:
    team = make_team(carriers=2)  # carrier-1 at x=0, carrier-2 at x=3; intake at (2, 1)
    await team.start()
    plan = await team.plan(handoff_mission())
    carrier_tasks = {a.participant for a in plan.assignments
                     if a.task_id not in ("pick", "inspect")}
    assert carrier_tasks == {"carrier-2"}
    assert (await team.execute(plan)).verified
    assert carrier(team, 1).executed == []


# -- contention --------------------------------------------------------------------------------
async def test_contention_for_exclusive_station_is_serialized(make_team: MakeTeam) -> None:
    team = make_team(carriers=2)
    await team.start()
    T = MissionTask
    mission = Mission(mission_id="contend", goal="both carriers visit handoff", tasks=(
        T("c1_dock", "dock_at", {"station": "handoff"}, assign_to="carrier-1"),
        T("c1_leave", "undock", depends_on=("c1_dock",), colocated_with="c1_dock"),
        T("c2_dock", "dock_at", {"station": "handoff"}, assign_to="carrier-2"),
        T("c2_leave", "undock", depends_on=("c2_dock",), colocated_with="c2_dock"),
    ))
    result = await team.execute(await team.plan(mission))
    assert result.verified, result.summary()  # a physical collision would have failed a dock
    commits = team.coordinator.ledger.query(kind="coordination.committed")
    docks = [e for e in commits if e.subject.endswith("_dock")]
    assert sorted(e.data["fencing"]["handoff"] for e in docks) == [1, 2]
    order = [(e.kind, e.subject) for e in team.coordinator.ledger]
    second = max(docks, key=lambda e: e.data["fencing"]["handoff"])
    assert order.index(("coordination.released", "handoff")) < order.index(
        ("coordination.committed", second.subject))


async def test_stale_owner_is_fenced_at_the_executor(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    await team.execute(await team.plan(handoff_mission()))
    team.resources.reserve("handoff", "agent:arm-1")  # arm holds it now (token 3)
    rt = team.runtimes["carrier-1"]
    stale = rt.submit(Command(
        command_id=new_id("cmd"), robot_id="carrier-1", skill_id="dock_at",
        issued_by="agent:carrier-1", delegation_id="dlg-carrier-1",
        authority_revision=team.authority.revision, args={"station": "handoff"},
        fencing={"handoff": 1}))
    assert (await stale.wait()).reason == "stale_fencing_token"


# -- duplicates and delay ----------------------------------------------------------------------
async def test_duplicated_messages_never_duplicate_execution(make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    plan = await team.plan(handoff_mission())
    for endpoint in ("coordinator", "carrier-1", "arm-1"):
        net.link(endpoint).duplicate = True
    result = await team.execute(plan)
    assert result.verified, result.summary()
    assert net.stats.duplicated > 0
    assert len(carrier(team).executed) == len(set(carrier(team).executed)) == 5
    assert len(arm(team).executed) == 2
    assert "duplicate_message_ignored" in team.trace.kinds()


async def test_message_delay_within_timeouts(make_team: MakeTeam) -> None:
    team = make_team(transport=LocalTransport(delay_s=0.03))
    await team.start()
    assert (await team.run(handoff_mission())).verified


async def test_slow_participant_is_reallocated_without_double_execution(
        make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(carriers=2, transport=net)
    await team.start()
    mission = Mission(mission_id="one", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "intake"}),))
    plan = await team.plan(mission)
    assert plan.assignee("dock") == "carrier-2"
    net.link("carrier-2").delay_s = 1.0  # readiness reply will miss the 0.3 s window
    result = await team.execute(plan)
    rec = result.tasks["dock"]
    assert result.verified and rec.assignee == "carrier-1" and rec.revision == 2
    assert carrier(team, 2).executed == []
    assert team.coordinator.ledger.query(kind="coordination.reallocated")


async def test_replies_for_a_superseded_revision_are_ignored(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    coord = team.coordinator
    late = CoordinationMessage(message_id=new_id("msg"), conversation_id="m", task_id="t",
                               task_revision=1, sender="carrier-1", recipients=("coordinator",),
                               kind=MessageKind.READY, sent_at=0.0)
    await coord.handle(late)
    got = await coord._collect("m", "t", 2, {"carrier-1"}, {MessageKind.READY}, 0.05)
    assert got == {}
    assert "stale_revision_ignored" in team.trace.kinds()


# -- disconnection and uncertain outcomes ---------------------------------------------
@uncertain
async def test_disconnection_before_commitment_fails_cleanly(make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    plan = await team.plan(handoff_mission())
    net.disconnect("arm-1")
    result = await team.execute(plan)
    assert result.state is MissionState.FAILED
    assert result.tasks["clear"].state is TaskState.VERIFIED
    assert result.tasks["pick"].state is TaskState.FAILED
    assert result.tasks["pick"].reason == "no_ready_participant"
    assert result.tasks["inspect"].state is TaskState.CANCELLED
    assert arm(team).executed == []
    assert team.resources.holder("handoff") is None  # nothing left held


@uncertain
async def test_lost_result_is_unresolved_then_reconciled_on_resume(make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    plan = await team.plan(handoff_mission())
    net.link("arm-1").drop_kinds = {MessageKind.RESULT, MessageKind.STATUS}
    result = await team.execute(plan)
    assert result.state is MissionState.UNRESOLVED, result.summary()
    pick = result.tasks["pick"]
    assert pick.state is TaskState.UNRESOLVED and pick.reason == "result_lost"
    holder = team.resources.holder("handoff")
    assert holder is not None and holder.holder == "agent:arm-1"  # kept: outcome unknown
    assert result.tasks["inspect"].state is TaskState.ACCEPTED   # not run on a guess

    net.link("arm-1").drop_kinds = set()
    resumed = await team.resume(result)
    assert resumed.verified, resumed.summary()
    assert len(arm(team).executed) == 2  # pick ran once; reconciliation only observed


@uncertain
async def test_robot_side_uncertainty_is_reconciled_by_observation(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    carrier(team).inject("lose_link_after_motion")
    result = await team.run(handoff_mission())
    assert result.verified, result.summary()
    unresolved = team.coordinator.ledger.query(kind="coordination.unresolved")
    assert {e.subject.split("/")[1] for e in unresolved} == {"dock_intake", "dock_handoff"}
    assert len(carrier(team).executed) == 5  # no re-dispatch


@commitments
async def test_lost_acknowledgements_are_retried_and_deduplicated(make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    mission = Mission(mission_id="ack", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "intake"}),))
    plan = await team.plan(mission)
    net.link("carrier-1").drop_kinds = {MessageKind.ACK, MessageKind.RESULT}
    result = await team.execute(plan)
    assert result.verified, result.summary()
    assert "team_commit_retry" in team.trace.kinds()
    assert len(carrier(team).executed) == 1


def test_team_task_state_machine() -> None:
    check_task_transition(TaskState.COMMITTED, TaskState.UNRESOLVED)
    check_task_transition(TaskState.UNRESOLVED, TaskState.VERIFIED)
    with pytest.raises(InvalidTransition):
        check_task_transition(TaskState.ACCEPTED, TaskState.COMMITTED)  # skips reserve/ready
    with pytest.raises(InvalidTransition):
        check_task_transition(TaskState.VERIFIED, TaskState.FAILED)


# -- evidence ------------------------------------------------------------------------------------
@pytest.mark.req("REQ10", "REQ15", "F01")
async def test_team_evidence_lands_in_project_ledger(make_team: MakeTeam,
                                                     tmp_path: Path) -> None:
    project = Project.init(tmp_path, requirements=baseline_requirements())
    team = make_team(project=project)
    await team.start()
    result = await team.run(handoff_mission())
    assert result.verified
    kinds_in_ledger = {e.kind for e in project.ledger}
    assert {"coordination.committed", "coordination.verified", "runtime.team_task_verified",
            "runtime.verified", "runtime.reservation_held"} <= kinds_in_ledger
    assert project.ledger.verify().ok
    cmd = result.tasks["pick"].command_id or ""
    text = "\n".join(explain(from_ledger(project.ledger), cmd))
    assert "outcome: verified" in text and "pick_part on arm-1" in text


def test_example_mission_matches_reference_scenario() -> None:
    example = Mission.load(Path(__file__).resolve().parents[1] / "examples" / "missions" /
                           "inspect_part.json")
    assert [t.skill_id for t in example.tasks] == [t.skill_id for t in handoff_mission().tasks]
