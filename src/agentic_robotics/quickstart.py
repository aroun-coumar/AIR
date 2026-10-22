"""Fake-device quickstart: one robot, an agent, a plan, and verified execution.

Run with ``agentic-robotics quickstart`` or ``python -m agentic_robotics.quickstart``.
No hardware, simulator, model key or network is needed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from .agents import ScriptedModel, TaskAgent
from .contracts import AdapterRef, Command, ComponentManifest, Delegation
from .core.errors import AdmissionRejected
from .core.ids import new_id
from .integrations.fake import fake_skill_set
from .robot import Robot
from .runtime import PlanExecutor, Runtime
from .skills import SkillRegistry

__requirements__ = ("REQ18",)

MANIFEST = ComponentManifest(
    robot_id="carrier-1",
    name="Quickstart carrier",
    hardware_revision="sim-A",
    adapter=AdapterRef(kind="fake", config={
        "start": {"x": 0.0, "y": 0.0},
        "stations": {"station_a": {"x": 2.0, "y": 1.0}},
    }),
    nominal_capabilities=("mobility", "imaging"),
    frames=("map",),
)

# A stand-in for a language model: deterministic, offline. Swap in any ModelProvider.
PLAN_RESPONSE = json.dumps({
    "rationale": "Dock at the station, then photograph the part.",
    "steps": [
        {"skill_id": "dock_at", "robot_id": "carrier-1", "args": {"station": "station_a"},
         "rationale": "the part is at station_a"},
        {"skill_id": "capture_image", "robot_id": "carrier-1", "args": {},
         "rationale": "inspection evidence"},
    ],
})


async def run(echo: Callable[[str], None] = print) -> bool:
    skills = SkillRegistry(fake_skill_set().skills)
    runtime = Runtime(skills, root_principals=["operator:alice"])
    robot = runtime.register(Robot.from_manifest(MANIFEST))

    async with runtime:
        grant = runtime.authority.grant(Delegation(
            delegation_id="dlg_quickstart", grantor="operator:alice", grantee="agent:task",
            robots=("carrier-1",), skills=("dock_at", "capture_image", "navigate_to"),
            valid_from=runtime.clock.now(), reason="quickstart"))
        echo(f"granted {grant.delegation_id}: agent:task may run "
             f"{', '.join(grant.skills)} on carrier-1")

        agent = TaskAgent(ScriptedModel([PLAN_RESPONSE]), skills, runtime.clock)
        plan = await agent.plan("inspect the part at station_a", [robot])
        echo(f"plan {plan.plan_id}: " + " -> ".join(s.skill_id for s in plan.steps))

        result = await PlanExecutor(runtime).submit(
            plan, principal="agent:task", delegation_id=grant.delegation_id).wait()
        for task_id, r in result.steps.items():
            checks = ", ".join(f"{c.description}={'ok' if c.satisfied else 'FAIL'}"
                               for c in r.verification)
            echo(f"  {task_id} {r.skill_id}: {r.state.value} [{checks}]")
        echo(f"plan outcome: {result.state.value}")

        runtime.authority.revoke(grant.delegation_id, reason="quickstart done")
        stale = runtime.submit(Command(
            command_id=new_id("cmd"), robot_id="carrier-1", skill_id="navigate_to",
            issued_by="agent:task", delegation_id=grant.delegation_id,
            authority_revision=runtime.authority.revision, args={"x": 0.0, "y": 0.0}))
        r = await stale.wait()
        rejection = stale.rejection
        assert isinstance(rejection, AdmissionRejected)
        echo(f"after revocation, a new command is {r.state.value}: {rejection.reason}")
        echo(f"trace: {len(runtime.trace)} events, model calls: {agent.stats.model_calls}")
    return result.verified


def main() -> None:
    raise SystemExit(0 if asyncio.run(run()) else 1)


if __name__ == "__main__":
    main()
