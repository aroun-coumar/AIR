"""ROS 2 action bridge against a fake action graph (no rclpy)."""

from __future__ import annotations

import asyncio
import sys

import pytest

from agentic_robotics import CommandState
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import ParamSpec, ProfileStatus
from agentic_robotics.integrations.ros2 import (
    FakeActionGraph,
    GoalStatus,
    Ros2ActionAdapter,
    ServerScript,
    action_skill,
    fill,
)

from .bridge_env import make_env, manifest

pytestmark = pytest.mark.req("REQ03", "REQ14", "REQ17")

NAV = {"action": "/navigate_to_pose", "type": "nav2_msgs/action/NavigateToPose",
       "goal": {"pose": {"header": {"frame_id": "map"},
                         "pose": {"position": {"x": "$args.x", "y": "$args.y"}}}}}
ARM = {"action": "/move_action", "type": "moveit_msgs/action/MoveGroup",
       "goal": {"request": {"group_name": "$args.group"}}, "preflight": "moveit_plan_only"}
SKILLS = (
    action_skill("navigate_to_pose", "Nav2 NavigateToPose",
                 (ParamSpec("x", "number", unit="m"), ParamSpec("y", "number", unit="m"))),
    action_skill("move_group", "MoveIt MoveGroup", (ParamSpec("group", "string"),)),
)


async def _env(servers: dict[str, ServerScript]):  # type: ignore[no-untyped-def]
    m = manifest("ros2_action", "ros-1", ("ros2_actions",),
                 actions={"navigate_to_pose": NAV, "move_group": ARM})
    graph = FakeActionGraph(servers)
    adapter = Ros2ActionAdapter(m, m.adapter.config)
    adapter.use_actions(graph)
    env = await make_env(adapter, SKILLS)
    return env, adapter, graph


def test_rclpy_is_not_imported_and_templates_are_data() -> None:
    assert "rclpy" not in sys.modules
    assert fill({"a": ["$args.x", 1], "b": "$argsx"}, {"x": 2}) == {"a": [2, 1], "b": "$argsx"}


async def test_conformance() -> None:
    env, _, _ = await _env({"/navigate_to_pose": ServerScript(), "/move_action": ServerScript()})
    try:
        report = await check_adapter(env.runtime.robot("ros-1"), env.runtime.skills,
                                     env.runtime.clock)
        assert report.passed, report.summary()
    finally:
        await env.runtime.stop()


async def test_verified_only_from_the_result() -> None:
    env, adapter, graph = await _env({"/navigate_to_pose": ServerScript(),
                                      "/move_action": ServerScript()})
    try:
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.VERIFIED
        sent = graph.goals[-1][1]
        assert sent["pose"]["pose"]["position"] == {"x": 1.0, "y": 2.0}
        assert adapter.observe()["action.navigate_to_pose.result"].value["status"] == "succeeded"
    finally:
        await env.runtime.stop()


async def test_rejected_goal_fails_before_motion_and_aborted_fails() -> None:
    env, _, _ = await _env({"/navigate_to_pose": ServerScript(accept=False),
                            "/move_action": ServerScript()})
    try:
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.FAILED and r.reason == "goal_rejected"
    finally:
        await env.runtime.stop()
    env, _, _ = await _env({"/navigate_to_pose": ServerScript(status=GoalStatus.ABORTED),
                            "/move_action": ServerScript()})
    try:
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.FAILED and r.reason == "aborted"
    finally:
        await env.runtime.stop()


async def test_canceled_by_server_is_never_success() -> None:
    env, _, _ = await _env({"/navigate_to_pose": ServerScript(status=GoalStatus.CANCELED),
                            "/move_action": ServerScript()})
    try:
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.FAILED and r.reason == "canceled"
    finally:
        await env.runtime.stop()


async def test_missing_server_restricts_capability_and_fails_cleanly() -> None:
    env, adapter, _ = await _env({"/navigate_to_pose": ServerScript(available=False),
                                  "/move_action": ServerScript()})
    try:
        prof = adapter.capability_report().get("ros2_actions")
        assert prof.status is ProfileStatus.RESTRICTED
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.FAILED and r.reason == "action_server_unavailable"
    finally:
        await env.runtime.stop()


async def test_moveit_plan_only_preflight_gates_execution() -> None:
    env, _, graph = await _env({"/navigate_to_pose": ServerScript(),
                                "/move_action": ServerScript(plan_error_code=-1)})
    try:
        r = await env.run("move_group", group="arm")
        assert r.state is CommandState.FAILED and r.reason == "preflight_plan_failed"
        assert [g[1]["planning_options"]["plan_only"] for g in graph.goals] == [True]
    finally:
        await env.runtime.stop()
    env, _, graph = await _env({"/navigate_to_pose": ServerScript(),
                                "/move_action": ServerScript()})
    try:
        r = await env.run("move_group", group="arm")
        assert r.state is CommandState.VERIFIED
        assert [g[1]["planning_options"]["plan_only"] for g in graph.goals] == [True, False]
        assert r.observed_effects["preflight"]["ok"] is True
    finally:
        await env.runtime.stop()


async def test_cancel_confirmed_by_canceled_status_and_lost_result_is_unresolved() -> None:
    env, _, _ = await _env({"/navigate_to_pose": ServerScript(duration_s=5.0),
                            "/move_action": ServerScript()})
    try:
        h = env.runtime.submit(env.command("navigate_to_pose", x=1.0, y=2.0))
        await asyncio.sleep(0.05)
        r = await h.cancel()
        assert r.state is CommandState.CANCELLED
        assert r.observed_effects["stopped_confirmed"] is True
    finally:
        await env.runtime.stop()
    m = manifest("ros2_action", "ros-1", ("ros2_actions",), actions={"navigate_to_pose": NAV})
    graph = FakeActionGraph({"/navigate_to_pose": ServerScript(never_finish=True)})
    adapter = Ros2ActionAdapter(m, m.adapter.config)
    adapter.use_actions(graph)
    short = action_skill("navigate_to_pose", "nav", (ParamSpec("x", "number"),
                                                    ParamSpec("y", "number")), deadline_s=0.2)
    env = await make_env(adapter, (short,))
    try:
        r = await env.run("navigate_to_pose", x=1.0, y=2.0)
        assert r.state is CommandState.UNRESOLVED and r.reason == "deadline_exceeded"
    finally:
        await env.runtime.stop()


def test_mapping_requires_action_and_type() -> None:
    m = manifest("ros2_action", "ros-1", (), actions={"bad": {"action": "/x"}})
    with pytest.raises(ValueError):
        Ros2ActionAdapter(m, m.adapter.config)
