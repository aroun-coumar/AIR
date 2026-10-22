"""VDA 5050 message builders and readers for v2 (2.1.0) and v3 (3.0.0).

Verified against ``VDA5050/VDA5050`` ``json_schemas/*.schema`` at tag ``2.1.0`` and at
HEAD 0b2ae43 (3.0.0), and ``VDA5050_EN.md`` (repo MIT; the specification text is
VDA-owned). Differences AIR handles:

=====================  ===========================  =============================
                       v2 (2.1.0)                   v3 (3.0.0)
=====================  ===========================  =============================
topic prefix           ``uagv/v2/...``               ``vda5050/v3/...``
edge                   needs startNodeId/endNodeId   no node ids on edges
blockingType           NONE, SOFT, HARD              NONE, SOFT, SINGLE, HARD
broken connection      ``CONNECTIONBROKEN``          ``CONNECTION_BROKEN``
battery                ``batteryState.batteryCharge`` ``powerSupply.stateOfCharge``
position               ``agvPosition.positionInitialized``  ``mobileRobotPosition.localized``
e-stop                 ``safetyState.eStop``         ``safetyState.activeEmergencyStop``
=====================  ===========================  =============================

Topic levels: ``interfaceName/majorVersion/manufacturer/serialNumber/topic``. Each topic
has its own ``headerId`` counter (+1 per message sent).
"""

from __future__ import annotations

import datetime as _dt
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import SchemaError

__requirements__ = ("REQ06", "REQ09")

TOPICS = ("order", "instantActions", "state", "visualization", "connection", "factsheet")
REQUIRED_INSTANT = ("cancelOrder", "startPause", "stopPause")   # every robot supports these
ACTION_TERMINAL = ("FINISHED", "FAILED")


@dataclass(frozen=True)
class Dialect:
    version: str                       # full protocol version string, e.g. "3.0.0"
    interface_name: str

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def broken(self) -> str:
        return "CONNECTIONBROKEN" if self.major == 2 else "CONNECTION_BROKEN"

    @property
    def blocking_types(self) -> tuple[str, ...]:
        return ("NONE", "SOFT", "HARD") if self.major == 2 else ("NONE", "SOFT", "SINGLE",
                                                                  "HARD")


def dialect(version: str, interface_name: str | None = None) -> Dialect:
    major = version.split(".")[0]
    if major not in ("2", "3"):
        raise SchemaError("unsupported VDA 5050 major version", version=version)
    return Dialect(version, interface_name or ("uagv" if major == "2" else "vda5050"))


