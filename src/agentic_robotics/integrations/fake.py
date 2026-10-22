"""Fake mobile robot: a dependency-free adapter for quickstarts, tests and conformance.

It simulates a planar mobile base with a battery and a camera, publishes timestamped
observations, reports effective capability, and supports deterministic fault injection
so failure semantics can be exercised without hardware::

    adapter.inject("stall")                   # motion never progresses
    adapter.inject("lose_link_after_motion")  # moves, then the result is lost
    adapter.inject("drift_m", 0.3)            # ends up off target
    adapter.inject("freeze_sensors")          # observations stop updating
    adapter.inject("camera_offline")
    adapter.inject("unconfirmed_stop")        # interrupt cannot confirm stopping
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from importlib import resources
from typing import Any

from ..contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ..contracts.manifest import ComponentManifest
from ..contracts.observation import Observation
from ..contracts.skill import SkillSet
from ..core.errors import UnsupportedOperation
from ..core.ids import new_id
from ..plugins import AdapterReport, ExecutionContext, InterruptReport, RobotAdapter
from .world import FakeWorld

__requirements__ = ("REQ03", "REQ13")


def fake_skill_set() -> SkillSet:
    text = resources.files(__package__).joinpath("fake_skills.json").read_text("utf-8")
    return SkillSet.from_json(text)


def world_observations(world: FakeWorld,
                       obs: Callable[[str, Any], Observation]) -> dict[str, Observation]:
    """What any robot in the shared world can see: parts at stations and occupancy."""
    out: dict[str, Observation] = {}
    for station in world.station_part:
        out[f"station.{station}.part"] = obs(f"station.{station}.part",
                                             world.station_part[station])
        out[f"occupancy.{station}"] = obs(f"occupancy.{station}", world.occupant[station])
    return out


class FakeRobotAdapter(RobotAdapter):
    kind = "fake"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        start = self.config.get("start", {"x": 0.0, "y": 0.0})
        self.x = float(start["x"])
        self.y = float(start["y"])
        self.theta = 0.0
        self.battery = float(self.config.get("battery_pct", 80.0))
        self.speed = float(self.config.get("speed_mps", 1.0))
        self.sim_dt = float(self.config.get("sim_dt_s", 0.05))
        self.tick = float(self.config.get("tick_s", 0.002))
        self.drain_per_m = float(self.config.get("drain_pct_per_m", 0.5))
        self.low_battery = float(self.config.get("low_battery_pct", 20.0))
        self.critical_battery = float(self.config.get("critical_battery_pct", 5.0))
        self.stations: dict[str, dict[str, float]] = dict(self.config.get("stations", {}))
        self.has_camera = bool(self.config.get("camera", True))
        self.world = FakeWorld.get(str(self.config.get("world", f"private:{id(self)}")))
        self.world.add_stations(self.stations)
        for part, location in dict(self.config.get("parts", {})).items():
            self.world.place_part(part, location)
        self.estop = False
        self.docked_at = ""
        self.payload = ""
        self.last_image: dict[str, Any] | None = None
        self.calibration: dict[str, Any] = {"valid": False}
        self.faults: dict[str, Any] = {}
        self.executed: list[str] = []
        self._skills = frozenset(self.config.get("skills", [
            "navigate_to", "dock_at", "undock", "load_part", "unload_part",
            *(["capture_image", "calibrate_camera"] if self.has_camera else [])]))
        self._active: str | None = None
        self._interrupt: set[str] = set()
        self._cap_revision = 0
        self._cap_key: tuple[Any, ...] | None = None
        self._frozen: dict[str, Observation] | None = None

    # -- fault injection -----------------------------------------------------------
    def inject(self, fault: str, value: Any = True) -> None:
        if fault == "freeze_sensors":
            self._frozen = self._snapshot()
        self.faults[fault] = value

    def clear(self, fault: str | None = None) -> None:
        if fault is None:
            self.faults.clear()
            self._frozen = None
        else:
            self.faults.pop(fault, None)
            if fault == "freeze_sensors":
                self._frozen = None

    # -- RobotAdapter --------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return self._skills

    def capability_report(self) -> CapabilityReport:
        clock = self.require_clock()
        profiles = (self._mobility(), self._imaging())
        key = tuple((p.name, p.status) for p in profiles)
        if key != self._cap_key:
            self._cap_key = key
            self._cap_revision += 1
        return CapabilityReport(robot_id=self.robot_id, capability_revision=self._cap_revision,
                                observed_at=clock.now(), clock_domain=clock.domain,
                                profiles=profiles)

    def observe(self) -> dict[str, Observation]:
        if self._frozen is not None:
            return dict(self._frozen)
        return self._snapshot()

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self._skills:
            raise UnsupportedOperation("fake adapter does not support skill",
                                       skill_id=cmd.skill_id)
        self._active = cmd.command_id
        self.executed.append(cmd.command_id)
        try:
            if cmd.skill_id == "navigate_to":
                return await self._drive(ctx, float(cmd.args["x"]), float(cmd.args["y"]))
            if cmd.skill_id == "dock_at":
                station = str(cmd.args["station"])
                if station not in self.stations:
                    return AdapterReport(False, f"unknown station {station!r}",
                                         failure_reason="unknown_station")
                s = self.stations[station]
                report = await self._drive(ctx, float(s["x"]), float(s["y"]),
                                           finish=lambda: self._dock(station))
                if report.completed:
                    report = AdapterReport(True, "docked", {**report.effects,
                                                            "docked_at": station})
                return report
            if cmd.skill_id == "undock":
                await asyncio.sleep(self.tick)
                self._undock()
                return AdapterReport(True, "undocked", self._pose_effects())
            if cmd.skill_id in ("load_part", "unload_part"):
                station, part = str(cmd.args["station"]), str(cmd.args["part"])
                await asyncio.sleep(self.tick * 3)
                if self.docked_at != station:
                    return AdapterReport(False, "not docked at station",
                                         failure_reason="not_docked")
                try:
                    if cmd.skill_id == "load_part":
                        self.world.move_part(part, station, self.robot_id)
                        self.payload = part
                    else:
                        self.world.move_part(part, self.robot_id, station)
                        self.payload = ""
                except Exception as exc:
                    return AdapterReport(False, str(exc), failure_reason="part_transfer")
                return AdapterReport(True, f"{cmd.skill_id} {part}", {"payload": self.payload})
            if cmd.skill_id == "capture_image":
                await asyncio.sleep(self.tick * 5)
                if self.faults.get("camera_offline"):
                    return AdapterReport(False, "camera offline", failure_reason="camera_offline")
                self.last_image = {"image_id": new_id("img"), "command_id": cmd.command_id}
                return AdapterReport(True, "image captured", {"image": self.last_image})
            if cmd.skill_id == "calibrate_camera":
                for _ in range(20):
                    await asyncio.sleep(self.tick)
                self.calibration = {"valid": True, "rms_px": 0.21}
                return AdapterReport(True, "calibrated", {"calibration": self.calibration})
            raise UnsupportedOperation("skill not implemented", skill_id=cmd.skill_id)
        finally:
            self._active = None
            self._interrupt.discard(cmd.command_id)

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active != command_id:
            return InterruptReport(False, False, "command is not active")
        self._interrupt.add(command_id)
        confirmed = not self.faults.get("unconfirmed_stop")
        return InterruptReport(True, confirmed,
                               "stop requested" if confirmed else "stop could not be confirmed")

    # -- simulation ----------------------------------------------------------------
    def estimate_cost(self, skill_id: str, args: Mapping[str, Any]) -> float | None:
        if skill_id == "navigate_to":
            return math.hypot(float(args["x"]) - self.x, float(args["y"]) - self.y)
        if skill_id == "dock_at" and str(args.get("station")) in self.stations:
            s = self.stations[str(args["station"])]
            return math.hypot(float(s["x"]) - self.x, float(s["y"]) - self.y)
        return 0.1

    def _dock(self, station: str) -> None:
        self.world.occupy(station, self.robot_id)
        self.docked_at = station

    def _undock(self) -> None:
        if self.docked_at:
            self.world.vacate(self.docked_at, self.robot_id)
        self.docked_at = ""

    async def _drive(self, ctx: ExecutionContext, tx: float, ty: float,
                     finish: Callable[[], None] | None = None) -> AdapterReport:
        cmd = ctx.command
        self._undock()
        step = self.speed * self.sim_dt
        ticks = 0
        while True:
            if cmd.command_id in self._interrupt:
                return AdapterReport(False, "interrupted", self._pose_effects(),
                                     failure_reason="interrupted")
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dist < 1e-9:
                break
            if not self.faults.get("stall"):
                move = min(step, dist)
                self.x += dx / dist * move
                self.y += dy / dist * move
                self.theta = math.atan2(dy, dx)
                self.battery = max(0.0, self.battery - move * self.drain_per_m)
            ticks += 1
            if ticks % 10 == 1:
                ctx.progress("moving", {"remaining_m": round(dist, 3)})
            await asyncio.sleep(self.tick)
        drift = float(self.faults.get("drift_m", 0.0))
        if drift:
            self.x += drift
        if finish is not None:
            finish()
        if self.faults.get("lose_link_after_motion"):
            raise ConnectionError("link to robot lost before result was received")
        return AdapterReport(True, "arrived", self._pose_effects())

    def _pose_effects(self) -> dict[str, Any]:
        return {"pose": {"x": round(self.x, 4), "y": round(self.y, 4)},
                "battery_pct": round(self.battery, 2)}

    def _mobility(self) -> CapabilityProfile:
        if self.estop:
            return CapabilityProfile("mobility", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("estop_engaged",))
        if self.battery < self.critical_battery:
            return CapabilityProfile("mobility", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("battery_critical",))
        if self.battery < self.low_battery:
            return CapabilityProfile("mobility", ProfileStatus.RESTRICTED,
                                     limits={"max_speed_mps": self.speed / 2},
                                     limiting_factors=("battery_low",))
        return CapabilityProfile("mobility", ProfileStatus.AVAILABLE,
                                 limits={"max_speed_mps": self.speed})

    def _imaging(self) -> CapabilityProfile:
        if not self.has_camera:
            return CapabilityProfile("imaging", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("not_installed",))
        if self.faults.get("camera_offline"):
            return CapabilityProfile("imaging", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("camera_offline",))
        return CapabilityProfile("imaging", ProfileStatus.AVAILABLE)

    def _snapshot(self) -> dict[str, Observation]:
        clock = self.require_clock()
        now = clock.now()
        src = f"{self.robot_id}/fake"

        def obs(signal: str, value: Any, **kw: Any) -> Observation:
            return Observation(source=src, signal=signal, value=value,
                               clock_domain=clock.domain, acquired_at=now, received_at=now,
                               **kw)

        out = {
            "base.pose": obs("base.pose", {"x": round(self.x, 4), "y": round(self.y, 4),
                                           "theta": round(self.theta, 4)},
                             frame="map", spatial=True, unit="m", uncertainty=0.01),
            "battery.charge": obs("battery.charge", round(self.battery, 2), unit="%"),
            "base.estop": obs("base.estop", self.estop),
            "base.docked_at": obs("base.docked_at", self.docked_at),
            "payload.part": obs("payload.part", self.payload),
        }
        out.update(world_observations(self.world, obs))
        if self.has_camera:
            out["camera.calibration"] = obs("camera.calibration", self.calibration)
            if self.last_image is not None:
                out["camera.last_image"] = obs("camera.last_image", self.last_image,
                                               artifact_ref=self.last_image["image_id"])
        return out
