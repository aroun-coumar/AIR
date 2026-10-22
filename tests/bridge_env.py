"""Shared helpers for the ecosystem-bridge tests (ADR 0019)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from agentic_robotics import Command, Delegation, Robot, Runtime, SkillRegistry
from agentic_robotics.contracts import AdapterRef, ComponentManifest, SkillSpec
from agentic_robotics.core import new_id
from agentic_robotics.plugins import RobotAdapter

ROOT = "operator:root"
AGENT = "agent:bridge"


def manifest(kind: str, robot_id: str, caps: tuple[str, ...], **config: Any) -> ComponentManifest:
    return ComponentManifest(robot_id=robot_id, name=robot_id, hardware_revision="t",
                             adapter=AdapterRef(kind=kind, config=dict(config)),
                             nominal_capabilities=caps)


class Env:
    def __init__(self, runtime: Runtime, adapter: RobotAdapter, delegation: Delegation) -> None:
        self.runtime = runtime
        self.adapter = adapter
        self.delegation = delegation

    def command(self, skill_id: str, **args: Any) -> Command:
        return Command(command_id=new_id("cmd"), robot_id=self.adapter.robot_id,
                       skill_id=skill_id, issued_by=AGENT,
                       delegation_id=self.delegation.delegation_id,
                       authority_revision=self.runtime.authority.revision, args=dict(args))

    async def run(self, skill_id: str, timeout: float = 10.0, **args: Any) -> Any:
        return await self.runtime.submit(self.command(skill_id, **args)).wait(timeout)


async def make_env(adapter: RobotAdapter, skills: tuple[SkillSpec, ...]) -> Env:
    rt = Runtime(SkillRegistry(skills), root_principals=[ROOT])
    rt.register(Robot(adapter.manifest, adapter))
    await rt.start()
    d = rt.authority.grant(Delegation(delegation_id=new_id("dlg"), grantor=ROOT, grantee=AGENT,
                                      robots=(adapter.robot_id,), valid_from=rt.clock.now()))
    return Env(rt, adapter, d)


async def until(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.003)
    return False
