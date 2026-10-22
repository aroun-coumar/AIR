"""VDA 5050 bridge: AIR commitments as orders and instant actions (ADR 0019).

AIR acts as fleet control for the commitments it makes; the vehicle keeps its own
navigation and safety. Mapping:

- An admitted ``drive_to_node`` becomes an ``order``: base ``[current node] -> [target]``
  released, new ``orderId``, ``orderUpdateId`` 0. Completion is decided by the runtime
  from ``state`` (``lastNodeId``, empty ``nodeStates``, not driving), never from the
  publish. An error referencing our ``orderId`` (e.g. a rejected order) is a FAILED
  outcome with that error type.
- ``CONNECTION_BROKEN``/``OFFLINE`` (or a stale state) while an order is running leaves the
  outcome unknown: per VDA 5050 the vehicle completes its released base on its own, so
  the runtime records UNRESOLVED and ``reconcile()`` settles it from the next state.
- Cancel is the instant action ``cancelOrder``; the stop is confirmed only when that
  action's state reads ``FINISHED`` (it stays RUNNING until the vehicle has stopped).
- ``pause``/``resume`` map to ``startPause``/``stopPause`` and verify ``paused``.
- ``state``, ``connection`` and ``factsheet`` become observations and capability.

The MQTT client is injected: anything with ``publish(topic, payload, qos, retain)``, with
incoming messages fed to :meth:`VdaAdapter.on_message`. :class:`PahoLink` is a lazy
paho-mqtt binding (extra ``[vda5050]``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol

from ...contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ...contracts.manifest import ComponentManifest
from ...contracts.observation import Observation
from ...contracts.skill import SkillSet
from ...core.errors import SchemaError, UnsupportedOperation
from ...core.ids import new_id
from ...plugins import AdapterReport, ExecutionContext, InterruptReport, RobotAdapter
from ..common import CapabilityTracker, InterruptGate, observation, wait_until
from . import messages as m

__requirements__ = ("REQ03", "REQ06", "REQ09", "REQ14", "REQ17")

SKILLS = ("drive_to_node", "pause", "resume")


class MqttPublisher(Protocol):
    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any: ...


def vda_skill_set() -> SkillSet:
    from importlib import resources
    text = resources.files(__package__).joinpath("skills.json").read_text("utf-8")
    return SkillSet.from_json(text)


class VdaAdapter(RobotAdapter):
    kind = "vda5050"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        self.d = m.dialect(str(self.config.get("version", "3.0.0")),
                           self.config.get("interface_name"))
        self.headers = m.Headers(self.d, str(self.config["manufacturer"]),
                                 str(self.config["serial_number"]))
        self.map_id = str(self.config.get("map_id", "map"))
        self.state_timeout = float(self.config.get("state_timeout_s", 3.0))
        self.order_timeout = float(self.config.get("order_timeout_s", 600.0))
        self._pub: MqttPublisher | None = None
        self.state: m.RobotState | None = None
        self.state_at: float | None = None
        self.state_seq = 0
        self.connection: str | None = None
        self.connection_at: float | None = None
        self.factsheet: dict[str, Any] | None = None
        self.rejected: list[dict[str, Any]] = []
        self._caps = CapabilityTracker()
        self._active: tuple[str, str] | None = None      # (command_id, orderId)
        self._interrupt: set[str] = set()
        self._gate = InterruptGate()

    # -- transport -------------------------------------------------------------------
    def use_publisher(self, publisher: MqttPublisher) -> None:
        self._pub = publisher

    def subscriptions(self) -> list[str]:
        return [self.headers.topic(t) for t in ("state", "connection", "factsheet")]

    def _publish(self, topic: str, msg: dict[str, Any]) -> None:
        if self._pub is None:
            raise ConnectionError("no MQTT publisher attached")
        self._pub.publish(self.headers.topic(topic), json.dumps(msg).encode("utf-8"), 0, False)

    async def start(self) -> None:
        if self._pub is None:
            raise UnsupportedOperation("attach an MQTT publisher with use_publisher() "
                                       "(or PahoLink) before starting")
        requests = [m.action("stateRequest", new_id("act"), "NONE", d=self.d)]
        if self.d.major == 3:
            requests.append(m.action("factsheetRequest", new_id("act"), "NONE", d=self.d))
        self._publish("instantActions", m.instant_actions(self.headers, requests))

    def on_message(self, topic: str, payload: bytes | str) -> None:
        """Feed one MQTT message for this vehicle (call on the runtime's event loop)."""
        name = topic.rsplit("/", 1)[-1]
        try:
            msg = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self.rejected.append({"topic": topic, "reason": "malformed json"})
            return
        if not isinstance(msg, dict) or msg.get("serialNumber") != self.headers.serial_number:
            self.rejected.append({"topic": topic, "reason": "not for this vehicle"})
            return
        now = self.require_clock().now()
        if name == "state":
            try:
                self.state = m.read_state(self.d, msg)
            except (SchemaError, KeyError, TypeError, ValueError) as exc:
                self.rejected.append({"topic": topic, "reason": str(exc)})
                return
            self.state_at, self.state_seq = now, self.state_seq + 1
        elif name == "connection":
            self.connection, self.connection_at = str(msg.get("connectionState")), now
        elif name == "factsheet":
            self.factsheet = msg

    # -- RobotAdapter ----------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.config.get("skills", SKILLS))

    def _online(self) -> bool:
        return self.connection == "ONLINE"

    def _fresh(self) -> bool:
        return (self.state_at is not None
                and self.require_clock().now() - self.state_at <= self.state_timeout)

    def capability_report(self) -> CapabilityReport:
        factors: list[str] = []
        status = ProfileStatus.AVAILABLE
        st = self.state
        if not self._online():
            factors.append(f"connection_{(self.connection or 'unknown').lower()}")
        if not self._fresh() or st is None:
            factors.append("state_stale")
        if st is not None:
            if st.operating_mode not in ("AUTOMATIC", "SEMIAUTOMATIC"):
                factors.append(f"operating_mode_{st.operating_mode.lower()}")
            if st.estop != "NONE":
                factors.append(f"estop_{st.estop.lower()}")
        if factors:
            status = ProfileStatus.UNAVAILABLE
        elif st is not None:
            if st.field_violation:
                factors.append("field_violation")
            if any(e.get("errorLevel") == "WARNING" for e in st.errors):
                factors.append("warnings_active")
            if st.position is not None and not st.position["localized"]:
                factors.append("not_localized")
            if factors:
                status = ProfileStatus.RESTRICTED
        limits: dict[str, float] = {}
        phys = (self.factsheet or {}).get("physicalParameters", {})
        if self.d.major == 3 and isinstance(phys.get("maximumSpeed"), int | float):
            limits["max_speed_mps"] = float(phys["maximumSpeed"])
        return self._caps.report(self.robot_id, self.require_clock(), (CapabilityProfile(
            "mobility", status, limits=limits, limiting_factors=tuple(factors),
            evidence=(f"vda5050:{self.d.version}",)),))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        src = f"{self.robot_id}/vda5050"
        out: dict[str, Observation] = {}
        if self.connection is not None and self.connection_at is not None:
            out["agv.connection"] = observation(clock, src, "agv.connection", self.connection,
                                                acquired_at=self.connection_at)
        st, at = self.state, self.state_at
        if st is None or at is None:
            return out

        def o(signal: str, value: Any, **kw: Any) -> Observation:
            return observation(clock, src, signal, value, acquired_at=at, **kw)

        out.update({
            "agv.last_node": o("agv.last_node", st.last_node_id),
            "agv.driving": o("agv.driving", st.driving),
            "agv.paused": o("agv.paused", st.paused),
            "agv.order": o("agv.order", {"order_id": st.order_id,
                                         "order_update_id": st.order_update_id,
                                         "open_nodes": list(st.node_states)}),
            "agv.operating_mode": o("agv.operating_mode", st.operating_mode),
            "agv.estop": o("agv.estop", st.estop),
            "agv.errors": o("agv.errors", [e.get("errorType") for e in st.errors]),
        })
        if st.position is not None:
            out["agv.position"] = o("agv.position", st.position, frame=st.position["map_id"]
                                    or self.map_id, spatial=True, unit="m")
        if st.battery_pct is not None:
            out["battery.charge"] = o("battery.charge", st.battery_pct, unit="%")
        return out

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.supported_skills() or cmd.skill_id not in SKILLS:
            raise UnsupportedOperation("vda5050 bridge does not support skill",
                                       skill_id=cmd.skill_id)
        if not self._online() or not self._fresh():
            raise ConnectionError("vehicle is not ONLINE with a fresh state")
        try:
            if cmd.skill_id == "drive_to_node":
                return await self._drive(ctx)
            kind = "startPause" if cmd.skill_id == "pause" else "stopPause"
            return await self._instant(kind, {})
        finally:
            await self._gate.settle(cmd.command_id, self.state_timeout)
            self._gate.clear(cmd.command_id)
            self._active = None
            self._interrupt.discard(cmd.command_id)

    async def _instant(self, kind: str, params: dict[str, Any]) -> AdapterReport:
        aid = new_id("act")
        self._publish("instantActions", m.instant_actions(
            self.headers, [m.action(kind, aid, "NONE", params or None, d=self.d)]))
        done = await wait_until(lambda: self.state is not None and
                                self.state.action_states.get(aid) in m.ACTION_TERMINAL,
                                self.state_timeout)
        if not done:
            raise ConnectionError(f"{kind} was sent but its state never became terminal")
        assert self.state is not None
        status = self.state.action_states[aid]
        if status == "FAILED":
            return AdapterReport(False, f"{kind} FAILED", {"action_id": aid},
                                 failure_reason=f"{kind}_failed")
        return AdapterReport(True, f"{kind} FINISHED", {"action_id": aid})

    async def _drive(self, ctx: ExecutionContext) -> AdapterReport:
        assert self.state is not None
        cmd = ctx.command
        target = str(cmd.args["node_id"])
        start = self.state
        pos = start.position or {"x": 0.0, "y": 0.0}
        order_id = new_id("order")
        nodes = [m.node(start.last_node_id or "start", 0, released=True, x=pos["x"], y=pos["y"],
                        map_id=self.map_id),
                 m.node(target, 2, released=True, x=float(cmd.args["x"]),
                        y=float(cmd.args["y"]), map_id=self.map_id)]
        edges = [m.edge(self.d, f"{nodes[0]['nodeId']}->{target}", 1, released=True,
                        start=nodes[0]["nodeId"], end=target)]
        self._publish("order", m.order(self.headers, order_id, 0, nodes, edges))
        self._active = (cmd.command_id, order_id)
        loop = asyncio.get_running_loop()
        end = loop.time() + self.order_timeout
        while True:
            st = self.state
            if cmd.command_id in self._interrupt:
                return AdapterReport(False, "order cancelled", {"order_id": order_id},
                                     failure_reason="interrupted")
            if not self._online() or not self._fresh():
                raise ConnectionError("vehicle connection lost while the order was released; "
                                      "it may complete the released base on its own")
            assert st is not None
            errs = m.errors_for(st, "orderId", order_id)
            if errs:
                return AdapterReport(False, f"vehicle reported {errs[0].get('errorType')}",
                                     {"order_id": order_id, "errors": errs},
                                     failure_reason=f"order_error:{errs[0].get('errorType')}")
            if (st.order_id == order_id and st.last_node_id == target and not st.node_states
                    and not st.driving):
                return AdapterReport(True, "order base completed", {"order_id": order_id})
            if loop.time() > end:
                raise ConnectionError("order did not complete before order_timeout_s")
            await asyncio.sleep(0.005)

    async def interrupt(self, command_id: str) -> InterruptReport:
        if self._active is None or self._active[0] != command_id:
            return InterruptReport(False, False, "command is not active")
        done = self._gate.begin(command_id)
        try:
            order_id = self._active[1]
            self._interrupt.add(command_id)
            aid = new_id("act")
            self._publish("instantActions", m.instant_actions(self.headers, [m.action(
                "cancelOrder", aid, "NONE", {"orderId": order_id}, d=self.d)]))
            ok = await wait_until(lambda: self.state is not None and
                                  self.state.action_states.get(aid) in m.ACTION_TERMINAL,
                                  self.state_timeout)
            status = self.state.action_states.get(aid) if self.state else None
            confirmed = ok and status == "FINISHED"
            return InterruptReport(True, confirmed, f"cancelOrder {status or 'not reported'}")
        finally:
            done.set()


class PahoLink:  # pragma: no cover - needs paho-mqtt and a broker
    """Connects a :class:`VdaAdapter` to a broker with paho-mqtt (BSD-3 option of its
    EPL-2.0 OR BSD-3-Clause dual license)."""

    def __init__(self, adapter: VdaAdapter, host: str, port: int = 1883) -> None:
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:
            raise UnsupportedOperation("paho-mqtt is not installed (extra [vda5050])",
                                       error=str(exc)) from exc
        self.adapter = adapter
        self.loop = asyncio.get_running_loop()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_message = lambda _c, _u, msg: self.loop.call_soon_threadsafe(
            adapter.on_message, msg.topic, msg.payload)
        self.client.connect(host, port)
        for topic in adapter.subscriptions():
            self.client.subscribe(topic, qos=0 if not topic.endswith("connection") else 1)
        self.client.loop_start()
        adapter.use_publisher(self)

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any:
        return self.client.publish(topic, payload, qos=qos, retain=retain)


__all__ = ["SKILLS", "MqttPublisher", "PahoLink", "VdaAdapter", "vda_skill_set"]
