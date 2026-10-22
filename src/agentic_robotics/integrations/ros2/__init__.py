"""ROS 2 action bridge (W6.2). Optional; rclpy is imported lazily by
:class:`RclpyActionLayer` only. Register with :func:`register`."""

from ...plugins import register_adapter
from .actions import MOVEIT_SUCCESS, ActionLayer, CancelReturn, GoalStatus, RclpyActionLayer
from .adapter import Ros2ActionAdapter, action_skill, fill
from .fake_actions import FakeActionGraph, ServerScript

__requirements__ = ("REQ03",)


def register() -> None:
    register_adapter(Ros2ActionAdapter.kind, Ros2ActionAdapter)


__all__ = ["MOVEIT_SUCCESS", "ActionLayer", "CancelReturn", "FakeActionGraph", "GoalStatus",
           "RclpyActionLayer", "Ros2ActionAdapter", "ServerScript", "action_skill", "fill",
           "register"]
