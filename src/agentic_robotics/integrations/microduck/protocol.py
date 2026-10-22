"""The robotd wire protocol, as verified from ``pollen-robotics/microduck``
``duck-ipc-proto/src/lib.rs`` (HEAD 942e58e, API_VERSION 44; see ADR 0019).

JSON-RPC 2.0, one object per line (NDJSON), over a unix socket (``/run/robotd.sock``).
Continuous intents (``robot.move``, ``robot.head``, ``robot.pose``) are *notifications*
(no ``id``, no reply, last writer wins, expiring after robotd's deadman, default 500 ms).
Discrete intents (``robot.stop``, ``robot.enable``, ``robot.init``, ``robot.do``...) are
requests answered with ``{"accepted": bool, "reason"?: str}``; ``accepted: false`` is a
normal outcome, not an error. Methods without parameters send ``{}``. Server pushes
``robot.state`` notifications after ``robot.subscribe``.

Only the subset AIR uses is modelled. Everything here is data; nothing grants authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...core.errors import FrameworkError

__requirements__ = ("REQ03", "REQ05")

JSONRPC_VERSION = "2.0"
#: ``duck_ipc_proto::API_VERSION`` at the verified revision. robotd *reports* a
#: difference and refuses only the calls whose shape moved, so AIR pins and records it.
API_VERSION = 44
ROBOT_SOCKET = "/run/robotd.sock"
#: ``pad_link::DEADMAN_MS``: where robotd zeroes the velocity without a fresh intent.
DEADMAN_S = 0.5
ROBOT_MODEL = "microduck"
JOINT_NAMES = (
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee", "left_ankle",
    "neck_pitch", "head_pitch", "head_yaw", "head_roll", "mouth",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch", "right_knee", "right_ankle",
)


class Method:
    HELLO = "hello"
    HEALTH = "robot.health"
    SAFE_TO_RESTART = "robot.safeToRestart"
    MOVE = "robot.move"            # continuous: notification
    HEAD = "robot.head"            # continuous: notification
    STOP = "robot.stop"            # discrete: request; zero velocity, not "go limp"
    ENABLE = "robot.enable"        # discrete: policy on/off
    INIT = "robot.init"            # power the joints, ramp to home pose
    RELAX = "robot.relax"          # torque off: the robot collapses
    DO = "robot.do"                # one-shot skill by name
    POLICIES = "robot.policies"
    SUBSCRIBE = "robot.subscribe"
    STATE = "robot.state"          # server -> client notification

    CONTINUOUS = frozenset({MOVE, HEAD, "robot.pose", "robot.mouth"})


class Code:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    BUSY = 1
    PERMISSION_DENIED = 14


class ProtocolError(FrameworkError):
    code = "microduck_protocol"


class RpcError(FrameworkError):
    """A JSON-RPC error object answered by robotd."""

    code = "microduck_rpc_error"

    def __init__(self, rpc_code: int, message: str, data: Any = None) -> None:
        super().__init__(message, rpc_code=rpc_code, data=data)
        self.rpc_code = rpc_code


def request(id_: int, method: str, params: dict[str, Any] | None = None) -> str:
    if method in Method.CONTINUOUS:
        raise ProtocolError("continuous intents are notifications, never requests",
                            method=method)
    return _line({"jsonrpc": JSONRPC_VERSION, "id": id_, "method": method,
                  "params": params or {}})


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    return _line({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}})


def _line(obj: dict[str, Any]) -> str:
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    return text + "\n"


@dataclass(frozen=True)
class Message:
    """One decoded line: a response (``id`` with ``result``/``error``) or a notification."""

    id: int | str | None
    method: str | None
    params: Any
    result: Any
    error: dict[str, Any] | None

    @property
    def is_notification(self) -> bool:
        return self.id is None and self.method is not None


def decode(line: str) -> Message:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError("malformed NDJSON line", error=str(exc)) from exc
    if not isinstance(obj, dict) or obj.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError("not a JSON-RPC 2.0 message")
    err = obj.get("error")
    if err is not None and not (isinstance(err, dict) and isinstance(err.get("code"), int)):
        raise ProtocolError("malformed error object")
    return Message(id=obj.get("id"), method=obj.get("method"), params=obj.get("params"),
                   result=obj.get("result"), error=err)


__all__ = ["API_VERSION", "DEADMAN_S", "JOINT_NAMES", "ROBOT_SOCKET", "Code", "Message",
           "Method", "ProtocolError", "RpcError", "decode", "notification", "request"]
