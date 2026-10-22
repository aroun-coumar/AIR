"""A fake robotd that speaks the verified duck-ipc-proto subset (ADR 0019).

It is strict where robotd is strict: parameters deny unknown fields (``-32602`` naming the
member), unknown methods are ``-32601``, continuous intents must arrive as notifications,
and a velocity intent expires after the 500 ms deadman. It integrates a planar body in
its own simulated time (``sim_dt_s`` per tick) so tests are fast and deterministic, and
it publishes ``robot.state`` with ``requested``/``applied``/``limited_by``.

Fault injection: ``fall()``, ``pick_up()``, ``drop_link()``, ``refuse_enable(reason)``.
This is a test double for AIR's adapter, not a model of the robot's dynamics.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from typing import Any

from . import protocol as p
from .transport import LineTransport

__requirements__ = ("REQ03", "REQ18")

_PARAMS: dict[str, tuple[set[str], set[str]]] = {
    # method: (allowed fields, required fields)
    p.Method.HELLO: ({"api_version"}, {"api_version"}),
    p.Method.MOVE: ({"vx", "vy", "vyaw"}, set()),
    p.Method.ENABLE: ({"on", "toggle"}, {"on"}),
    p.Method.DO: ({"skill"}, {"skill"}),
    p.Method.SUBSCRIBE: ({"hz"}, set()),
}
_NO_PARAMS = {p.Method.HEALTH, p.Method.SAFE_TO_RESTART, p.Method.STOP, p.Method.INIT,
              p.Method.RELAX, p.Method.POLICIES}


class FakeRobotd:
    def __init__(self, *, sim_dt_s: float = 0.02, tick_s: float = 0.001,
                 max_vx: float = 0.3, max_vyaw: float = 1.5, skills: tuple[str, ...] = (
                     "sitstand", "ground_pick"), battery_v: float = 7.8,
                 api_version: int = p.API_VERSION) -> None:
        self.sim_dt = sim_dt_s
        self.tick_s = tick_s
        self.max_vx = max_vx
        self.max_vyaw = max_vyaw
        self.skills = skills
        self.battery_v = battery_v
        self.api_version = api_version
        self.t = 0.0
        self.powered = False
        self.enabled = False
        self.fallen = False
        self.picked_up = False
        self.x = self.y = self.yaw = 0.0
        self.requested = [0.0, 0.0, 0.0]
        self.applied = [0.0, 0.0, 0.0]
        self.limited_by: list[str] = []
        self.last_intent_t = -1e9
        self.missed = 0
        self.received: list[dict[str, Any]] = []
        self._refuse_enable: str | None = None
        self._subs: dict[int, tuple[LineTransport, int]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._links: list[LineTransport] = []
        self._loop_task: asyncio.Task[None] | None = None
        self._tick = 0

    # -- fault injection -----------------------------------------------------------
    def fall(self) -> None:
        self.fallen = True
        self.enabled = False

    def stand_up(self) -> None:
        self.fallen = False

    def pick_up(self, held: bool = True) -> None:
        self.picked_up = held

    def refuse_enable(self, reason: str | None) -> None:
        self._refuse_enable = reason

    async def drop_link(self) -> None:
        for link in list(self._links):
            with contextlib.suppress(Exception):
                await link.close()
        self._links.clear()
        self._subs.clear()

    # -- lifecycle -----------------------------------------------------------------
    def serve(self, transport: LineTransport) -> None:
        self._links.append(transport)
        task = asyncio.get_running_loop().create_task(self._serve(transport))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._control_loop())

    async def close(self) -> None:
        for t in [*self._tasks, *([self._loop_task] if self._loop_task else [])]:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # -- the wire ------------------------------------------------------------------
    async def _serve(self, link: LineTransport) -> None:
        try:
            while True:
                line = await link.recv()
                reply = self._handle(line, link)
                if reply is not None:
                    await link.send(json.dumps(reply) + "\n")
        except ConnectionError:
            self._subs = {k: v for k, v in self._subs.items() if v[0] is not link}

    def _handle(self, line: str, link: LineTransport) -> dict[str, Any] | None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return _error(None, p.Code.PARSE_ERROR, "parse error")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None,
                          p.Code.INVALID_REQUEST, "invalid request")
        self.received.append(msg)
        rid, method, params = msg.get("id"), msg["method"], msg.get("params", {})
        is_note = "id" not in msg
        if method in p.Method.CONTINUOUS and not is_note:
            return _error(rid, p.Code.INVALID_REQUEST,
                          f"{method} is a continuous intent; send it as a notification")
        if method not in _PARAMS and method not in _NO_PARAMS:
            return None if is_note else _error(rid, p.Code.METHOD_NOT_FOUND,
                                                f"method not found: {method}")
        if not isinstance(params, dict):
            return None if is_note else _error(rid, p.Code.INVALID_PARAMS, "params")
        allowed, required = _PARAMS.get(method, (set(), set()))
        unknown = sorted(set(params) - allowed)
        missing = sorted(required - set(params))
        if unknown or missing:
            return None if is_note else _error(
                rid, p.Code.INVALID_PARAMS,
                f"unknown field `{unknown[0]}`" if unknown else f"missing field `{missing[0]}`")
        result = self._dispatch(method, params, link, rid)
        return None if is_note else {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any], link: LineTransport,
                  rid: Any) -> Any:
        if method == p.Method.HELLO:
            return {"api_version": self.api_version, "daemon_version": "0.0.0-fake",
                    "revision": None}
        if method == p.Method.HEALTH:
            pct = max(0.0, min(100.0, (self.battery_v - 6.6) / (8.4 - 6.6) * 100))
            return {"healthy": True, "battery": {"volts": self.battery_v,
                                                 "percent": round(pct, 1)}}
        if method == p.Method.SAFE_TO_RESTART:
            moving = any(abs(v) > 1e-9 for v in self.applied)
            return {"safe": not moving} | ({"reason": "moving"} if moving else {})
        if method == p.Method.SUBSCRIBE:
            hz = params.get("hz")
            every = 1 if not hz else max(1, round(1.0 / (float(hz) * self.sim_dt)))
            self._subs[id(link)] = (link, every)
            return {"accepted": True, "walk": "walk-fake.onnx", "skills": list(self.skills)}
        if method == p.Method.MOVE:
            v = [float(params.get("vx", 0.0)), float(params.get("vy", 0.0)),
                 float(params.get("vyaw", 0.0))]
            if all(math.isfinite(x) for x in v):
                self.requested = v
                self.last_intent_t = self.t
            return None
        if method == p.Method.STOP:
            self.requested = [0.0, 0.0, 0.0]
            self.last_intent_t = -1e9
            return {"accepted": True}
        if method == p.Method.INIT:
            if self.fallen:
                return {"accepted": False, "reason": "fallen"}
            self.powered = True
            return {"accepted": True}
        if method == p.Method.RELAX:
            self.powered = False
            self.enabled = False
            return {"accepted": True}
        if method == p.Method.ENABLE:
            on = bool(params["on"]) if not params.get("toggle") else not self.enabled
            if on and self.fallen:
                return {"accepted": False, "reason": "fallen"}
            if on and self._refuse_enable:
                return {"accepted": False, "reason": self._refuse_enable}
            self.enabled = on
            self.powered = self.powered or on
            return {"accepted": True}
        if method == p.Method.DO:
            if params["skill"] not in self.skills:
                return {"accepted": False,
                        "reason": f"unknown skill; known: {', '.join(self.skills)}"}
            if self.fallen or not self.powered:
                return {"accepted": False, "reason": "not standing"}
            return {"accepted": True}
        if method == p.Method.POLICIES:
            return {"walk": {"file": "walk-fake.onnx"}}
        return None

    # -- simulation ----------------------------------------------------------------
    async def _control_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            self._step()
            self._tick += 1
            for link, every in list(self._subs.values()):
                if self._tick % every == 0:
                    with contextlib.suppress(Exception):
                        await link.send(json.dumps({"jsonrpc": "2.0", "method": p.Method.STATE,
                                                    "params": self.state()}) + "\n")

    def _step(self) -> None:
        self.t += self.sim_dt
        if self.t - self.last_intent_t > p.DEADMAN_S:
            self.requested = [0.0, 0.0, 0.0]
        self.limited_by = []
        if not (self.powered and self.enabled) or self.fallen or self.picked_up:
            self.applied = [0.0, 0.0, 0.0]
            if any(abs(v) > 1e-9 for v in self.requested):
                self.limited_by = ["fallen" if self.fallen else
                                   "picked_up" if self.picked_up else "policy_disabled"]
        else:
            vx = max(-self.max_vx, min(self.max_vx, self.requested[0]))
            vy = max(-self.max_vx, min(self.max_vx, self.requested[1]))
            vyaw = max(-self.max_vyaw, min(self.max_vyaw, self.requested[2]))
            self.applied = [vx, vy, vyaw]
            if self.applied != self.requested:
                self.limited_by = ["max_speed"]
        vx, vy, vyaw = self.applied
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        self.x += (vx * c - vy * s) * self.sim_dt
        self.y += (vx * s + vy * c) * self.sim_dt
        self.yaw = math.atan2(math.sin(self.yaw + vyaw * self.sim_dt),
                              math.cos(self.yaw + vyaw * self.sim_dt))

    def state(self) -> dict[str, Any]:
        moving = any(abs(v) > 1e-9 for v in self.applied)
        policy = ("held" if (self.picked_up or self.fallen or not self.enabled)
                  else "walk" if moving else "stand")
        out: dict[str, Any] = {
            "t": round(self.t, 4),
            "move": {"requested": list(self.requested), "applied": list(self.applied)},
            "head": [0.0, 0.0, 0.0, 0.0],
            "policy": policy,
            "safety": {"fallen": self.fallen, "limp": self.fallen,
                       "gravity": [0.0, 0.0, -1.0] if not self.fallen else [1.0, 0.0, 0.0],
                       "gain": 30 if self.powered else None, "picked_up": self.picked_up},
            "loop": {"hz": 50.0, "missed": self.missed},
            "joints": [0.0] * len(p.JOINT_NAMES),
            "targets": [0.0] * len(p.JOINT_NAMES),
            "odom": {"position": [self.x, self.y, 0.12], "yaw": self.yaw},
            "t_ns": int(self.t * 1e9),
        }
        if self.limited_by:
            out["move"]["limited_by"] = list(self.limited_by)
        return out


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


__all__ = ["FakeRobotd"]
