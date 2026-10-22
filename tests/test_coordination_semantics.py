"""Coordination semantics adopted from VDA 5050, ROS 2 actions, Open-RMF, openTCS and
zenoh (ADR 0018): cancellation and instant actions, released prefix vs horizon with
order-update acceptance rules, liveness, per-sender sequence numbers, HLC timestamps
and graded robot trust."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from agentic_robotics import ManualClock, SkillRegistry
from agentic_robotics.contracts.resources import Occupancy
from agentic_robotics.coordination import (
    Acceptance,
    CancelMode,
    ClockDriftError,
    ConnectionState,
    CoordinationMessage,
    CoordinatorTimeouts,
    HlcTimestamp,
    HybridLogicalClock,
    LivenessMonitor,
    LocalTransport,
    MessageKind,
    Mission,
    MissionState,
    MissionTask,
    OrderBook,
    OrderStep,
    OrderUpdate,
    SeqStatus,
    SeqTracker,
    TaskState,
    Team,
    TeamPlan,
    TrustLevel,
    check_task_transition,
)
from agentic_robotics.core import InvalidTransition, SchemaError, new_id
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set
from agentic_robotics.integrations.world import FakeWorld
from agentic_robotics.scenarios import EXPECTED, handoff_mission, handoff_robots, run_handoff

pytestmark = pytest.mark.req("REQ06", "REQ09", "G06", "G07")
cancellation = pytest.mark.req("REQ17", "REQ07", "REQ10")
liveness = pytest.mark.req("G14", "REQ12")

TIMEOUTS = CoordinatorTimeouts(bid_s=0.3, ready_s=0.3, ack_s=0.3, result_margin_s=1.0,
                               query_s=0.3, query_retries=2, commit_retries=2,
                               reservation_wait_s=5.0, cancel_s=0.5, instant_s=0.5)
SLOW = {"tick_s": 0.01, "speed_mps": 0.2}   # driving to a station takes ~0.5 s
MakeTeam = Callable[..., Team]


@pytest.fixture
async def make_team() -> AsyncIterator[MakeTeam]:
    teams: list[Team] = []
    worlds: list[str] = []

    def build(carriers: int = 1, transport: LocalTransport | None = None,
              timeouts: CoordinatorTimeouts = TIMEOUTS, heartbeat_s: float | None = None,
              **carrier_config: Any) -> Team:
        world = new_id("w")
        worlds.append(world)
        team = Team(handoff_robots(world, carriers=carriers, tick_s=0.001, sim_dt_s=0.5,
                                   carrier_config=carrier_config),
                    SkillRegistry(fake_skill_set().skills),
                    transport=transport or LocalTransport(), timeouts=timeouts,
                    heartbeat_s=heartbeat_s)
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


def ledger_kinds(team: Team) -> list[str]:
    return [e.kind for e in team.coordinator.ledger]


async def until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.005)


def state_of(team: Team, mission_id: str, task_id: str) -> TaskState | None:
    rec = team.coordinator.records(mission_id).get(task_id)
    return None if rec is None else rec.state


# -- cancellation ------------------------------------------------------------------------
@cancellation
async def test_cancel_has_its_own_response_and_frees_only_after_confirmed_stop(
        make_team: MakeTeam) -> None:
    team = make_team(**SLOW)
    await team.start()
    plan = await team.plan(handoff_mission())
    run = asyncio.get_running_loop().create_task(team.execute(plan))
    await until(lambda: state_of(team, "inspect-part-7", "dock_intake") is TaskState.EXECUTING)
    report = await team.cancel("inspect-part-7")
    cmd = team.coordinator.records("inspect-part-7")["dock_intake"].command_id or ""
    assert report.responses["carrier-1"][cmd]["accepted"] is True
    assert report.tasks["dock_intake"] == "cancelling"   # not cancelled until confirmed
    result = await run
    assert result.state is MissionState.CANCELLED and not result.verified
    assert all(r.state is TaskState.CANCELLED for r in result.tasks.values())
    history = [s for _, s, _, _ in result.tasks["dock_intake"].history]
    assert history[-2:] == ["cancelling", "cancelled"]
    kinds = ledger_kinds(team)
    assert {"coordination.cancel_requested", "coordination.cancel_response",
            "coordination.cancelled"} <= set(kinds)
    assert "coordination.verified" not in kinds          # a cancel is never a success
    assert team.resources.holder("intake") is None       # stop confirmed, vacant: freed
    assert carrier(team).executed == [cmd]               # nothing else started


@cancellation
async def test_unconfirmed_stop_is_unresolved_and_keeps_the_hold(make_team: MakeTeam) -> None:
    team = make_team(**SLOW)
    await team.start()
    carrier(team).inject("unconfirmed_stop")
    mission = Mission(mission_id="m", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "intake"}),))
    run = asyncio.get_running_loop().create_task(team.run(mission))
    await until(lambda: "dock" in team.coordinator.records("m")
                and state_of(team, "m", "dock") is TaskState.EXECUTING)
    await team.cancel("m")
    result = await run
    assert result.state is MissionState.UNRESOLVED
    assert result.tasks["dock"].state is TaskState.UNRESOLVED
    assert result.tasks["dock"].reason == "stop_unconfirmed"
    held = team.resources.holder("intake")
    assert held is not None and held.holder == "agent:carrier-1"


@cancellation
async def test_uncancellable_work_keeps_executing_and_is_never_called_cancelled(
        make_team: MakeTeam) -> None:
    team = make_team(tick_s=0.02)
    await team.start()
    mission = Mission(mission_id="cal", goal="calibrate", tasks=(
        MissionTask("cal", "calibrate_camera", {}),))
    run = asyncio.get_running_loop().create_task(team.run(mission))
    await until(lambda: "cal" in team.coordinator.records("cal")
                and state_of(team, "cal", "cal") is TaskState.EXECUTING)
    report = await team.cancel("cal")
    assert list(report.rejected.values()) == ["not_interruptible"]
    result = await run
    rec = result.tasks["cal"]
    assert rec.state is TaskState.VERIFIED                # it ran to completion, honestly
    assert "cancel rejected: not interruptible" in [n for *_, n in rec.history]


@cancellation
async def test_kill_differs_from_cancel(make_team: MakeTeam) -> None:
    team = make_team(tick_s=0.02)
    await team.start()
    mission = Mission(mission_id="k", goal="calibrate then dock", tasks=(
        MissionTask("cal", "calibrate_camera", {}),
        MissionTask("dock", "dock_at", {"station": "intake"}, depends_on=("cal",),
                    colocated_with="cal")))
    run = asyncio.get_running_loop().create_task(team.run(mission))
    await until(lambda: "cal" in team.coordinator.records("k")
                and state_of(team, "k", "cal") is TaskState.EXECUTING)
    await team.cancel("k", kill=True)
    result = await run
    # Kill does not wait for uncancellable work: its outcome is unknown, not success.
    assert result.tasks["cal"].state is TaskState.UNRESOLVED
    assert result.tasks["cal"].reason == "killed_stop_unconfirmed"
    assert result.tasks["dock"].state is TaskState.KILLED     # never started: killed
    assert result.state is MissionState.UNRESOLVED
    rt = team.runtimes["carrier-1"]
    await until(lambda: not rt.busy("carrier-1"))   # the calibration really finishes
    resumed = await team.resume(result)
    assert resumed.tasks["cal"].state is TaskState.VERIFIED   # reconciled by observation
    assert resumed.state is MissionState.CANCELLED
    assert "coordination.killed" in ledger_kinds(team)


@cancellation
async def test_released_unstarted_steps_are_confirmed_never_started(
        make_team: MakeTeam) -> None:
    team = make_team(**SLOW)
    await team.start()
    plan = await team.plan(handoff_mission(), release_window=3)
    run = asyncio.get_running_loop().create_task(team.execute(plan))
    await until(lambda: state_of(team, "inspect-part-7", "dock_intake") is TaskState.EXECUTING)
    report = await team.cancel("inspect-part-7")
    reasons = {cid.split("-")[-2]: r["reason"]
               for cid, r in report.responses["carrier-1"].items()}
    assert reasons == {"dock_intake": "stop_requested", "load": "not_started",
                       "dock_handoff": "not_started"}
    result = await run
    assert result.tasks["load"].reason == "not_started"
    assert result.state is MissionState.CANCELLED
    assert team.resources.holder("handoff") is None   # released step's hold freed
    assert len(carrier(team).executed) == 1


def test_cancelling_state_machine() -> None:
    check_task_transition(TaskState.EXECUTING, TaskState.CANCELLING)
    check_task_transition(TaskState.CANCELLING, TaskState.UNRESOLVED)
    check_task_transition(TaskState.CANCELLING, TaskState.EXECUTING)   # cancel rejected
    with pytest.raises(InvalidTransition):
        check_task_transition(TaskState.EXECUTING, TaskState.CANCELLED)  # needs a stop
    with pytest.raises(InvalidTransition):
        check_task_transition(TaskState.CANCELLED, TaskState.VERIFIED)
    assert TaskState.KILLED.terminal and TaskState.KILLED.stopped
    assert not TaskState.CANCELLING.terminal


# -- instant actions ---------------------------------------------------------------------
@cancellation
async def test_instant_pause_resume_and_state_request(make_team: MakeTeam) -> None:
    team = make_team(carriers=2)
    await team.start()
    paused = await team.pause("carrier-2")
    assert paused is not None and paused["accepted"] and paused["paused"]
    state = await team.state("carrier-2")
    assert state is not None and state["paused"] is True
    mission = Mission(mission_id="one", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "intake"}),))
    plan = await team.plan(mission)
    assert plan.assignee("dock") == "carrier-2"           # cheapest
    result = await team.execute(plan)
    rec = result.tasks["dock"]
    assert result.verified and rec.assignee == "carrier-1"  # paused robot is not ready
    assert any("not_ready:paused" in n for *_, n in rec.history)
    resumed = await team.resume_robot("carrier-2")
    assert resumed is not None and resumed["paused"] is False
    state = await team.state("carrier-1")
    assert state is not None
    assert state["orders"]["one:carrier-1"]["outcomes"] == {"dock": "verified"}


# -- released prefix vs horizon --------------------------------------------------------------
def _step(task_id: str) -> OrderStep:
    return OrderStep(task_id=task_id, task_revision=1, command_id=f"cmd-{task_id}",
                     skill_id="dock_at", args={"station": "intake"})


def test_order_acceptance_rules() -> None:
    book = OrderBook("o")
    first = OrderUpdate("o", 1, (_step("a"), _step("b")), None, ("c",))
    assert book.offer(first).acceptance is Acceptance.ACCEPTED
    assert book.offer(first).acceptance is Acceptance.IGNORED_IDENTICAL
    changed = OrderUpdate("o", 1, (_step("a"),), None, ("c",))
    assert book.offer(changed).reason == "conflicting_update"
    assert book.offer(OrderUpdate("o", 0, (_step("z"),))).reason == "outdated"
    assert book.offer(OrderUpdate("o", 2, (_step("c"),), "a")).reason == "stitch_mismatch"
    assert book.offer(OrderUpdate("o", 2, (_step("b"),), "b")).reason == \
        "released_step_changed"
    # On every rejection the current work is unchanged.
    assert [s.task_id for s in book.steps] == ["a", "b"] and book.update_id == 1
    assert book.offer(OrderUpdate("o", 3, (_step("c"),), "b")).acceptance is \
        Acceptance.ACCEPTED   # ids need not be contiguous, only increasing
    book.cancelled = True
    assert book.offer(OrderUpdate("o", 4, (_step("d"),), "c")).reason == "order_cancelled"
    assert book.offer(OrderUpdate("p", 9, ())).reason == "wrong_order"


async def test_participant_rejects_conflicting_update_and_keeps_its_work(
        make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    assert (await team.run(handoff_mission())).verified
    net = team.transport
    assert isinstance(net, LocalTransport)
    p = team.participants["carrier-1"]
    order = p.orders["inspect-part-7:carrier-1"]
    before = [s.task_id for s in order.book.steps]
    bogus = OrderUpdate(order.book.order_id, order.book.update_id, (_step("x"),),
                        order.book.last_released)
    await p.handle(CoordinationMessage(
        message_id=new_id("msg"), conversation_id="inspect-part-7", task_id="x",
        task_revision=1, sender="coordinator", recipients=("carrier-1",),
        kind=MessageKind.COMMIT, sent_at=0.0, update_id=bogus.update_id,
        payload=bogus.payload()))
    nack = [m for m in net.log if m.kind is MessageKind.NACK]
    assert nack and nack[-1].payload["reason"] == "conflicting_update"
    assert [s.task_id for s in order.book.steps] == before
    assert "cmd-x" not in carrier(team).executed


async def test_released_prefix_is_committed_as_one_update(make_team: MakeTeam) -> None:
    team = make_team()
    await team.start()
    plan = await team.plan(handoff_mission(), release_window=3)
    assert plan.release_window == 3
    assert plan.sequence("carrier-1") == ("dock_intake", "load", "dock_handoff", "unload",
                                          "clear")
    result = await team.execute(plan)
    assert result.verified, result.summary()
    commits = team.coordinator.ledger.query(kind="coordination.committed")
    updates = {e.subject.split("/")[1]: e.data["update_id"] for e in commits}
    assert updates["dock_intake"] == updates["load"] == updates["dock_handoff"]
    assert updates["unload"] == updates["clear"] > updates["dock_intake"]
    assert len(carrier(team).executed) == 5


@liveness
async def test_disconnected_robot_finishes_only_the_released_prefix(
        make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net, **SLOW)
    await team.start()
    plan = await team.plan(handoff_mission(), release_window=3)
    run = asyncio.get_running_loop().create_task(team.execute(plan))
    await until(lambda: len(carrier(team).executed) >= 1)
    net.disconnect("carrier-1")
    result = await run
    assert result.state is MissionState.UNRESOLVED
    prefix = ("dock_intake", "load", "dock_handoff")
    assert {t: result.tasks[t].reason for t in prefix} == dict.fromkeys(
        prefix, "connection_broken")
    assert team.coordinator.connection("carrier-1") is ConnectionState.CONNECTION_BROKEN
    for station in ("intake", "handoff"):                  # holds kept while broken
        held = team.resources.holder(station)
        assert held is not None and held.holder == "agent:carrier-1"
    rt = team.runtimes["carrier-1"]
    await until(lambda: len(carrier(team).executed) == 3 and not rt.busy("carrier-1"))
    await asyncio.sleep(0.1)
    assert len(carrier(team).executed) == 3                # horizon never executed
    assert result.tasks["unload"].state is TaskState.ACCEPTED
    assert team.participants["carrier-1"].coordinator_state is \
        ConnectionState.CONNECTION_BROKEN

    net.reconnect("carrier-1")
    assert team.coordinator.connection("carrier-1") is ConnectionState.ONLINE
    resumed = await team.resume(result)
    assert resumed.verified, resumed.summary()
    assert len(carrier(team).executed) == len(set(carrier(team).executed)) == 5
    states = [e.data["new"] for e in team.coordinator.ledger.query(
        kind="coordination.connection")]
    assert states == ["connection_broken", "online"]


@pytest.mark.req("REQ18")
@pytest.mark.parametrize("variant", ["disconnect", "cancel"])
async def test_reference_scenario_variants(variant: str) -> None:
    lines: list[str] = []
    result = await run_handoff(lines.append, variant=variant)
    assert result.state is EXPECTED[variant], "\n".join(lines)


def test_team_plan_release_window_is_validated() -> None:
    with pytest.raises(SchemaError):
        TeamPlan(plan_id="p", mission=handoff_mission(), release_window=0)


# -- liveness, sequence numbers, clocks ------------------------------------------------------
@liveness
def test_liveness_monitor_from_heartbeats_and_transport_facts() -> None:
    clock = ManualClock()
    mon = LivenessMonitor(clock.now, heartbeat_timeout_s=1.0)
    changes: list[tuple[str, str, str]] = []
    mon.on_change(lambda e, _o, n, c: changes.append((e, n.value, c)))
    mon.heard("r1")
    clock.advance(0.5)
    assert mon.check() == []
    clock.advance(1.0)
    assert mon.check() == ["r1"]
    assert mon.state("r1") is ConnectionState.CONNECTION_BROKEN
    mon.heard("r1")
    mon.liveliness("r1", alive=False, graceful=True)
    assert mon.state("r1") is ConnectionState.OFFLINE
    mon.liveliness("r1", alive=False, graceful=False)
    assert changes == [("r1", "connection_broken", "heartbeat_timeout"),
                       ("r1", "online", "heard_from"), ("r1", "offline", "graceful_leave"),
                       ("r1", "connection_broken", "liveliness_lost")]


@liveness
async def test_silent_participant_becomes_broken_without_resetting_the_team(
        make_team: MakeTeam) -> None:
    net = LocalTransport()
    timeouts = CoordinatorTimeouts(**{**TIMEOUTS.__dict__, "heartbeat_timeout_s": 0.15,
                                      "watchdog_s": 0.02})
    team = make_team(carriers=2, transport=net, timeouts=timeouts, heartbeat_s=0.03)
    await team.start()
    await asyncio.sleep(0.1)
    assert team.coordinator.connection("carrier-2") is ConnectionState.ONLINE
    net.link("carrier-2").drop_kinds = {MessageKind.HEARTBEAT}
    await until(lambda: team.coordinator.connection("carrier-2") is
                ConnectionState.CONNECTION_BROKEN, timeout=3.0)
    assert team.coordinator.connection("carrier-1") is ConnectionState.ONLINE
    mission = Mission(mission_id="one", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "intake"}),))
    plan = await team.plan(mission)
    assert plan.assignee("dock") == "carrier-1"   # a broken peer gets no new work
    assert (await team.execute(plan)).verified
    net.link("carrier-2").drop_kinds = set()
    await until(lambda: team.coordinator.connection("carrier-2") is ConnectionState.ONLINE,
                timeout=3.0)
    causes = [e.data["cause"] for e in team.coordinator.ledger.query(
        kind="coordination.connection")]
    assert causes == ["heartbeat_timeout", "heard_from"]


@pytest.mark.req("REQ10")
def test_sequence_tracker_classifies_gaps_duplicates_and_reordering() -> None:
    t = SeqTracker()
    assert t.observe("a", 0).status is SeqStatus.UNSTAMPED
    assert t.observe("a", 1).status is SeqStatus.FIRST
    assert t.observe("a", 2).status is SeqStatus.IN_ORDER
    gap = t.observe("a", 5)
    assert gap.status is SeqStatus.GAP and gap.missing == 2 and gap.expected == 3
    assert t.observe("a", 5).status is SeqStatus.DUPLICATE
    assert t.observe("a", 4).status is SeqStatus.REORDERED
    assert t.observe("b", 3).status is SeqStatus.GAP   # first seen mid-stream


@pytest.mark.req("REQ10")
async def test_messages_carry_per_channel_seq_and_gaps_are_ledgered(
        make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    plan = await team.plan(handoff_mission())
    net.link("arm-1").drop_kinds = {MessageKind.RESULT, MessageKind.STATUS}
    result = await team.execute(plan)
    net.link("arm-1").drop_kinds = set()
    assert (await team.resume(result)).verified
    to_carrier = [m.seq for m in net.log if m.sender == "coordinator"
                  and m.recipients == ("carrier-1",)]
    assert to_carrier == list(range(1, len(to_carrier) + 1))
    assert all(len(m.recipients) == 1 for m in net.log)
    gaps = team.coordinator.ledger.query(kind="coordination.message_gap")
    assert gaps and {e.subject for e in gaps} == {"arm-1"}


@pytest.mark.req("REQ10", "REQ12")
def test_hybrid_logical_clock() -> None:
    pt_a, pt_b = [10.0], [9.0]          # b's physical clock is 1 s behind a's
    a = HybridLogicalClock("a", lambda: pt_a[0])
    b = HybridLogicalClock("b", lambda: pt_b[0], max_offset_s=2.0)
    t1, t2 = a.now(), a.now()
    assert t2 > t1 and t2.l == t1.l and t2.c == t1.c + 1   # same physical time: counter
    pt_a[0] = 5.0                                          # physical clock steps back
    t3 = a.now()
    assert t3 > t2                                         # never runs backwards
    received = b.update(str(t3))
    # Causality survives clock skew: the receive event orders after the send.
    assert (received.l, received.c) > (t3.l, t3.c)
    assert b.now() > received
    assert HlcTimestamp.parse(str(received)) == received
    ahead = HlcTimestamp(int((pt_b[0] + 30) * 1e6), 0, "x")
    with pytest.raises(ClockDriftError):
        b.update(ahead)


async def test_coordination_messages_are_hlc_ordered(make_team: MakeTeam) -> None:
    net = LocalTransport()
    team = make_team(transport=net)
    await team.start()
    assert (await team.run(handoff_mission())).verified
    by_id = {m.message_id: m for m in net.log}
    replies = [m for m in net.log if m.causal_ref in by_id and m.hlc]
    assert replies
    for reply in replies:   # a reply is always after the message it answers
        request = by_id[reply.causal_ref or ""]
        assert request.hlc is not None
        assert HlcTimestamp.parse(reply.hlc or "") > HlcTimestamp.parse(request.hlc)


def test_coordination_message_1_0_documents_still_load() -> None:
    old = {"$schema": "ar.coordination_message/1.0", "message_id": "m",
           "conversation_id": "c", "task_id": "t", "task_revision": 1, "sender": "s",
           "recipients": ["r"], "kind": "commit", "sent_at": 0.0}
    msg = CoordinationMessage.from_dict(old)
    assert msg.seq == 0 and msg.update_id == 0 and msg.hlc is None
    with pytest.raises(SchemaError):
        CoordinationMessage.from_dict({**old, "surprise": 1})


# -- graded trust --------------------------------------------------------------------------------
async def test_trust_levels_gate_work_and_reports(make_team: MakeTeam) -> None:
    team = make_team(carriers=2)
    await team.start()
    coord = team.coordinator
    team.set_trust("carrier-2", TrustLevel.RESPECTED, reason="manual takeover")
    mission = Mission(mission_id="one", goal="dock", tasks=(
        MissionTask("dock", "dock_at", {"station": "handoff"}),))
    plan = await team.plan(mission)
    assert plan.assignee("dock") == "carrier-1"   # respected: no new work
    result = await team.execute(plan)
    assert result.verified
    held = team.resources.holder("handoff")
    assert held is not None and held.holder == "agent:carrier-1"   # docked there

    # NOTICED reports can make occupancy more conservative but never free a resource.
    team.set_trust("carrier-1", TrustLevel.NOTICED)
    coord._observe_occupancy({"handoff": ""}, "carrier-1")
    assert team.resources.occupancy("handoff") is Occupancy.OCCUPIED
    # A respected robot keeps its holds even once the resource reads vacant.
    team.set_trust("carrier-1", TrustLevel.RESPECTED)
    coord._observe_occupancy({"handoff": ""}, "carrier-1")
    coord._release_eligible(result.tasks)
    assert team.resources.holder("handoff") is not None
    team.set_trust("carrier-1", TrustLevel.UTILIZED)
    coord._release_eligible(result.tasks)
    assert team.resources.holder("handoff") is None

    # IGNORED: its messages are discarded.
    team.set_trust("carrier-2", TrustLevel.IGNORED)
    assert await team.state("carrier-2") is None
    assert "report_ignored" in team.trace.kinds()
    assert [e.data["new"] for e in coord.ledger.query(kind="coordination.trust_changed")] == \
        ["respected", "noticed", "respected", "utilized", "ignored"]
    assert CancelMode("kill") is CancelMode.KILL
