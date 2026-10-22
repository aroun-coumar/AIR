from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from agentic_robotics import Command, Delegation, Robot, Runtime, SkillRegistry
from agentic_robotics.contracts import AdapterRef, ComponentManifest
from agentic_robotics.core import Clock, new_id
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set

ROOT = "operator:root"
AGENT = "agent:task"


@dataclass
class Env:
    runtime: Runtime
    robot: Robot
    adapter: FakeRobotAdapter
    delegation: Delegation

    def command(self, skill_id: str, args: dict[str, Any] | None = None, **kw: Any) -> Command:
        fields: dict[str, Any] = {
            "command_id": new_id("cmd"), "robot_id": self.robot.robot_id,
            "skill_id": skill_id, "issued_by": AGENT,
            "delegation_id": self.delegation.delegation_id,
            "authority_revision": self.runtime.authority.revision, "args": args or {},
        }
        fields.update(kw)
        return Command(**fields)


def manifest(robot_id: str = "carrier-1", **config: Any) -> ComponentManifest:
    cfg = {"stations": {"station_a": {"x": 1.0, "y": 0.0}}, "tick_s": 0.001, "sim_dt_s": 0.2}
    cfg.update(config)
    return ComponentManifest(robot_id=robot_id, name="test carrier", hardware_revision="t1",
                             adapter=AdapterRef(kind="fake", config=cfg),
                             nominal_capabilities=("mobility", "imaging"))


@pytest.fixture
def make_env() -> Callable[..., Env]:
    def build(clock: Clock | None = None, **config: Any) -> Env:
        rt = Runtime(SkillRegistry(fake_skill_set().skills), clock=clock, root_principals=[ROOT])
        robot = rt.register(Robot.from_manifest(manifest(**config)))
        d = rt.authority.grant(Delegation(delegation_id="dlg_test", grantor=ROOT, grantee=AGENT,
                                          robots=("carrier-1",), valid_from=rt.clock.now()))
        assert isinstance(robot.adapter, FakeRobotAdapter)
        return Env(rt, robot, robot.adapter, d)

    return build
