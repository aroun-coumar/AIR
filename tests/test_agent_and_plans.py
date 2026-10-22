from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from agentic_robotics import AgentBudget, PlanExecutor, ScriptedModel, TaskAgent
from agentic_robotics.contracts import ReservationState
from agentic_robotics.core import PlanError
from agentic_robotics.runtime import PlanState

from .conftest import AGENT, Env

MakeEnv = Callable[..., Env]


def plan_json(*steps: dict) -> str:
    return json.dumps({"rationale": "test", "steps": list(steps)})


DOCK = {"skill_id": "dock_at", "robot_id": "carrier-1", "args": {"station": "station_a"}}
SNAP = {"skill_id": "capture_image", "robot_id": "carrier-1", "args": {}}


def agent(env: Env, *responses: str, **kw: object) -> TaskAgent:
    return TaskAgent(ScriptedModel(list(responses)), env.runtime.skills, env.runtime.clock,
                     **kw)  # type: ignore[arg-type]


async def test_agent_produces_typed_sequential_plan(make_env: MakeEnv) -> None:
    env = make_env()
    a = agent(env, "```json\n" + plan_json(DOCK, SNAP) + "\n```")
    plan = await a.plan("inspect", [env.robot])
    assert [s.skill_id for s in plan.steps] == ["dock_at", "capture_image"]
    assert plan.steps[1].depends_on == ("t1",)
    assert plan.produced_by == "agent:task/scripted"


@pytest.mark.parametrize(("step", "message"), [
    ({"skill_id": "fly", "robot_id": "carrier-1", "args": {}}, "unknown skill"),
    ({"skill_id": "dock_at", "robot_id": "carrier-9", "args": {}}, "unknown robot"),
    ({"skill_id": "navigate_to", "robot_id": "carrier-1", "args": {"x": 1}}, "y: required"),
])
async def test_agent_rejects_invalid_plans(make_env: MakeEnv, step: dict, message: str) -> None:
    env = make_env()
    with pytest.raises(PlanError) as exc:
        await agent(env, plan_json(step)).plan("g", [env.robot])
    assert any(message in p for p in exc.value.details["problems"])


async def test_agent_rejects_skill_robot_does_not_support(make_env: MakeEnv) -> None:
    env = make_env(camera=False)
    with pytest.raises(PlanError) as exc:
        await agent(env, plan_json(SNAP)).plan("g", [env.robot])
    assert "does not support" in exc.value.details["problems"][0]


async def test_agent_rejects_non_json(make_env: MakeEnv) -> None:
    env = make_env()
    with pytest.raises(PlanError, match="not valid JSON"):
        await agent(env, "sure! I'll dock first").plan("g", [env.robot])


async def test_tools_offered_to_model_are_limited_to_supported_skills(make_env: MakeEnv) -> None:
    env = make_env(camera=False)
    model = ScriptedModel([plan_json(DOCK)])
    await TaskAgent(model, env.runtime.skills, env.runtime.clock).plan("g", [env.robot])
    offered = {t["skill_id"] for t in model.requests[0].tools}
    assert offered == {"navigate_to", "dock_at"}


async def test_plan_reuse_avoids_repeat_model_calls_until_capability_changes(
        make_env: MakeEnv) -> None:
    env = make_env()
    a = agent(env, plan_json(DOCK), plan_json(DOCK))
    p1 = await a.plan("dock", [env.robot])
    p2 = await a.plan("dock", [env.robot])
    assert a.stats.model_calls == 1 and a.stats.cache_hits == 1
    assert p1.plan_id != p2.plan_id and p1.steps == p2.steps
    env.adapter.battery = 15.0  # capability revision changes -> fresh decision
    await a.plan("dock", [env.robot])
    assert a.stats.model_calls == 2


async def test_model_call_budget_is_enforced(make_env: MakeEnv) -> None:
    env = make_env()
    a = agent(env, plan_json(DOCK), budget=AgentBudget(max_model_calls=1), reuse_plans=False)
    await a.plan("dock", [env.robot])
    with pytest.raises(PlanError, match="budget"):
        await a.plan("dock", [env.robot])


async def test_plan_executes_with_fencing_and_releases_after_verification(
        make_env: MakeEnv) -> None:
    env = make_env()
    plan = await agent(env, plan_json(DOCK, SNAP)).plan("inspect", [env.robot])
    handle = PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id)
    result = await handle.wait_for_verified_result(5)
    assert result.state is PlanState.VERIFIED
    assert env.runtime.resources.holder("station_a") is None
    assert "reservation_released" in env.runtime.trace.kinds(plan.plan_id)


async def test_plan_stops_on_failure(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("camera_offline")
    plan = await agent(env, plan_json(DOCK, SNAP)).plan("inspect", [env.robot])
    result = await PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id).wait(5)
    assert result.state is PlanState.FAILED and result.stopped_at == "t2"
    assert result.steps["t1"].verified
    assert result.steps["t2"].reason == "capability_unavailable"


async def test_unresolved_step_keeps_its_reservation(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("lose_link_after_motion")
    plan = await agent(env, plan_json(DOCK)).plan("dock", [env.robot])
    result = await PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id).wait(5)
    assert result.state is PlanState.UNRESOLVED
    held = result.held_reservations[0]
    assert held.resource == "station_a" and held.state is ReservationState.HELD
    assert env.runtime.resources.holder("station_a") is not None


async def test_plan_blocked_by_resource_held_elsewhere(make_env: MakeEnv) -> None:
    env = make_env()
    env.runtime.resources.reserve("station_a", "agent:other")
    plan = await agent(env, plan_json(DOCK)).plan("dock", [env.robot])
    result = await PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id).wait(5)
    assert result.state is PlanState.FAILED
    assert env.adapter.executed == []
