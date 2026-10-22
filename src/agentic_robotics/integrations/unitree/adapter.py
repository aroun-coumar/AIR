"""Unitree RobotAdapter over the unitree_sdk2 DDS channel model (ADR 0019).

- **Sport-mode RPC becomes skills.** Posture calls (``StandUp`` 1004, ``StandDown`` 1005,
  ``BalanceStand`` 1002, ``RecoveryStand`` 1006, ``StopMove`` 1003, ``Damp`` 1001) are
  answered with a code; the runtime still verifies the posture from
  ``SportModeState_.body_height``. ``Move`` (1008) is fire-and-forget, so ``walk_to``
  refreshes it from a closed loop on ``SportModeState_.position`` and verifies the pose.
- **A timeout is not a failure.** An RPC timeout (3104, 4201) may have reached the robot:
  the adapter raises, and the runtime records UNRESOLVED for ``reconcile()``.
- **LowCmd/LowState stay behind admission with freshness.** ``set_joint_position``
  requires the ``low_level`` capability (sport service *observed* released through
  ``motion_switcher`` CheckMode, re-checked just before writing),
  a ``rt/lowstate`` sample younger than ``max_input_age_s``, operator-configured joint
  limits (no defaults are invented), and checks freshness again before every write;
  a stale stream mid-write raises (outcome unknown). Requested vs applied targets are
  reported.

The channel layer is injected (:meth:`UnitreeAdapter.use_channels`); without one,
:class:`~.channels.SdkChannelLayer` is created lazily at start.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from ..common import CapabilityTracker, InterruptGate, clamp, observation, wait_until
from . import api
from .channels import ChannelLayer, SdkChannelLayer

__requirements__ = ("REQ03", "REQ05", "REQ12", "REQ14")

POSTURES = {"stand_up": api.Sport.STAND_UP, "stand_down": api.Sport.STAND_DOWN,
            "balance_stand": api.Sport.BALANCE_STAND,
            "recovery_stand": api.Sport.RECOVERY_STAND, "damp": api.Sport.DAMP}
SKILLS = (*POSTURES, "stop_move", "walk_to", "release_sport_mode", "set_joint_position")


def unitree_skill_set() -> SkillSet:
    text = resources.files(__package__).joinpath("skills.json").read_text("utf-8")
    return SkillSet.from_json(text)


class UnknownOutcome(ConnectionError):
    """The request may have reached the robot; the outcome must be reconciled."""


class UnitreeAdapter(RobotAdapter):
    kind = "unitree_sport"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        c = self.config
        self.sport_topic = str(c.get("sport_state_topic", api.TOPIC_SPORT_STATE))
        self.rpc_timeout = float(c.get("rpc_timeout_s", 2.0))
        self.refresh_s = float(c.get("move_refresh_s", 0.1))
        self.max_speed = float(c.get("max_speed_mps", 0.5))
        self.tolerance = float(c.get("goal_tolerance_m", 0.05))
        self.state_timeout = float(c.get("state_timeout_s", 0.5))
        self.posture_timeout = float(c.get("posture_timeout_s", 5.0))
        self.low_max_age = float(c.get("low_state_max_age_s", 0.05))
        self.low_period = float(c.get("low_cmd_period_s", 0.002))
        self.settle_s = float(c.get("settle_s", 1.0))
        self.low_level_enabled = bool(c.get("low_level", False))
        self.joint_limits: dict[str, tuple[float, float]] = {
            str(k): (float(v[0]), float(v[1])) for k, v in dict(c.get("joint_limits", {})).items()}
        self._channels: ChannelLayer | None = None
        self.sport: dict[str, Any] | None = None
        self.sport_at: float | None = None
        self.low: dict[str, Any] | None = None
        self.low_at: float | None = None
        self.low_seq = 0
        self.sport_seq = 0
        self.mode_name: str | None = None
        self.mode_at: float | None = None
        self._caps = CapabilityTracker()
        self._active: str | None = None
        self._interrupt: set[str] = set()
        self._gate = InterruptGate()

    # -- channels --------------------------------------------------------------------
    def use_channels(self, channels: ChannelLayer) -> None:
        self._channels = channels

    async def start(self) -> None:
        if self._channels is None:  # pragma: no cover - real SDK only
            self._channels = SdkChannelLayer(self.config.get("network_interface"),
                                             int(self.config.get("domain_id", 0)))
        self._channels.subscribe(self.sport_topic, self._on_sport)
        self._channels.subscribe(api.TOPIC_LOWSTATE, self._on_low)
        await self._check_mode()

    async def stop(self) -> None:
        if self._channels is not None:
            self._channels.close()

    def _on_sport(self, msg: dict[str, Any]) -> None:
        self.sport, self.sport_at = msg, self.require_clock().now()
        self.sport_seq += 1

    def _on_low(self, msg: dict[str, Any]) -> None:
        self.low, self.low_at = msg, self.require_clock().now()
        self.low_seq += 1

    async def _rpc(self, service: str, api_id: int, parameter: dict[str, Any] | None = None,
                   reply: bool = True) -> tuple[int, Any]:
        assert self._channels is not None
        ch = self._channels
        code, data = await asyncio.wait_for(asyncio.to_thread(
            ch.rpc, service, api_id, parameter or {}, reply=reply, timeout_s=self.rpc_timeout),
            self.rpc_timeout + 1.0)
        if code in api.UNKNOWN_OUTCOME:
            raise UnknownOutcome(f"{service} api {api_id} timed out (code {code}); "
                                 "the robot may have acted")
        return code, data

    async def _check_mode(self) -> None:
        with contextlib.suppress(Exception):
            code, data = await self._rpc(api.MOTION_SWITCHER_SERVICE,
                                         api.MotionSwitcher.CHECK_MODE)
            if code == 0 and isinstance(data, dict):
                self.mode_name = str(data.get("name", ""))
                self.mode_at = self.require_clock().now()

    def _fresh(self, at: float | None, limit: float) -> bool:
        return at is not None and self.require_clock().now() - at <= limit

    # -- RobotAdapter ----------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.config.get("skills", SKILLS))

    def capability_report(self) -> CapabilityReport:
        return self._caps.report(self.robot_id, self.require_clock(),
                                 (self._locomotion(), self._low_level()))

    def _locomotion(self) -> CapabilityProfile:
        if not self._fresh(self.sport_at, self.state_timeout) or self.sport is None:
            return CapabilityProfile("locomotion", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("sport_state_stale",))
        if self.mode_name == "":
            return CapabilityProfile("locomotion", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("sport_mode_released",))
        if int(self.sport.get("error_code", 0)) != 0:
            return CapabilityProfile("locomotion", ProfileStatus.RESTRICTED,
                                     limiting_factors=(f"error_code_{self.sport['error_code']}",))
        return CapabilityProfile("locomotion", ProfileStatus.AVAILABLE,
                                 limits={"max_speed_mps": self.max_speed})

    def _low_level(self) -> CapabilityProfile:
        factors = []
        if not self.low_level_enabled:
            factors.append("disabled_by_config")
        if not self.joint_limits:
            factors.append("no_joint_limits_configured")
        if self.mode_name != "":
            factors.append("sport_mode_active")
        if not self._fresh(self.low_at, self.low_max_age):
            factors.append("low_state_stale")
        return CapabilityProfile("low_level", ProfileStatus.UNAVAILABLE if factors
                                 else ProfileStatus.AVAILABLE, limiting_factors=tuple(factors))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        src = f"{self.robot_id}/unitree"
        out: dict[str, Observation] = {}
        if self.sport is not None and self.sport_at is not None:
            pos = self.sport.get("position", [0.0, 0.0, 0.0])
            rpy = self.sport.get("imu_state", {}).get("rpy", [0.0, 0.0, 0.0])
            at = self.sport_at
            out["sport.pose"] = observation(clock, src, "sport.pose",
                                            {"x": float(pos[0]), "y": float(pos[1]),
                                             "yaw": float(rpy[2])}, acquired_at=at,
                                            frame="odom", spatial=True, unit="m")
            out["sport.body_height"] = observation(clock, src, "sport.body_height",
                                                   float(self.sport.get("body_height", 0.0)),
                                                   acquired_at=at, unit="m")
            out["sport.error_code"] = observation(clock, src, "sport.error_code",
                                                  int(self.sport.get("error_code", 0)),
                                                  acquired_at=at)
        if self.low is not None and self.low_at is not None:
            motors = self.low.get("motor_state", [])
            for i, name in enumerate(api.LEG_JOINTS):
                if i < len(motors):
                    sig = f"low.joint.{name}.q"
                    out[sig] = observation(clock, src, sig, float(motors[i]["q"]),
                                           acquired_at=self.low_at, unit="rad")
            out["low.power_v"] = observation(clock, src, "low.power_v",
                                             float(self.low.get("power_v", 0.0)),
                                             acquired_at=self.low_at, unit="V")
        if self.mode_name is not None and self.mode_at is not None:
            out["low.sport_released"] = observation(clock, src, "low.sport_released",
                                                    self.mode_name == "",
                                                    acquired_at=self.mode_at)
        return out

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.supported_skills() or cmd.skill_id not in SKILLS:
            raise UnsupportedOperation("unitree adapter does not support skill",
                                       skill_id=cmd.skill_id)
        if self._channels is None:
            raise ConnectionError("channel layer not started")
        self._active = cmd.command_id
        try:
            if cmd.skill_id in POSTURES:
                return await self._posture(cmd.skill_id)
            if cmd.skill_id == "stop_move":
                return self._report(await self._rpc(api.SPORT_SERVICE, api.Sport.STOP_MOVE),
                                    "stopped")
            if cmd.skill_id == "walk_to":
                return await self._walk_to(ctx, float(cmd.args["x"]), float(cmd.args["y"]))
            if cmd.skill_id == "release_sport_mode":
                r = self._report(await self._rpc(api.MOTION_SWITCHER_SERVICE,
                                                 api.MotionSwitcher.RELEASE_MODE), "released")
                await self._check_mode()
                return r
            return await self._set_joint(ctx)
        finally:
            await self._gate.settle(cmd.command_id, self.state_timeout * 3)
            self._gate.clear(cmd.command_id)
            self._active = None
            self._interrupt.discard(cmd.command_id)

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active != command_id:
            return InterruptReport(False, False, "command is not active")
        self._interrupt.add(command_id)
        done = self._gate.begin(command_id)
        try:
            return await self._stop_for_interrupt()
        finally:
            done.set()

    async def _stop_for_interrupt(self) -> InterruptReport:
        try:
            code, _ = await self._rpc(api.SPORT_SERVICE, api.Sport.STOP_MOVE)
        except Exception as exc:
            return InterruptReport(True, False, f"StopMove failed: {exc}")
        if code != 0:
            return InterruptReport(True, False, f"StopMove refused (code {code})")
        mark = self.sport_seq
        stopped = await wait_until(
            lambda: self.sport_seq > mark + 1 and self.sport is not None
            and all(abs(float(v)) < 1e-3 for v in self.sport.get("velocity", [1.0])[:2]),
            self.state_timeout * 2)
        return InterruptReport(True, stopped, "velocity observed zero" if stopped
                               else "StopMove accepted; zero velocity not observed")

    # -- skills ----------------------------------------------------------------------
    @staticmethod
    def _report(result: tuple[int, Any], ok: str) -> AdapterReport:
        code, _ = result
        if code != 0:
            return AdapterReport(False, f"robot answered code {code}",
                                 failure_reason=f"rpc_{code}", effects={"code": code})
        return AdapterReport(True, ok, effects={"code": 0})

    async def _posture(self, skill_id: str) -> AdapterReport:
        mark = self.sport_seq
        report = self._report(await self._rpc(api.SPORT_SERVICE, POSTURES[skill_id]), skill_id)
        if report.completed:
            # wait (bounded) for a later state sample; the runtime verifies the posture
            await wait_until(lambda: self.sport_seq > mark + 2, self.posture_timeout)
        return report

    async def _walk_to(self, ctx: ExecutionContext, tx: float, ty: float) -> AdapterReport:
        cmd = ctx.command
        try:
            while True:
                if cmd.command_id in self._interrupt:
                    return AdapterReport(False, "interrupted", failure_reason="interrupted")
                if not ctx.authority_valid():
                    return AdapterReport(False, "authority lost", failure_reason="authority_lost")
                if not self._fresh(self.sport_at, self.state_timeout) or self.sport is None:
                    raise UnknownOutcome("sport state went stale during motion")
                pos = self.sport.get("position", [0.0, 0.0, 0.0])
                yaw = float(self.sport.get("imu_state", {}).get("rpy", [0, 0, 0])[2])
                dx, dy = tx - float(pos[0]), ty - float(pos[1])
                dist = math.hypot(dx, dy)
                if dist <= self.tolerance:
                    break
                err = math.atan2(math.sin(math.atan2(dy, dx) - yaw),
                                 math.cos(math.atan2(dy, dx) - yaw))
                vx = self.max_speed * min(1.0, dist / 0.3) * max(0.0, math.cos(err))
                await self._rpc(api.SPORT_SERVICE, api.Sport.MOVE,
                                {"x": round(vx, 4), "y": 0.0,
                                 "z": round(clamp(2.0 * err, -1.0, 1.0), 4)}, reply=False)
                await asyncio.sleep(self.refresh_s)
        finally:
            with contextlib.suppress(Exception):
                await self._rpc(api.SPORT_SERVICE, api.Sport.STOP_MOVE)
        mark = self.sport_seq
        await wait_until(lambda: self.sport_seq > mark + 1, self.state_timeout)
        return AdapterReport(True, "arrived", effects={"target": {"x": tx, "y": ty}})

    async def _set_joint(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        joint = str(cmd.args["joint"])
        if joint not in api.LEG_JOINTS:
            return AdapterReport(False, f"unknown joint {joint!r}", failure_reason="invalid_joint")
        if not self.low_level_enabled:
            return AdapterReport(False, "low-level control disabled in config",
                                 failure_reason="low_level_disabled")
        if joint not in self.joint_limits:
            return AdapterReport(False, "no operator-configured limit for this joint",
                                 failure_reason="no_joint_limits_configured")
        lo, hi = self.joint_limits[joint]
        requested = float(cmd.args["q"])
        target = clamp(requested, lo, hi)
        kp, kd = float(cmd.args.get("kp", 20.0)), float(cmd.args.get("kd", 0.5))
        duration = float(cmd.args.get("duration_s", 1.0))
        idx = api.LEG_JOINTS.index(joint)
        await self._check_mode()  # re-observe: the capability report may be a tick old
        if self.mode_name != "":
            return AdapterReport(False, "sport service still owns the motors",
                                 failure_reason="sport_mode_active")
        if not self._fresh(self.low_at, self.low_max_age) or self.low is None:
            return AdapterReport(False, "rt/lowstate is stale", failure_reason="stale_low_state")
        start = float(self.low["motor_state"][idx]["q"])
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while True:
            if cmd.command_id in self._interrupt:
                return AdapterReport(False, "interrupted", failure_reason="interrupted")
            if not self._fresh(self.low_at, self.low_max_age):
                raise UnknownOutcome("rt/lowstate went stale while commanding joints")
            frac = min(1.0, (loop.time() - t0) / max(duration, 1e-6))
            self._channels.publish(api.TOPIC_LOWCMD, _low_cmd(idx, start + (target - start)  # type: ignore[union-attr]
                                                              * frac, kp, kd))
            if frac >= 1.0 and (abs(self._joint_q(idx) - target) < 0.02
                                or loop.time() - t0 > duration + self.settle_s):
                break  # holding the final target until observed there (bounded)
            await asyncio.sleep(self.low_period)
        mark = self.low_seq
        await wait_until(lambda: self.low_seq > mark + 1, self.state_timeout)
        limited = ["joint_limit"] if target != requested else []
        return AdapterReport(True, "target commanded",
                             effects={"requested": requested, "applied": target,
                                      "limited_by": limited})


    def _joint_q(self, idx: int) -> float:
        motors = (self.low or {}).get("motor_state", [])
        return float(motors[idx]["q"]) if idx < len(motors) else math.nan


def _low_cmd(idx: int, q: float, kp: float, kd: float) -> dict[str, Any]:
    motors = [{"mode": api.MOTOR_MODE_PMSM, "q": api.POS_STOP_F, "dq": api.VEL_STOP_F,
               "kp": 0.0, "kd": 0.0, "tau": 0.0} for _ in range(api.MOTOR_SLOTS)]
    motors[idx] = {"mode": api.MOTOR_MODE_PMSM, "q": q, "dq": 0.0, "kp": kp, "kd": kd,
                   "tau": 0.0}
    return {"head": list(api.LOWCMD_HEAD), "level_flag": api.LOWLEVEL, "motor_cmd": motors}


__all__ = ["POSTURES", "SKILLS", "UnitreeAdapter", "UnknownOutcome", "unitree_skill_set"]
