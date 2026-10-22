"""The injectable ROS 2 action layer (ADR 0019).

Verified against ``ros2/rclpy`` ``rclpy/action/client.py`` (HEAD 6b5b070: ``ActionClient``
``wait_for_server``, ``send_goal_async``, ``ClientGoalHandle.accepted``,
``get_result_async``, ``cancel_goal_async``), ``ros2/rcl_interfaces``
``action_msgs/msg/GoalStatus.msg`` and ``srv/CancelGoal.srv``, and ``moveit/moveit_msgs``
``action/MoveGroup.action``, ``msg/PlanningOptions.msg`` (``plan_only``) and
``msg/MoveItErrorCodes.msg`` (``SUCCESS=1``). Goals and results cross this seam as dicts
(``rosidl_runtime_py`` field names); rclpy is imported only by :class:`RclpyActionLayer`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, Protocol

from ...core.errors import UnsupportedOperation

__requirements__ = ("REQ03",)


class GoalStatus:
    UNKNOWN = 0
    ACCEPTED = 1
    EXECUTING = 2
    CANCELING = 3
    SUCCEEDED = 4
    CANCELED = 5
    ABORTED = 6

    NAMES = {0: "unknown", 1: "accepted", 2: "executing", 3: "canceling", 4: "succeeded",
             5: "canceled", 6: "aborted"}


class CancelReturn:
    ERROR_NONE = 0
    ERROR_REJECTED = 1
    ERROR_UNKNOWN_GOAL_ID = 2
    ERROR_GOAL_TERMINATED = 3


MOVEIT_SUCCESS = 1


class GoalHandle(Protocol):
    goal_id: str
    accepted: bool

    async def result(self) -> tuple[int, dict[str, Any]]:
        """``(GoalStatus, result fields)`` once the goal is terminal."""
        ...

    async def cancel(self) -> int:
        """Request cancellation; returns ``CancelGoal`` ``return_code``."""
        ...


class ActionLayer(Protocol):
    async def wait_for_server(self, action: str, action_type: str, timeout_s: float) -> bool: ...

    async def send_goal(self, action: str, action_type: str, goal: dict[str, Any],
                        feedback: Callable[[dict[str, Any]], None]) -> GoalHandle: ...

    def close(self) -> None: ...


class RclpyActionLayer:  # pragma: no cover - needs a sourced ROS 2 environment
    """Production binding. ``rclpy`` is not pip-installable; source a ROS 2 distro."""

    def __init__(self, node_name: str = "air_action_bridge") -> None:
        try:
            import rclpy  # type: ignore[import-not-found,import-untyped,unused-ignore]
            from rclpy.executors import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
                MultiThreadedExecutor,
            )
        except ImportError as exc:
            raise UnsupportedOperation("rclpy is not available; source a ROS 2 setup file",
                                       error=str(exc)) from exc
        if not rclpy.ok():
            rclpy.init()
        self._rclpy = rclpy
        self._node = rclpy.create_node(node_name)
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)
        self._thread.start()
        self._clients: dict[tuple[str, str], Any] = {}

    def _client(self, action: str, action_type: str) -> Any:
        from rclpy.action import ActionClient  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from rosidl_runtime_py.utilities import get_action  # type: ignore[import-not-found,import-untyped,unused-ignore]

        key = (action, action_type)
        if key not in self._clients:
            self._clients[key] = (ActionClient(self._node, get_action(action_type), action),
                                  get_action(action_type))
        return self._clients[key]

    async def wait_for_server(self, action: str, action_type: str, timeout_s: float) -> bool:
        client, _ = self._client(action, action_type)
        return bool(await asyncio.to_thread(client.wait_for_server, timeout_sec=timeout_s))

    async def send_goal(self, action: str, action_type: str, goal: dict[str, Any],
                        feedback: Callable[[dict[str, Any]], None]) -> GoalHandle:
        from rosidl_runtime_py.convert import message_to_ordereddict  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from rosidl_runtime_py.set_message import set_message_fields  # type: ignore[import-not-found,import-untyped,unused-ignore]

        client, kind = self._client(action, action_type)
        msg = kind.Goal()
        set_message_fields(msg, goal)
        loop = asyncio.get_running_loop()

        def on_feedback(fb: Any) -> None:
            loop.call_soon_threadsafe(feedback, dict(message_to_ordereddict(fb.feedback)))

        handle = await _await(loop, client.send_goal_async(msg, feedback_callback=on_feedback))
        return _RclpyGoal(loop, handle, message_to_ordereddict)

    def close(self) -> None:
        self._executor.shutdown()
        self._node.destroy_node()


class _RclpyGoal:  # pragma: no cover
    def __init__(self, loop: asyncio.AbstractEventLoop, handle: Any, to_dict: Any) -> None:
        self._loop = loop
        self._handle = handle
        self._to_dict = to_dict
        self.accepted = bool(handle.accepted)
        self.goal_id = bytes(handle.goal_id.uuid).hex()

    async def result(self) -> tuple[int, dict[str, Any]]:
        res = await _await(self._loop, self._handle.get_result_async())
        return int(res.status), dict(self._to_dict(res.result))

    async def cancel(self) -> int:
        res = await _await(self._loop, self._handle.cancel_goal_async())
        return int(res.return_code)


async def _await(loop: asyncio.AbstractEventLoop, rcl_future: Any) -> Any:  # pragma: no cover
    fut: asyncio.Future[Any] = loop.create_future()

    def resolve(value: Any) -> None:
        if not fut.done():
            fut.set_result(value)

    def done(f: Any) -> None:
        loop.call_soon_threadsafe(resolve, f.result())

    rcl_future.add_done_callback(done)
    return await fut


__all__ = ["MOVEIT_SUCCESS", "ActionLayer", "CancelReturn", "GoalHandle", "GoalStatus",
           "RclpyActionLayer"]
