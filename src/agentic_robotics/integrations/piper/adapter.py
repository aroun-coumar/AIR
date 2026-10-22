"""AgileX Piper RobotAdapter over CAN through an injected layer (ADR 0019).

- **CAN activation is an explicit precondition and health check.** The interface state
  (operstate, bitrate) is observed as ``can.up`` / ``can.interface``; a down interface or
  the wrong bitrate makes ``manipulation`` UNAVAILABLE with a limiting factor that tells
  the operator to run the SDK's ``can_activate.sh``. AIR never activates it.
- **Feedback freshness from the SDK's own frames.** The SDK keeps returning the last
  frame after the bus dies, so freshness comes from the frame ``time_stamp`` advancing,
  stamped in the runtime clock when AIR saw it change.
- **Clamped and reported.** Joint targets are clamped to the limits in ``JointCtrl``'s
  documentation; requested vs applied is reported. Interrupt is the SDK's quick stop
  (``EmergencyStop(0x01)``), confirmed only when the arm status reads emergency stop.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from importlib import resources
from typing import Any

from ...contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ...contracts.manifest import ComponentManifest
from ...contracts.observation import Observation
from ...contracts.skill import SkillSet
from ...core.errors import UnsupportedOperation
from ...plugins import AdapterReport, ExecutionContext, InterruptReport, RobotAdapter
from ..common import CapabilityTracker, clamp, observation
from . import can as c
from .can import CanLayer, PiperSdkLayer

__requirements__ = ("REQ03", "REQ05", "REQ12", "REQ13")

SKILLS = ("enable_arm", "move_joints", "set_gripper", "reset_estop")
JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")


def piper_skill_set() -> SkillSet:
    text = resources.files(__package__).joinpath("skills.json").read_text("utf-8")
    return SkillSet.from_json(text)


class PiperAdapter(RobotAdapter):
    kind = "agilex_piper"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        self.can_name = str(self.config.get("can_name", "can0"))
        self.expected_bitrate = int(self.config.get("expected_bitrate", 1_000_000))
        self.speed_pct = int(self.config.get("speed_pct", 30))
        self.feedback_timeout = float(self.config.get("feedback_timeout_s", 0.2))
        self.motion_timeout = float(self.config.get("motion_timeout_s", 10.0))
        self.period = float(self.config.get("command_period_s", 0.01))
        self._layer: CanLayer | None = None
        self._last_stamp: float | None = None
        self._stamp_at: float | None = None
        self._caps = CapabilityTracker()
        self._active: str | None = None
        self._interrupt: set[str] = set()

    def use_can(self, layer: CanLayer) -> None:
        self._layer = layer

    async def start(self) -> None:
        if self._layer is None:  # pragma: no cover - real SDK only
            self._layer = PiperSdkLayer(self.can_name)
        if self._layer.interface_state().get("operstate") == "up":
            self._layer.connect()

    async def stop(self) -> None:
        if self._layer is not None:
            self._layer.disconnect()

    # -- health ----------------------------------------------------------------------
    def _iface(self) -> dict[str, Any]:
        if self._layer is None:
            return {"name": self.can_name, "operstate": "unknown", "bitrate": None}
        return dict(self._layer.interface_state())

    def _can_factors(self, iface: dict[str, Any]) -> list[str]:
        out = []
        if iface.get("operstate") != "up":
            out.append("can_interface_down: operator must run can_activate.sh")
        elif iface.get("bitrate") not in (None, self.expected_bitrate):
            out.append("can_bitrate_mismatch")
        return out

    def _feedback(self) -> tuple[list[int], bool]:
        assert self._layer is not None
        stamp, mdeg = self._layer.joint_feedback()
        now = self.require_clock().now()
        if stamp != self._last_stamp:
            self._last_stamp, self._stamp_at = stamp, now
        fresh = self._stamp_at is not None and now - self._stamp_at <= self.feedback_timeout
        return mdeg, fresh

    # -- RobotAdapter ----------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.config.get("skills", SKILLS))

    def capability_report(self) -> CapabilityReport:
        iface = self._iface()
        factors = self._can_factors(iface)
        status = ProfileStatus.UNAVAILABLE if factors else ProfileStatus.AVAILABLE
        if not factors and self._layer is not None:
            _, fresh = self._feedback()
            arm = self._layer.arm_status()
            if not fresh:
                factors.append("feedback_stale")
            if arm.get("arm_status") not in (c.ARM_STATUS_NORMAL, None):
                factors.append(f"arm_status_{arm.get('arm_status')}")
            if factors:
                status = ProfileStatus.UNAVAILABLE
            elif not all(self._layer.enable_status()):
                factors.append("arm_disabled")
                status = ProfileStatus.RESTRICTED
        return self._caps.report(self.robot_id, self.require_clock(), (CapabilityProfile(
            "manipulation", status, limits={"speed_pct": float(self.speed_pct)},
            limiting_factors=tuple(factors), evidence=(f"can:{iface.get('name')}",)),))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        src = f"{self.robot_id}/piper"
        iface = self._iface()
        out = {
            "can.interface": observation(clock, src, "can.interface", iface),
            "can.up": observation(clock, src, "can.up", not self._can_factors(iface)),
        }
        if self._layer is None:
            return out
        mdeg, fresh = self._feedback()
        at = self._stamp_at if self._stamp_at is not None else clock.now()
        if fresh or self._stamp_at is not None:
            out["arm.joints"] = observation(
                clock, src, "arm.joints",
                {j: round(math.radians(v / 1000.0), 5) for j, v in zip(JOINTS, mdeg, strict=True)},
                acquired_at=at, unit="rad")
            _, stroke, _ = self._layer.gripper_feedback()
            out["gripper.opening"] = observation(clock, src, "gripper.opening",
                                                 stroke / 1_000_000.0, acquired_at=at, unit="m")
        arm = self._layer.arm_status()
        status = arm.get("arm_status")
        out["arm.status"] = observation(clock, src, "arm.status",
                                        "normal" if status == c.ARM_STATUS_NORMAL else
                                        "emergency_stop" if status == c.ARM_STATUS_ESTOP
                                        else f"code_{status}")
        out["arm.enabled"] = observation(clock, src, "arm.enabled",
                                         all(self._layer.enable_status()))
        return out

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.supported_skills() or cmd.skill_id not in SKILLS:
            raise UnsupportedOperation("piper adapter does not support skill",
                                       skill_id=cmd.skill_id)
        layer = self._layer
        if layer is None:
            raise ConnectionError("CAN layer not started")
        if self._can_factors(self._iface()):
            return AdapterReport(False, "CAN interface is not up; activation is an operator "
                                 "step (can_activate.sh)", failure_reason="can_interface_down")
        self._active = cmd.command_id
        try:
            if cmd.skill_id == "enable_arm":
                layer.enable_arm()
                return await self._hold(ctx, lambda: all(layer.enable_status()), "enabled")
            if cmd.skill_id == "reset_estop":
                layer.emergency_stop(c.RESUME)
                return await self._hold(
                    ctx, lambda: layer.arm_status().get("arm_status") == c.ARM_STATUS_NORMAL,
                    "resumed")
            if cmd.skill_id == "set_gripper":
                requested = float(cmd.args["opening_m"])
                applied = clamp(requested, 0.0, float(self.config.get("gripper_max_m", 0.07)))
                layer.gripper_ctrl(int(applied * 1_000_000), int(self.config.get(
                    "gripper_effort_mnm", 1000)), c.GRIPPER_ENABLE)
                report = await self._hold(
                    ctx, lambda: abs(layer.gripper_feedback()[1] - applied * 1e6) < 500,
                    "gripper commanded")
                return _with(report, requested=requested, applied=applied)
            return await self._move(ctx)
        finally:
            self._active = None
            self._interrupt.discard(cmd.command_id)

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active != command_id or self._layer is None:
            return InterruptReport(False, False, "command is not active")
        self._interrupt.add(command_id)
        self._layer.emergency_stop(c.ESTOP)
        stopped = self._layer.arm_status().get("arm_status") == c.ARM_STATUS_ESTOP
        return InterruptReport(True, stopped, "quick stop observed" if stopped
                               else "quick stop sent; not observed")

    # -- skills ----------------------------------------------------------------------
    async def _hold(self, ctx: ExecutionContext, done: Any, ok: str) -> AdapterReport:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.motion_timeout
        while not done():
            if ctx.command.command_id in self._interrupt:
                return AdapterReport(False, "interrupted", failure_reason="interrupted")
            if loop.time() > end:
                return AdapterReport(False, "timed out waiting for feedback",
                                     failure_reason="feedback_timeout")
            _, fresh = self._feedback()
            if not fresh:
                raise ConnectionError("Piper feedback went stale")
            await asyncio.sleep(self.period)
        self._feedback()
        return AdapterReport(True, ok)

    async def _move(self, ctx: ExecutionContext) -> AdapterReport:
        assert self._layer is not None
        layer = self._layer
        requested = [float(ctx.command.args[j]) for j in JOINTS]
        applied = [clamp(v, lo, hi) for v, (lo, hi) in
                   zip(requested, c.JOINT_LIMITS_RAD, strict=True)]
        target = [round(math.degrees(v) * 1000) for v in applied]
        layer.motion_ctrl_2(c.CTRL_MODE_CAN, c.MOVE_J, self.speed_pct)
        loop = asyncio.get_running_loop()
        end = loop.time() + self.motion_timeout
        while True:
            if ctx.command.command_id in self._interrupt:
                return AdapterReport(False, "interrupted", failure_reason="interrupted")
            if not ctx.authority_valid():
                layer.emergency_stop(c.ESTOP)
                return AdapterReport(False, "authority lost", failure_reason="authority_lost")
            layer.joint_ctrl(target)
            mdeg, fresh = self._feedback()
            if not fresh:
                raise ConnectionError("Piper feedback went stale during motion")
            status = layer.arm_status()
            if status.get("arm_status") != c.ARM_STATUS_NORMAL:
                return AdapterReport(False, f"arm status {status.get('arm_status')}",
                                     failure_reason=f"arm_status_{status.get('arm_status')}")
            if all(abs(a - b) <= 200 for a, b in zip(mdeg, target, strict=True)):
                break
            if loop.time() > end:
                return AdapterReport(False, "joints did not reach target in time",
                                     failure_reason="motion_timeout")
            await asyncio.sleep(self.period)
        limited = [j for j, r, a in zip(JOINTS, requested, applied, strict=True) if r != a]
        return AdapterReport(True, "joints reached target", effects={
            "requested": dict(zip(JOINTS, requested, strict=True)),
            "applied": dict(zip(JOINTS, applied, strict=True)),
            "limited_by": [f"joint_limit:{j}" for j in limited]})


def _with(report: AdapterReport, **effects: Any) -> AdapterReport:
    return AdapterReport(report.completed, report.message, {**report.effects, **effects},
                         report.failure_reason)


__all__ = ["JOINTS", "SKILLS", "PiperAdapter", "piper_skill_set"]
