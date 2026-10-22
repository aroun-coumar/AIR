"""Microduck RobotAdapter over robotd's JSON-RPC/NDJSON IPC (W3.6, gate G02).

What is materially different from the fake adapters, and how AIR treats it:

- **Continuous intents with no completion event.** ``robot.move`` is a notification that
  robotd forgets after its 500 ms deadman. ``walk_to`` therefore refreshes the intent
  every ``refresh_s`` (default 0.1 s, well under the deadman) from a closed loop on
  contact odometry, and completion is decided by the runtime from ``base.odom``
  postconditions with a wide tolerance (odometry drift on hardware is unmeasured; the
  skill is marked experimental).
- **An external safety authority.** robotd owns the only actuator write handle. A fall
  or a pick-up *restricts* the locomotion profile (report, not gate: AIR never commands
  robot-owned recovery), and a running walk ends FAILED with reason ``fallen``.
- **Lost link means unknown outcome.** A stale state stream or a closed socket after a
  walk started raises, which the runtime records as UNRESOLVED for ``reconcile()``.
- **No protection is implied that the robot lacks.** robotd clamps the actuator range
  only; the capability report carries ``joint_limits:none`` as evidence.

Transport is injected (:meth:`MicroduckAdapter.use_transport`) or opened from
``config["endpoint"]`` (``unix:///run/robotd.sock`` or ``tcp://host:port``).
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
from ..common import CapabilityTracker, clamp, observation, wait_until
from . import protocol as p
from .client import RobotdClient
from .transport import LineTransport, TcpTransport, UnixSocketTransport

__requirements__ = ("REQ03", "REQ05", "REQ12", "REQ13", "REQ14")

SKILLS = ("walk_to", "turn_to", "stand_up", "disable_policy", "stop", "do_skill")


def microduck_skill_set() -> SkillSet:
    text = resources.files(__package__).joinpath("skills.json").read_text("utf-8")
    return SkillSet.from_json(text)


class MicroduckAdapter(RobotAdapter):
    kind = "microduck"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        self.endpoint = str(self.config.get("endpoint", f"unix://{p.ROBOT_SOCKET}"))
        self.refresh_s = float(self.config.get("refresh_s", 0.1))
        if not 0 < self.refresh_s < p.DEADMAN_S:
            raise ValueError("refresh_s must be positive and below robotd's 0.5 s deadman")
        self.max_speed = float(self.config.get("max_speed_mps", 0.2))
        self.max_turn = float(self.config.get("max_turn_rps", 1.0))
        self.tolerance = float(self.config.get("goal_tolerance_m", 0.05))
        self.yaw_tolerance = float(self.config.get("yaw_tolerance_rad", 0.05))
        self.state_hz = int(self.config.get("state_hz", 20))
        self.state_timeout = float(self.config.get("state_timeout_s", 0.5))
        self.low_battery = float(self.config.get("low_battery_pct", 30.0))
        self.timeout_s = float(self.config.get("timeout_s", 2.0))
        self._transport: LineTransport | None = None
        self.client: RobotdClient | None = None
        self.state: dict[str, Any] | None = None
        self.state_at: float | None = None
        self.state_seq = 0
        self.health: dict[str, Any] | None = None
        self.health_at: float | None = None
        self.remote_skills: tuple[str, ...] = ()
        self._caps = CapabilityTracker()
        self._active: str | None = None
        self._interrupt: set[str] = set()
        self._interrupt_done: dict[str, asyncio.Event] = {}

    # -- connection ----------------------------------------------------------------
    def use_transport(self, transport: LineTransport) -> None:
        """Inject the line transport (tests, duck-sim, a forwarded socket)."""
        self._transport = transport

    async def _open(self) -> LineTransport:
        if self._transport is not None:
            return self._transport
        if self.endpoint.startswith("unix://"):
            return await UnixSocketTransport(self.endpoint[len("unix://"):])
        if self.endpoint.startswith("tcp://"):
            host, _, port = self.endpoint[len("tcp://"):].rpartition(":")
            return await TcpTransport(host, int(port))
        raise UnsupportedOperation("unsupported robotd endpoint", endpoint=self.endpoint)

    async def start(self) -> None:
        transport = await self._open()
        self.client = RobotdClient(transport, timeout_s=self.timeout_s, on_state=self._on_state)
        await self.client.start()
        ack = await self.client.call(p.Method.SUBSCRIBE, {"hz": self.state_hz})
        if isinstance(ack, dict):
            self.remote_skills = tuple(str(s) for s in ack.get("skills", []))
        await self._refresh_health()

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()

    def _on_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self.state_at = self.require_clock().now()
        self.state_seq += 1

    async def _refresh_health(self) -> None:
        if self.client is None:
            return
        with contextlib.suppress(Exception):
            result = await self.client.call(p.Method.HEALTH)
            if isinstance(result, dict):
                self.health = result
                self.health_at = self.require_clock().now()

    def _connected(self) -> bool:
        return self.client is not None and self.client.connected

    def _state_fresh(self) -> bool:
        return (self.state_at is not None
                and self.require_clock().now() - self.state_at <= self.state_timeout)

    # -- RobotAdapter --------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.config.get("skills", SKILLS))

    def capability_report(self) -> CapabilityReport:
        clock = self.require_clock()
        return self._caps.report(self.robot_id, clock, (self._locomotion(), self._head(),
                                                         self._control_loop()))

    def _locomotion(self) -> CapabilityProfile:
        evidence = ("joint_limits:none", "safety_authority:robotd")
        if not self._connected():
            return CapabilityProfile("locomotion", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("link_down",), evidence=evidence)
        if not self._state_fresh() or self.state is None:
            return CapabilityProfile("locomotion", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("state_stale",), evidence=evidence)
        hello = self.client.hello if self.client else None
        factors: list[str] = []
        safety = self.state.get("safety", {})
        if safety.get("fallen"):
            factors.append("fallen")
        if safety.get("picked_up"):
            factors.append("picked_up")
        if self.state.get("policy") == "held" and not factors:
            factors.append("policy_held")
        battery = (self.health or {}).get("battery")
        if isinstance(battery, dict) and float(battery.get("percent", 100.0)) < self.low_battery:
            factors.append("battery_low")
        if hello and hello.get("api_version") != p.API_VERSION:
            factors.append("api_version_mismatch")
        status = ProfileStatus.RESTRICTED if factors else ProfileStatus.AVAILABLE
        return CapabilityProfile("locomotion", status, limits={"max_speed_mps": self.max_speed},
                                 limiting_factors=tuple(factors), evidence=evidence)

    def _head(self) -> CapabilityProfile:
        ok = self._connected() and self._state_fresh()
        return CapabilityProfile("head", ProfileStatus.AVAILABLE if ok
                                 else ProfileStatus.UNAVAILABLE,
                                 limiting_factors=() if ok else ("state_stale",))

    def _control_loop(self) -> CapabilityProfile:
        if not (self._connected() and self._state_fresh() and self.state):
            return CapabilityProfile("control_loop", ProfileStatus.UNAVAILABLE,
                                     limiting_factors=("state_stale",))
        loop = self.state.get("loop", {})
        hz = float(loop.get("hz", 0.0))
        return CapabilityProfile(
            "control_loop", ProfileStatus.AVAILABLE if hz >= 40.0 else ProfileStatus.RESTRICTED,
            limits={"hz": hz, "missed": float(loop.get("missed", 0))},
            limiting_factors=() if hz >= 40.0 else ("loop_rate_low",))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        src = f"{self.robot_id}/robotd"
        out: dict[str, Observation] = {
            "link.connected": observation(clock, src, "link.connected", self._connected()),
        }
        if self.state is not None and self.state_at is not None:
            st, at = self.state, self.state_at
            odom = st.get("odom", {})
            pos = odom.get("position", [0.0, 0.0, 0.0])
            safety = st.get("safety", {})
            move = st.get("move", {})
            applied = move.get("applied", [0.0, 0.0, 0.0])

            def o(signal: str, value: Any, **kw: Any) -> Observation:
                return observation(clock, src, signal, value, acquired_at=at, **kw)

            out.update({
                "base.odom": o("base.odom", {"x": float(pos[0]), "y": float(pos[1]),
                                             "yaw": float(odom.get("yaw", 0.0))},
                               frame="odom", spatial=True, unit="m"),
                "safety.fallen": o("safety.fallen", bool(safety.get("fallen", False))),
                "safety.picked_up": o("safety.picked_up", bool(safety.get("picked_up", False))),
                "policy.active": o("policy.active", str(st.get("policy", ""))),
                "policy.driving": o("policy.driving", st.get("policy") in ("walk", "stand")),
                "motion.applied": o("motion.applied", {"vx": applied[0], "vy": applied[1],
                                                       "vyaw": applied[2]}),
                "motion.limited_by": o("motion.limited_by", list(move.get("limited_by", []))),
                "loop.hz": o("loop.hz", float(st.get("loop", {}).get("hz", 0.0)), unit="Hz"),
            })
        battery = (self.health or {}).get("battery")
        if isinstance(battery, dict) and self.health_at is not None:
            out["battery.charge"] = observation(clock, src, "battery.charge",
                                                float(battery.get("percent", 0.0)), unit="%",
                                                acquired_at=self.health_at)
        return out

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.supported_skills() or cmd.skill_id not in SKILLS:
            raise UnsupportedOperation("microduck adapter does not support skill",
                                       skill_id=cmd.skill_id)
        if self.client is None or not self.client.connected:
            raise ConnectionError("robotd link is down")
        self._active = cmd.command_id
        try:
            if cmd.skill_id == "walk_to":
                return await self._walk_to(ctx, float(cmd.args["x"]), float(cmd.args["y"]))
            if cmd.skill_id == "turn_to":
                return await self._turn_to(ctx, float(cmd.args["yaw"]))
            if cmd.skill_id == "stand_up":
                r = await self._discrete(p.Method.INIT)
                if r.completed:
                    r = await self._discrete(p.Method.ENABLE, {"on": True})
                await self._settle_state()
                return r
            if cmd.skill_id == "disable_policy":
                r = await self._discrete(p.Method.ENABLE, {"on": False})
                await self._settle_state()
                return r
            if cmd.skill_id == "stop":
                r = await self._discrete(p.Method.STOP)
                await self._settle_state()
                return r
            r = await self._discrete(p.Method.DO, {"skill": str(cmd.args["skill"])})
            await self._refresh_health()
            return r
        finally:
            self._active = None
            self._interrupt.discard(cmd.command_id)
            self._interrupt_done.pop(cmd.command_id, None)

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active != command_id:
            return InterruptReport(False, False, "command is not active")
        done = self._interrupt_done.setdefault(command_id, asyncio.Event())
        self._interrupt.add(command_id)
        try:
            return await self._stop_for_interrupt()
        finally:
            done.set()

    async def _stop_for_interrupt(self) -> InterruptReport:
        if self.client is None or not self.client.connected:
            return InterruptReport(True, False, "link down; stop cannot be confirmed")
        try:
            r = await self.client.call(p.Method.STOP)
        except Exception as exc:
            return InterruptReport(True, False, f"stop request failed: {exc}")
        if not (isinstance(r, dict) and r.get("accepted")):
            return InterruptReport(True, False, "robotd refused stop")
        stopped = await self._settle_state()
        return InterruptReport(True, stopped, "stopped (applied velocity observed zero)"
                               if stopped else "stop accepted; zero velocity not yet observed")

    # -- skills --------------------------------------------------------------------
    async def _discrete(self, method: str, params: dict[str, Any] | None = None
                        ) -> AdapterReport:
        assert self.client is not None
        result = await self.client.call(method, params)
        if not isinstance(result, dict):
            return AdapterReport(False, "malformed IntentResult", failure_reason="protocol")
        if not result.get("accepted"):
            return AdapterReport(False, str(result.get("reason") or "refused by robotd"),
                                 failure_reason="refused_by_robot",
                                 effects={"reason": result.get("reason")})
        return AdapterReport(True, str(result.get("reason") or "accepted"),
                             effects={"accepted": True})

    async def _settle_state(self) -> bool:
        """Wait (bounded) for a fresh state frame with zero applied velocity."""
        mark = self.state_seq

        def settled() -> bool:
            # sequence, not time: host monotonic clocks can be coarse (about 15 ms)
            if self.state is None or self.state_seq <= mark:
                return False
            applied = self.state.get("move", {}).get("applied", [1.0])
            return all(abs(float(v)) < 1e-6 for v in applied)

        return await wait_until(settled, self.state_timeout * 2)

    def _guard(self, ctx: ExecutionContext) -> AdapterReport | None:
        cmd = ctx.command
        if cmd.command_id in self._interrupt:
            return AdapterReport(False, "interrupted", self._pose(), failure_reason="interrupted")
        if not ctx.authority_valid():
            return AdapterReport(False, "authority lost", self._pose(),
                                 failure_reason="authority_lost")
        if not self._state_fresh():
            raise ConnectionError("robotd state stream went stale during motion")
        assert self.state is not None
        safety = self.state.get("safety", {})
        if safety.get("fallen"):
            return AdapterReport(False, "robot fell", self._pose(), failure_reason="fallen")
        if safety.get("picked_up"):
            return AdapterReport(False, "robot was picked up", self._pose(),
                                 failure_reason="picked_up")
        return None

    async def _drive(self, ctx: ExecutionContext, intent: Any) -> AdapterReport:
        """Refresh a continuous intent until it reports done; always end with a stop."""
        assert self.client is not None
        ticks = 0
        try:
            while True:
                stop = self._guard(ctx)
                if stop is not None:
                    done = self._interrupt_done.get(ctx.command.command_id)
                    if done is not None:  # let the interrupt report its own confirmation
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(done.wait(), self.state_timeout * 3)
                    return stop
                twist = intent()
                if twist is None:
                    break
                await self.client.notify(p.Method.MOVE, twist)
                ticks += 1
                if ticks % 10 == 1:
                    ctx.progress("moving", self._pose())
                await asyncio.sleep(self.refresh_s)
        finally:
            if self.client.connected:
                with contextlib.suppress(Exception):
                    await self.client.call(p.Method.STOP)
        await self._settle_state()
        return AdapterReport(True, "intent complete", self._pose())

    async def _walk_to(self, ctx: ExecutionContext, tx: float, ty: float) -> AdapterReport:
        def intent() -> dict[str, float] | None:
            pose = self._pose()["odom"]
            dx, dy = tx - pose["x"], ty - pose["y"]
            dist = math.hypot(dx, dy)
            if dist <= self.tolerance:
                return None
            err = math.atan2(math.sin(math.atan2(dy, dx) - pose["yaw"]),
                             math.cos(math.atan2(dy, dx) - pose["yaw"]))
            vx = self.max_speed * min(1.0, dist / 0.2) * max(0.0, math.cos(err)) \
                if abs(err) < 0.6 else 0.0
            return {"vx": round(vx, 4), "vy": 0.0,
                    "vyaw": round(clamp(2.0 * err, -self.max_turn, self.max_turn), 4)}

        return await self._drive(ctx, intent)

    async def _turn_to(self, ctx: ExecutionContext, yaw: float) -> AdapterReport:
        def intent() -> dict[str, float] | None:
            cur = self._pose()["odom"]["yaw"]
            err = math.atan2(math.sin(yaw - cur), math.cos(yaw - cur))
            if abs(err) <= self.yaw_tolerance:
                return None
            return {"vx": 0.0, "vy": 0.0,
                    "vyaw": round(clamp(2.0 * err, -self.max_turn, self.max_turn), 4)}

        return await self._drive(ctx, intent)

    def _pose(self) -> dict[str, Any]:
        st = self.state or {}
        odom = st.get("odom", {})
        pos = odom.get("position", [0.0, 0.0, 0.0])
        return {"odom": {"x": round(float(pos[0]), 4), "y": round(float(pos[1]), 4),
                         "yaw": round(float(odom.get("yaw", 0.0)), 4)},
                "limited_by": list(st.get("move", {}).get("limited_by", []))}


__all__ = ["SKILLS", "MicroduckAdapter", "microduck_skill_set"]
