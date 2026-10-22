"""Reference scenarios built only on public APIs (baseline section 20).

``handoff``: a mobile carrier brings a part from intake to a shared handoff station; a
stationary arm picks and inspects it. Exercises allocation, fenced reservations of the
shared station, readiness, ledgered commitments, physical-occupancy-aware release and
verified outcomes. Run with ``air scenario handoff``.

Variants (``air scenario handoff --variant ...``):

- ``disconnect``: the carrier's first three steps are *released* together (a released
  prefix), the rest of its sequence is horizon. The carrier loses its connection while
  driving. The coordinator sees CONNECTION_BROKEN, keeps the carrier's holds and marks
  the released steps UNRESOLVED; the carrier finishes exactly the released prefix and
  executes nothing from the horizon. After reconnecting, ``resume`` reconciles the
  prefix by observation and the mission completes, with nothing executed twice.
- ``cancel``: the mission is cancelled while the carrier is driving. The cancel gets its
  own response, the drive is interrupted, the released-but-unstarted step is confirmed
  never started, and every task ends CANCELLED. The mission is CANCELLED, never VERIFIED.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .contracts.manifest import AdapterRef, ComponentManifest
from .coordination import (
    CoordinatorTimeouts,
    LocalTransport,
    Mission,
    MissionResult,
    MissionState,
    MissionTask,
    TaskState,
    Team,
)
from .core.errors import FrameworkError
from .core.ids import new_id
from .integrations.fake import FakeRobotAdapter, fake_skill_set
from .integrations.world import FakeWorld
from .provenance.project import Project
from .robot import Robot
from .skills import SkillRegistry

__requirements__ = ("REQ06", "REQ09", "REQ17", "REQ18")

VARIANTS = ("nominal", "disconnect", "cancel")
#: The mission state each variant is expected to end in.
EXPECTED = {"nominal": MissionState.VERIFIED, "disconnect": MissionState.VERIFIED,
            "cancel": MissionState.CANCELLED}


def handoff_robots(world: str, *, carriers: int = 1, tick_s: float = 0.002,
                   sim_dt_s: float = 0.25, carrier_config: dict[str, Any] | None = None
                   ) -> list[Robot]:
    stations = {"intake": {"x": 2.0, "y": 1.0}, "handoff": {"x": 4.0, "y": 0.0}}
    robots = []
    for i in range(carriers):
        cfg: dict[str, Any] = {"world": world, "stations": stations,
                               "parts": {"part-7": "intake"}, "tick_s": tick_s,
                               "sim_dt_s": sim_dt_s, "start": {"x": float(i) * 3, "y": 0.0}}
        cfg.update(carrier_config or {})
        robots.append(Robot.from_manifest(ComponentManifest(
            robot_id=f"carrier-{i + 1}", name=f"carrier {i + 1}", hardware_revision="sim-A",
            adapter=AdapterRef(kind="fake", config=cfg),
            nominal_capabilities=("mobility", "imaging"))))
    robots.append(Robot.from_manifest(ComponentManifest(
        robot_id="arm-1", name="inspection arm", hardware_revision="sim-B",
        adapter=AdapterRef(kind="fake_arm", config={"world": world, "reach": ["handoff"],
                                                     "tick_s": tick_s}),
        nominal_capabilities=("manipulation", "inspection"))))
    return robots


def handoff_mission(mission_id: str = "inspect-part-7") -> Mission:
    T = MissionTask
    return Mission(mission_id=mission_id, goal="bring part-7 to handoff and inspect it", tasks=(
        T("dock_intake", "dock_at", {"station": "intake"}),
        T("load", "load_part", {"station": "intake", "part": "part-7"},
          depends_on=("dock_intake",), colocated_with="dock_intake"),
        T("dock_handoff", "dock_at", {"station": "handoff"}, depends_on=("load",),
          colocated_with="dock_intake"),
        T("unload", "unload_part", {"station": "handoff", "part": "part-7"},
          depends_on=("dock_handoff",), colocated_with="dock_intake"),
        T("clear", "undock", depends_on=("unload",), colocated_with="dock_intake"),
        T("pick", "pick_part", {"station": "handoff", "part": "part-7"},
          depends_on=("clear",)),
        T("inspect", "inspect_part", {"part": "part-7"}, depends_on=("pick",),
          colocated_with="pick"),
    ))


async def until(predicate: Callable[[], bool], timeout: float = 10.0,
                what: str = "condition") -> None:
    """Wait (polling) until ``predicate()`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise FrameworkError("scenario timed out waiting", waiting_for=what)
        await asyncio.sleep(0.005)


def _holds(team: Team) -> str:
    held = []
    for name in ("intake", "handoff"):
        r = team.resources.holder(name)
        held.append(f"{name}={r.holder}#{r.fencing_token}" if r else f"{name}=free")
    return ", ".join(held)


async def run_handoff(echo: Callable[[str], None] = print, *,
                      project: Project | None = None,
                      variant: str = "nominal") -> MissionResult:
    if variant not in VARIANTS:
        raise FrameworkError("unknown handoff variant", variant=variant, known=VARIANTS)
    world = new_id("cell")
    slow = {"tick_s": 0.01, "speed_mps": 0.2} if variant != "nominal" else None
    try:
        team = Team(handoff_robots(world, carrier_config=slow),
                    SkillRegistry(fake_skill_set().skills), project=project,
                    timeouts=CoordinatorTimeouts())
        async with team:
            window = 3 if variant != "nominal" else 1
            plan = await team.plan(handoff_mission(), release_window=window)
            for a in plan.assignments:
                echo(f"  allocate {a.task_id:<13} -> {a.participant:<10} "
                     f"bids={json.dumps(dict(a.bids))}")
            if variant == "nominal":
                result = await team.execute(plan)
                echo(result.summary())
            elif variant == "disconnect":
                result = await _disconnect(team, plan, echo)
            else:
                result = await _cancel(team, plan, echo)
            sent = team.transport.stats.sent if isinstance(team.transport,
                                                           LocalTransport) else "?"
            echo(f"ledger: {len(team.coordinator.ledger)} entries, "
                 f"integrity {'OK' if team.coordinator.ledger.verify().ok else 'BROKEN'}; "
                 f"messages sent: {sent}")
            return result
    finally:
        FakeWorld.reset(world)


async def _disconnect(team: Team, plan: Any, echo: Callable[[str], None]) -> MissionResult:
    net = team.transport
    assert isinstance(net, LocalTransport)
    carrier = team.robot("carrier-1").adapter
    assert isinstance(carrier, FakeRobotAdapter)
    mission_id = plan.mission.mission_id
    running = asyncio.get_running_loop().create_task(team.execute(plan))
    await until(lambda: len(carrier.executed) >= 1, what="carrier moving")
    echo("  >> carrier-1 loses its connection while driving to intake")
    net.disconnect("carrier-1")
    first = await running
    echo(first.summary())
    echo(f"  connection: carrier-1 {team.coordinator.connection('carrier-1').value}; "
         f"holds kept: {_holds(team)}")
    rt = team.runtimes["carrier-1"]
    await until(lambda: len(carrier.executed) >= 3 and not rt.busy("carrier-1"),
                what="released prefix finished")
    await asyncio.sleep(0.05)
    horizon = [t for t, r in team.coordinator.records(mission_id).items()
               if r.assignee == "carrier-1" and r.state is TaskState.ACCEPTED]
    echo(f"  carrier-1 executed {len(carrier.executed)} released steps while disconnected; "
         f"horizon not executed: {', '.join(horizon)}")
    echo("  >> carrier-1 reconnects; resume reconciles the prefix by observation")
    net.reconnect("carrier-1")
    resumed = await team.resume(first)
    echo(resumed.summary())
    echo(f"  carrier-1 commands executed in total: {len(carrier.executed)} (none twice)")
    return resumed


async def _cancel(team: Team, plan: Any, echo: Callable[[str], None]) -> MissionResult:
    mission_id = plan.mission.mission_id
    running = asyncio.get_running_loop().create_task(team.execute(plan))

    def driving() -> bool:
        rec = team.coordinator.records(mission_id).get("dock_intake")
        return rec is not None and rec.state is TaskState.EXECUTING

    await until(driving, what="carrier driving")
    echo("  >> operator cancels the mission while carrier-1 is driving")
    report = await team.cancel(mission_id)
    for participant, results in report.responses.items():
        for command_id, r in results.items():
            echo(f"  cancel response {participant}: {command_id}: "
                 f"{'accepted' if r.get('accepted') else 'rejected'} ({r.get('reason')})")
    result = await running
    echo(result.summary())
    echo(f"  holds after confirmed stop: {_holds(team)}")
    return result
