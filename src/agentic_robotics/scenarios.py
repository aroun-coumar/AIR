"""Reference scenarios built only on public APIs (baseline section 20).

``handoff``: a mobile carrier brings a part from intake to a shared handoff station; a
stationary arm picks and inspects it. Exercises allocation, fenced reservations of the
shared station, readiness, ledgered commitments, physical-occupancy-aware release and
verified outcomes. Run with ``air scenario handoff``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .contracts.manifest import AdapterRef, ComponentManifest
from .coordination import (
    CoordinatorTimeouts,
    LocalTransport,
    Mission,
    MissionResult,
    MissionTask,
    Team,
)
from .core.ids import new_id
from .integrations.fake import fake_skill_set
from .integrations.world import FakeWorld
from .provenance.project import Project
from .robot import Robot
from .skills import SkillRegistry

__requirements__ = ("REQ06", "REQ09", "REQ18")


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


async def run_handoff(echo: Callable[[str], None] = print, *,
                      project: Project | None = None) -> MissionResult:
    world = new_id("cell")
    try:
        team = Team(handoff_robots(world), SkillRegistry(fake_skill_set().skills),
                    project=project, timeouts=CoordinatorTimeouts())
        async with team:
            plan = await team.plan(handoff_mission())
            for a in plan.assignments:
                echo(f"  allocate {a.task_id:<13} -> {a.participant:<10} "
                     f"bids={json.dumps(dict(a.bids))}")
            result = await team.execute(plan)
            echo(result.summary())
            sent = team.transport.stats.sent if isinstance(team.transport,
                                                           LocalTransport) else "?"
            echo(f"ledger: {len(team.coordinator.ledger)} entries, "
                 f"integrity {'OK' if team.coordinator.ledger.verify().ok else 'BROKEN'}; "
                 f"messages sent: {sent}")
            return result
    finally:
        FakeWorld.reset(world)
