"""Fake stationary manipulator with an inspection station, for the two-robot reference
scenario (baseline section 20). Shares a :class:`FakeWorld` with fake carriers.

Faults: ``gripper_fault`` (manipulation unavailable), ``inspection_fail`` (part fails
inspection), ``lose_link_after_pick`` (picks, then the result is lost).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ..contracts.manifest import ComponentManifest
from ..contracts.observation import Observation
from ..core.errors import UnsupportedOperation
from ..core.ids import new_id
from ..plugins import AdapterReport, ExecutionContext, InterruptReport, RobotAdapter
from .fake import world_observations
from .world import FakeWorld

__requirements__ = ("REQ03", "REQ06")


class FakeArmAdapter(RobotAdapter):
    kind = "fake_arm"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        self.world = FakeWorld.get(str(self.config.get("world", f"private:{id(self)}")))
        self.reach = frozenset(self.config.get("reach", []))
        self.world.add_stations(sorted(self.reach))
        self.tick = float(self.config.get("tick_s", 0.002))
        self.holding = ""
        self.last_inspection: dict[str, Any] | None = None
        self.faults: dict[str, Any] = {}
        self.executed: list[str] = []
        self._active: str | None = None
        self._cap_revision = 0
        self._cap_key: tuple[Any, ...] | None = None

    def inject(self, fault: str, value: Any = True) -> None:
        self.faults[fault] = value

    def clear(self, fault: str | None = None) -> None:
        if fault is None:
            self.faults.clear()
        else:
            self.faults.pop(fault, None)

    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.config.get("skills", ["pick_part", "inspect_part"]))

    def capability_report(self) -> CapabilityReport:
        clock = self.require_clock()
        manip = (CapabilityProfile("manipulation", ProfileStatus.UNAVAILABLE,
                                   limiting_factors=("gripper_fault",))
                 if self.faults.get("gripper_fault")
                 else CapabilityProfile("manipulation", ProfileStatus.AVAILABLE,
                                        limits={"payload_kg": 2.0}))
        profiles = (manip, CapabilityProfile("inspection", ProfileStatus.AVAILABLE))
        key = tuple((p.name, p.status) for p in profiles)
        if key != self._cap_key:
            self._cap_key = key
            self._cap_revision += 1
        return CapabilityReport(robot_id=self.robot_id, capability_revision=self._cap_revision,
                                observed_at=clock.now(), clock_domain=clock.domain,
                                profiles=profiles)

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        now = clock.now()

        def obs(signal: str, value: Any, **kw: Any) -> Observation:
            return Observation(source=f"{self.robot_id}/fake_arm", signal=signal, value=value,
                               clock_domain=clock.domain, acquired_at=now, received_at=now,
                               **kw)

        out = {"gripper.holding": obs("gripper.holding", self.holding)}
        if self.last_inspection is not None:
            out["inspection.last"] = obs("inspection.last", self.last_inspection,
                                         artifact_ref=self.last_inspection["inspection_id"])
        out.update(world_observations(self.world, obs))
        return out

    def estimate_cost(self, skill_id: str, args: Mapping[str, Any]) -> float | None:
        return 0.5

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.supported_skills():
            raise UnsupportedOperation("fake arm does not support skill", skill_id=cmd.skill_id)
        self._active = cmd.command_id
        self.executed.append(cmd.command_id)
        try:
            for _ in range(5):
                await asyncio.sleep(self.tick)
            part = str(cmd.args["part"])
            if cmd.skill_id == "pick_part":
                station = str(cmd.args["station"])
                if station not in self.reach:
                    return AdapterReport(False, "station out of reach",
                                         failure_reason="out_of_reach")
                try:
                    self.world.move_part(part, station, self.robot_id)
                except Exception as exc:
                    return AdapterReport(False, str(exc), failure_reason="part_transfer")
                self.holding = part
                if self.faults.get("lose_link_after_pick"):
                    raise ConnectionError("link to arm lost before result was received")
                return AdapterReport(True, f"picked {part}", {"holding": part})
            if self.holding != part:
                return AdapterReport(False, "not holding part", failure_reason="not_holding")
            self.last_inspection = {"inspection_id": new_id("insp"), "part": part,
                                    "passed": not self.faults.get("inspection_fail")}
            return AdapterReport(True, "inspected", {"inspection": self.last_inspection})
        finally:
            self._active = None

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active != command_id:
            return InterruptReport(False, False, "command is not active")
        return InterruptReport(False, False, "arm motions complete once started")
