"""A fake Unitree DDS graph behind :class:`~.channels.ChannelLayer` (ADR 0019).

It answers the ``sport`` and ``motion_switcher`` RPC ids verified from unitree_sdk2_python,
publishes ``SportModeState_``-shaped and ``LowState_``-shaped dicts, and applies
``LowCmd_``-shaped dicts only after the sport service was released (as the low-level
example requires). ``Move`` is fire-and-forget and expires after ``move_timeout_s``.
Faults: ``silence()`` (stop publishing), ``timeout_next_call()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import Any

from . import api
from .channels import Handler

__requirements__ = ("REQ03", "REQ18")


class FakeUnitree:
    def __init__(self, *, dt_s: float = 0.02, tick_s: float = 0.001,
                 move_timeout_s: float = 1.0, max_speed: float = 1.0) -> None:
        self.dt = dt_s
        self.tick_s = tick_s
        self.move_timeout = move_timeout_s
        self.max_speed = max_speed
        self.t = 0.0
        self.posture = "lying"
        self.mode_name = "normal"          # motion_switcher: "" once released
        self.x = self.y = self.yaw = 0.0
        self.vel = [0.0, 0.0, 0.0]
        self.last_move_t = -1e9
        self.q = [0.0] * api.MOTOR_SLOTS
        self.calls: list[tuple[str, int, dict[str, Any], bool]] = []
        self.lowcmds = 0
        self.ignored_lowcmds = 0
        self._subs: dict[str, list[Handler]] = {}
        self._task: asyncio.Task[None] | None = None
        self._silent = False
        self._timeout_next = False
        self._targets: dict[int, tuple[float, float]] = {}

    # -- faults ----------------------------------------------------------------------
    def silence(self, on: bool = True) -> None:
        self._silent = on

    def timeout_next_call(self) -> None:
        self._timeout_next = True

    # -- ChannelLayer ----------------------------------------------------------------
    def rpc(self, service: str, api_id: int, parameter: dict[str, Any], *, reply: bool,
            timeout_s: float) -> tuple[int, Any]:
        self.calls.append((service, api_id, dict(parameter), reply))
        if self._timeout_next:
            self._timeout_next = False
            return api.RpcCode.CLIENT_API_TIMEOUT, None
        if service == api.MOTION_SWITCHER_SERVICE:
            if api_id == api.MotionSwitcher.CHECK_MODE:
                return 0, {"name": self.mode_name, "form": "0"}
            if api_id == api.MotionSwitcher.RELEASE_MODE:
                self.mode_name = ""
                self.posture = "damp"
                return 0, None
            if api_id == api.MotionSwitcher.SELECT_MODE:
                self.mode_name = str(parameter.get("name", "normal"))
                return 0, None
            return api.RpcCode.SERVER_API_NOT_IMPL, None
        if service != api.SPORT_SERVICE:
            return api.RpcCode.CLIENT_API_NOT_REG, None
        if self.mode_name == "":
            return api.RpcCode.SPORT_SERVER_NOT_INIT, None
        if api_id == api.Sport.MOVE:
            if self.posture not in ("standing", "balance"):
                return 0, None             # no reply: the caller cannot know
            self.vel = [_clamp(float(parameter.get(k, 0.0)), self.max_speed)
                        for k in ("x", "y", "z")]
            self.last_move_t = self.t
            return 0, None
        posture = {api.Sport.STAND_UP: "standing", api.Sport.STAND_DOWN: "lying",
                   api.Sport.BALANCE_STAND: "balance", api.Sport.RECOVERY_STAND: "standing",
                   api.Sport.DAMP: "damp"}.get(api_id)
        if api_id == api.Sport.STOP_MOVE:
            self.vel = [0.0, 0.0, 0.0]
            return 0, None
        if posture is None:
            return api.RpcCode.SERVER_API_NOT_IMPL, None
        self.posture = posture
        self.vel = [0.0, 0.0, 0.0]
        return 0, None

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs.setdefault(topic, []).append(handler)
        if self._task is None:
            with contextlib.suppress(RuntimeError):
                self._task = asyncio.get_running_loop().create_task(self._loop())

    def publish(self, topic: str, sample: dict[str, Any]) -> None:
        if topic != api.TOPIC_LOWCMD:
            return
        if (tuple(sample.get("head", ())) != api.LOWCMD_HEAD
                or sample.get("level_flag") != api.LOWLEVEL
                or len(sample.get("motor_cmd", [])) != api.MOTOR_SLOTS):
            self.ignored_lowcmds += 1
            return
        if self.mode_name != "":
            self.ignored_lowcmds += 1      # sport service still owns the motors
            return
        self.lowcmds += 1
        self._targets = {i: (float(m["q"]), float(m["kp"]))
                         for i, m in enumerate(sample["motor_cmd"])
                         if m["kp"] > 0 and abs(m["q"]) < api.POS_STOP_F / 2}

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()

    # -- simulation ----------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            self.step()

    def step(self) -> None:
        self.t += self.dt
        if self.t - self.last_move_t > self.move_timeout:
            self.vel = [0.0, 0.0, 0.0]
        vx, vy, vyaw = self.vel
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        self.x += (vx * c - vy * s) * self.dt
        self.y += (vx * s + vy * c) * self.dt
        self.yaw += vyaw * self.dt
        for i, (q, kp) in self._targets.items():
            self.q[i] += (q - self.q[i]) * min(1.0, kp * self.dt * 0.5)
        if self._silent:
            return
        sport = self.sport_state()
        low = self.low_state()
        for h in self._subs.get(api.TOPIC_SPORT_STATE, []):
            h(sport)
        for h in self._subs.get(api.TOPIC_LOWSTATE, []):
            h(low)

    def sport_state(self) -> dict[str, Any]:
        height = {"standing": 0.32, "balance": 0.32, "lying": 0.08, "damp": 0.06}[self.posture]
        sec = int(self.t)
        return {"stamp": {"sec": sec, "nanosec": int((self.t - sec) * 1e9)}, "error_code": 0,
                "imu_state": {"quaternion": [math.cos(self.yaw / 2), 0.0, 0.0,
                                             math.sin(self.yaw / 2)],
                              "gyroscope": [0.0, 0.0, self.vel[2]],
                              "accelerometer": [0.0, 0.0, 9.81],
                              "rpy": [0.0, 0.0, self.yaw], "temperature": 40},
                "mode": 1 if self.posture == "balance" else 0, "progress": 0.0, "gait_type": 0,
                "foot_raise_height": 0.08, "position": [self.x, self.y, height],
                "body_height": height, "velocity": [self.vel[0], self.vel[1], 0.0],
                "yaw_speed": self.vel[2], "range_obstacle": [0.0] * 4, "foot_force": [0] * 4,
                "foot_position_body": [0.0] * 12, "foot_speed_body": [0.0] * 12,
                "path_point": []}

    def low_state(self) -> dict[str, Any]:
        return {"head": [0xFE, 0xEF], "level_flag": api.LOWLEVEL, "tick": int(self.t * 1000),
                "imu_state": {"rpy": [0.0, 0.0, self.yaw], "temperature": 40},
                "motor_state": [{"mode": 1, "q": q, "dq": 0.0, "tau_est": 0.0,
                                 "temperature": 35, "lost": 0} for q in self.q],
                "power_v": 28.6, "power_a": 1.2, "foot_force": [0] * 4}


def _clamp(v: float, limit: float) -> float:
    return max(-limit, min(limit, v))


__all__ = ["FakeUnitree"]
