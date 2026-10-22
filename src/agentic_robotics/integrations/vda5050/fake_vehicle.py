"""A fake VDA 5050 vehicle on the other side of an injected MQTT publisher (ADR 0019).

It consumes ``order`` and ``instantActions`` payloads published by the bridge and feeds
``state``/``connection``/``factsheet`` messages back, shaped per the v2 (2.1.0) or v3
(3.0.0) JSON schemas. Faults: ``reject_next_order(error_type)``, ``break_connection()``,
``set_operating_mode(mode)``, ``estop()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Callable
from typing import Any

from . import messages as m

__requirements__ = ("REQ03", "REQ18")

Feed = Callable[[str, bytes], None]


class FakeVehicle:
    def __init__(self, d: m.Dialect, manufacturer: str, serial: str, *, speed: float = 2.0,
                 dt_s: float = 0.02, tick_s: float = 0.002, cancel_ticks: int = 3) -> None:
        self.d = d
        self.headers = m.Headers(d, manufacturer, serial)
        self.speed = speed
        self.dt = dt_s
        self.tick_s = tick_s
        self.cancel_ticks = cancel_ticks
        self.x = self.y = 0.0
        self.last_node, self.last_seq = "home", 0
        self.order_id, self.update_id = "", 0
        self.target: dict[str, Any] | None = None
        self.node_states: list[dict[str, Any]] = []
        self.edge_states: list[dict[str, Any]] = []
        self.actions: dict[str, dict[str, Any]] = {}     # actionId -> actionState
        self.pending_cancel: tuple[str, int] | None = None
        self.paused = False
        self.mode = "AUTOMATIC"
        self.estop_state = "NONE"
        self.errors: list[dict[str, Any]] = []
        self.received: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.online = True
        self._reject: str | None = None
        self._feed: Feed | None = None
        self._task: asyncio.Task[None] | None = None

    # -- wiring ----------------------------------------------------------------------
    def attach(self, feed: Feed) -> None:
        self._feed = feed
        self._emit("connection", {**self.headers.header("connection"),
                                  "connectionState": "ONLINE"})
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _emit(self, topic: str, msg: dict[str, Any]) -> None:
        self.sent.append((topic, msg))
        if self._feed is not None:
            self._feed(self.headers.topic(topic), json.dumps(msg).encode("utf-8"))

    # -- faults ----------------------------------------------------------------------
    def reject_next_order(self, error_type: str = "orderUpdateError") -> None:
        self._reject = error_type

    def break_connection(self) -> None:
        self.online = False
        self._emit("connection", {**self.headers.header("connection"),
                                  "connectionState": self.d.broken})

    def set_operating_mode(self, mode: str) -> None:
        self.mode = mode

    def estop(self, kind: str = "MANUAL") -> None:
        self.estop_state = kind

    # -- MqttPublisher ---------------------------------------------------------------
    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        if not self.online:
            return
        msg = json.loads(payload)
        name = topic.rsplit("/", 1)[-1]
        self.received.append((name, msg))
        if name == "order":
            self._order(msg)
        elif name == "instantActions":
            for a in msg["actions"]:
                self._instant(a)

    def _order(self, msg: dict[str, Any]) -> None:
        if self._reject:
            self.errors.append({"errorType": self._reject, "errorLevel": "WARNING",
                                "errorReferences": [{"referenceKey": "orderId",
                                                     "referenceValue": msg["orderId"]}]})
            self._reject = None
            return
        nodes = sorted(msg["nodes"], key=lambda n: n["sequenceId"])
        self.order_id, self.update_id = msg["orderId"], msg["orderUpdateId"]
        self.target = nodes[-1]
        self.node_states = [{"nodeId": n["nodeId"], "sequenceId": n["sequenceId"],
                             "released": n["released"]} for n in nodes[1:]]
        self.edge_states = [{"edgeId": e["edgeId"], "sequenceId": e["sequenceId"],
                             "released": e["released"]} for e in msg["edges"]]
        self.last_node, self.last_seq = nodes[0]["nodeId"], nodes[0]["sequenceId"]

    def _instant(self, a: dict[str, Any]) -> None:
        kind, aid = a["actionType"], a["actionId"]
        state = {"actionId": aid, "actionType": kind, "actionStatus": "FINISHED"}
        if kind == "cancelOrder":
            if not self.order_id or self.target is None:
                state["actionStatus"] = "FAILED"
            else:
                state["actionStatus"] = "RUNNING"
                self.pending_cancel = (aid, self.cancel_ticks)
        elif kind == "startPause":
            self.paused = True
        elif kind == "stopPause":
            self.paused = False
        self.actions[aid] = state

    # -- simulation ------------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            self.step()

    def step(self) -> None:
        if not self.online:
            return
        if self.pending_cancel is not None:
            aid, left = self.pending_cancel
            if left <= 0:
                self.actions[aid]["actionStatus"] = "FINISHED"
                self.pending_cancel = None
                self.target, self.node_states, self.edge_states = None, [], []
            else:
                self.pending_cancel = (aid, left - 1)
        driving = False
        movable = not self.paused and self.mode == "AUTOMATIC" and self.estop_state == "NONE"
        if self.target is not None and movable and self.pending_cancel is None:
            tx, ty = self.target["nodePosition"]["x"], self.target["nodePosition"]["y"]
            dist = math.hypot(tx - self.x, ty - self.y)
            step = self.speed * self.dt
            if dist <= step:
                self.x, self.y = tx, ty
                self.last_node, self.last_seq = self.target["nodeId"], self.target["sequenceId"]
                self.node_states, self.edge_states, self.target = [], [], None
            else:
                self.x += (tx - self.x) / dist * step
                self.y += (ty - self.y) / dist * step
                driving = True
        self._emit("state", self.state(driving))

    def state(self, driving: bool = False) -> dict[str, Any]:
        base: dict[str, Any] = {
            **self.headers.header("state"), "orderId": self.order_id,
            "orderUpdateId": self.update_id, "lastNodeId": self.last_node,
            "lastNodeSequenceId": self.last_seq, "nodeStates": list(self.node_states),
            "edgeStates": list(self.edge_states), "driving": driving, "paused": self.paused,
            "operatingMode": self.mode, "errors": list(self.errors),
        }
        actions = list(self.actions.values())
        if self.d.major == 2:
            base.update({"actionStates": actions,
                         "batteryState": {"batteryCharge": 80.0, "charging": False},
                         "agvPosition": {"x": self.x, "y": self.y, "theta": 0.0, "mapId": "floor1",
                                         "positionInitialized": True},
                         "safetyState": {"eStop": self.estop_state, "fieldViolation": False}})
        else:
            base.update({"actionStates": [], "instantActionStates": actions,
                         "powerSupply": {"stateOfCharge": 80.0, "charging": False},
                         "mobileRobotPosition": {"x": self.x, "y": self.y, "theta": 0.0,
                                                 "mapId": "floor1", "localized": True},
                         "safetyState": {"activeEmergencyStop": self.estop_state,
                                         "fieldViolation": False}})
        return base


__all__ = ["FakeVehicle"]