def timestamp(now: _dt.datetime | None = None) -> str:
    t = (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@dataclass
class Headers:
    """Per-topic ``headerId`` counters for one robot."""

    d: Dialect
    manufacturer: str
    serial_number: str
    clock: Callable[[], str] = timestamp
    _ids: dict[str, int] = field(default_factory=dict)

    def topic(self, name: str) -> str:
        if name not in TOPICS:
            raise SchemaError("unknown VDA 5050 topic", topic=name)
        for part in (self.manufacturer, self.serial_number):
            if any(ch in part for ch in "/+#$"):
                raise SchemaError("topic level contains a reserved character", value=part)
        return f"{self.d.interface_name}/v{self.d.major}/{self.manufacturer}/" \
               f"{self.serial_number}/{name}"

    def header(self, name: str) -> dict[str, Any]:
        self._ids[name] = self._ids.get(name, -1) + 1
        return {"headerId": self._ids[name], "timestamp": self.clock(),
                "version": self.d.version, "manufacturer": self.manufacturer,
                "serialNumber": self.serial_number}


def action(action_type: str, action_id: str, blocking: str = "HARD",
           parameters: dict[str, Any] | None = None, d: Dialect | None = None) -> dict[str, Any]:
    if d is not None and blocking not in d.blocking_types:
        raise SchemaError("blockingType not available in this protocol version",
                          blocking=blocking, version=d.version)
    out: dict[str, Any] = {"actionType": action_type, "actionId": action_id,
                           "blockingType": blocking}
    if parameters:
        out["actionParameters"] = [{"key": k, "value": v} for k, v in parameters.items()]
    return out


def node(node_id: str, sequence_id: int, *, released: bool, x: float, y: float, map_id: str,
         theta: float | None = None, actions: list[dict[str, Any]] | None = None
         ) -> dict[str, Any]:
    pos: dict[str, Any] = {"x": x, "y": y, "mapId": map_id}
    if theta is not None:
        pos["theta"] = theta
    return {"nodeId": node_id, "sequenceId": sequence_id, "released": released,
            "nodePosition": pos, "actions": actions or []}


def edge(d: Dialect, edge_id: str, sequence_id: int, *, released: bool, start: str, end: str,
         max_speed: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"edgeId": edge_id, "sequenceId": sequence_id, "released": released,
                           "actions": []}
    if d.major == 2:
        out["startNodeId"], out["endNodeId"] = start, end
    if max_speed is not None:
        out["maximumSpeed" if d.major == 3 else "maxSpeed"] = max_speed
    return out


def order(h: Headers, order_id: str, update_id: int, nodes: list[dict[str, Any]],
          edges: list[dict[str, Any]]) -> dict[str, Any]:
    seqs = [n["sequenceId"] for n in nodes] + [e["sequenceId"] for e in edges]
    if len(set(seqs)) != len(seqs):
        raise SchemaError("node and edge sequenceIds must be unique within an order")
    released = [n["released"] for n in sorted(nodes, key=lambda n: n["sequenceId"])]
    if released and not released[0]:
        raise SchemaError("the first node of an order must be released (base)")
    if any(not a and b for a, b in itertools.pairwise(released)):
        raise SchemaError("released base must be a prefix; horizon follows it")
    return {**h.header("order"), "orderId": order_id, "orderUpdateId": update_id,
            "nodes": nodes, "edges": edges}


def instant_actions(h: Headers, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """v3: an instant action's blockingType is always ``NONE``."""
    if h.d.major == 3 and any(a.get("blockingType") != "NONE" for a in actions):
        raise SchemaError("VDA 5050 v3 instant actions must use blockingType NONE")
    return {**h.header("instantActions"), "actions": actions}


@dataclass(frozen=True)
class RobotState:
    """The fields of a ``state`` message AIR reads, normalized across v2/v3."""

    order_id: str
    order_update_id: int
    last_node_id: str
    last_node_sequence_id: int
    node_states: tuple[str, ...]
    driving: bool
    paused: bool
    operating_mode: str
    action_states: dict[str, str]
    errors: tuple[dict[str, Any], ...]
    battery_pct: float | None
    position: dict[str, Any] | None
    estop: str
    field_violation: bool


def read_state(d: Dialect, msg: dict[str, Any]) -> RobotState:
    for key in ("orderId", "lastNodeId", "nodeStates", "actionStates", "operatingMode",
                "safetyState", "driving"):
        if key not in msg:
            raise SchemaError("state message is missing a required field", field=key)
    safety = msg["safetyState"]
    if d.major == 2:
        battery = (msg.get("batteryState") or {}).get("batteryCharge")
        pos = msg.get("agvPosition")
        localized = bool(pos and pos.get("positionInitialized"))
        estop = str(safety.get("eStop", "NONE"))
    else:
        battery = (msg.get("powerSupply") or {}).get("stateOfCharge")
        pos = msg.get("mobileRobotPosition")
        localized = bool(pos and pos.get("localized"))
        estop = str(safety.get("activeEmergencyStop", "NONE"))
    position = None if pos is None else {"x": float(pos["x"]), "y": float(pos["y"]),
                                         "theta": float(pos.get("theta", 0.0)),
                                         "map_id": str(pos.get("mapId", "")),
                                         "localized": localized}
    states = {a["actionId"]: a["actionStatus"] for a in msg.get("instantActionStates", [])}
    states.update({a["actionId"]: a["actionStatus"] for a in msg["actionStates"]})
    return RobotState(
        order_id=str(msg["orderId"]), order_update_id=int(msg.get("orderUpdateId", 0)),
        last_node_id=str(msg["lastNodeId"]),
        last_node_sequence_id=int(msg.get("lastNodeSequenceId", 0)),
        node_states=tuple(n["nodeId"] for n in msg["nodeStates"]), driving=bool(msg["driving"]),
        paused=bool(msg.get("paused", False)), operating_mode=str(msg["operatingMode"]),
        action_states=states, errors=tuple(msg.get("errors", [])),
        battery_pct=None if battery is None else float(battery), position=position,
        estop=estop, field_violation=bool(safety.get("fieldViolation", False)))


def errors_for(state: RobotState, key: str, value: str) -> list[dict[str, Any]]:
    return [e for e in state.errors
            if any(r.get("referenceKey") == key and r.get("referenceValue") == value
                   for r in e.get("errorReferences", []))]


__all__ = ["ACTION_TERMINAL", "REQUIRED_INSTANT", "TOPICS", "Dialect", "Headers", "RobotState",
           "action", "dialect", "edge", "errors_for", "instant_actions", "node", "order",
           "read_state", "timestamp"]
