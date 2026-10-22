"""Team: the convenience entry point for multi-robot work (baseline section 7 API shape).

    team = Team([carrier, arm], skills, project=Project.open("."))
    async with team:
        plan = await team.plan(Mission.load("missions/inspect_part.json"))
        result = await team.execute(plan)

Each robot gets its own robot-local :class:`Runtime` (so local admission, authority and
fencing stay on the robot) sharing one authority domain, one resource manager and one
causal trace. A :class:`TeamCoordinator` drives the coordination protocol over a
:class:`Transport`. With a ``project``, all evidence lands in its ledger.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..contracts.authority import Delegation
from ..core.clock import Clock, MonotonicClock
from ..provenance.ledger import Ledger
from ..provenance.project import DEFAULT_RUNTIME_KINDS, Project
from ..robot import Robot
from ..runtime.authority import AuthorityService
from ..runtime.resources import ResourceManager
from ..runtime.runtime import Runtime
from ..runtime.trace import Trace
from ..skills import SkillRegistry
from .allocation import Allocator
from .coordinator import CoordinatorTimeouts, MissionResult, TeamCoordinator
from .participant import Participant
from .protocol import Mission, TeamPlan
from .transport import LocalTransport, Transport

__requirements__ = ("REQ06", "REQ09", "REQ18")

TEAM_TRACE_KINDS = frozenset({
    "team_planned", "mission_finished", "team_not_ready", "team_commit_retry",
    "stale_revision_ignored", "participant_announced", "duplicate_message_ignored",
    *(f"team_task_{s}" for s in ("proposed", "accepted", "reserved", "ready", "committed",
                                 "executing", "completed", "verified", "cancelled",
                                 "failed", "unresolved")),
})


class Team:
    def __init__(self, robots: Sequence[Robot], skills: SkillRegistry, *,
                 clock: Clock | None = None, operator: str = "operator:root",
                 transport: Transport | None = None, allocator: Allocator | None = None,
                 project: Project | None = None, timeouts: CoordinatorTimeouts | None = None,
                 debug: bool = False) -> None:
        self.clock = clock or MonotonicClock()
        self.skills = skills
        self.operator = operator
        self.project = project
        self.transport = transport or LocalTransport()
        self.trace = Trace(self.clock)
        if project is not None:
            self.trace.add_sink(project.runtime_sink(DEFAULT_RUNTIME_KINDS | TEAM_TRACE_KINDS))
        self.authority = AuthorityService(self.clock, [operator])
        self.resources = ResourceManager(self.clock)
        self.runtimes: dict[str, Runtime] = {}
        self.participants: dict[str, Participant] = {}
        self.delegations: dict[str, Delegation] = {}
        for i, robot in enumerate(robots):
            rt = Runtime(skills, clock=self.clock, authority=self.authority,
                         resources=self.resources, trace=self.trace, debug=debug,
                         record_shared_events=(i == 0))
            rt.register(robot)
            principal = f"agent:{robot.robot_id}"
            d = self.authority.grant(Delegation(
                delegation_id=f"dlg-{robot.robot_id}", grantor=operator, grantee=principal,
                robots=(robot.robot_id,), valid_from=self.clock.now(),
                reason="team member"))
            self.runtimes[robot.robot_id] = rt
            self.delegations[robot.robot_id] = d
            self.participants[robot.robot_id] = Participant(
                robot, rt, self.transport, principal=principal, delegation_id=d.delegation_id)
        self.coordinator = TeamCoordinator(
            self.transport, self.resources, skills, self.clock, self.trace,
            ledger=project.ledger if project is not None else Ledger(),
            allocator=allocator, timeouts=timeouts)

    def robot(self, robot_id: str) -> Robot:
        return self.runtimes[robot_id].robot(robot_id)

    async def start(self) -> None:
        for rt in self.runtimes.values():
            await rt.start()
        for p in self.participants.values():
            await p.announce()
        await self.coordinator.wait_for_participants(self.participants)

    async def stop(self) -> None:
        if isinstance(self.transport, LocalTransport):
            await self.transport.drain()
        for rt in self.runtimes.values():
            await rt.stop()

    async def __aenter__(self) -> Team:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def plan(self, mission: Mission) -> TeamPlan:
        return await self.coordinator.plan(mission)

    async def execute(self, plan: TeamPlan) -> MissionResult:
        return await self.coordinator.execute(plan)

    async def run(self, mission: Mission) -> MissionResult:
        return await self.execute(await self.plan(mission))

    async def resume(self, result: MissionResult) -> MissionResult:
        return await self.coordinator.resume(result)
