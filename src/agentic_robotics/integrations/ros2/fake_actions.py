"""A fake ROS 2 action graph behind :class:`~.actions.ActionLayer` (ADR 0019).

Each server is a scripted behaviour: ``accept`` (bool), terminal ``status``, ``result``
fields, ``duration_s`` before the result, and whether cancel requests are honoured. A
MoveIt ``MoveGroup`` server answers plan-only goals with ``error_code.val``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .actions import MOVEIT_SUCCESS, CancelReturn, GoalStatus

__requirements__ = ("REQ03", "REQ18")


@dataclass
class ServerScript:
    accept: bool = True
    status: int = GoalStatus.SUCCEEDED
    result: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.02
    cancellable: bool = True
    plan_error_code: int = MOVEIT_SUCCESS     # for MoveGroup plan_only goals
    available: bool = True
    never_finish: bool = False


class FakeGoal:
    def __init__(self, server: ServerScript, goal: dict[str, Any],
                 feedback: Callable[[dict[str, Any]], None]) -> None:
        self.goal_id = uuid.uuid4().hex
        self.accepted = server.accept
        self.goal = goal
        self._server = server
        self._feedback = feedback
        self._cancel = asyncio.Event()
        self.status = GoalStatus.ACCEPTED if self.accepted else GoalStatus.UNKNOWN

    async def result(self) -> tuple[int, dict[str, Any]]:
        s = self._server
        plan_only = bool(self.goal.get("planning_options", {}).get("plan_only"))
        self.status = GoalStatus.EXECUTING
        self._feedback({"state": "PLANNING" if plan_only else "MONITOR"})
        if s.never_finish:
            await asyncio.Event().wait()
        try:
            await asyncio.wait_for(self._cancel.wait(), s.duration_s)
            self.status = GoalStatus.CANCELED
            return self.status, {}
        except TimeoutError:
            pass
        if plan_only:
            self.status = GoalStatus.SUCCEEDED
            return self.status, {"error_code": {"val": s.plan_error_code}, "planning_time": 0.01}
        self.status = s.status
        return self.status, dict(s.result)

    async def cancel(self) -> int:
        if self.status in (GoalStatus.SUCCEEDED, GoalStatus.ABORTED, GoalStatus.CANCELED):
            return CancelReturn.ERROR_GOAL_TERMINATED
        if not self._server.cancellable:
            return CancelReturn.ERROR_REJECTED
        self.status = GoalStatus.CANCELING
        self._cancel.set()
        return CancelReturn.ERROR_NONE


class FakeActionGraph:
    def __init__(self, servers: dict[str, ServerScript] | None = None) -> None:
        self.servers = dict(servers or {})
        self.goals: list[tuple[str, dict[str, Any]]] = []

    async def wait_for_server(self, action: str, action_type: str, timeout_s: float) -> bool:
        s = self.servers.get(action)
        return bool(s and s.available)

    async def send_goal(self, action: str, action_type: str, goal: dict[str, Any],
                        feedback: Callable[[dict[str, Any]], None]) -> FakeGoal:
        self.goals.append((action, goal))
        return FakeGoal(self.servers[action], goal, feedback)

    def close(self) -> None:
        pass


__all__ = ["FakeActionGraph", "FakeGoal", "ServerScript"]
