"""dora operator mapping and dataflow validation (no dora/pyarrow needed)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentic_robotics.integrations.dora import (
    SkillOperatorCore,
    make_operator,
    validate_dataflow,
)
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set

from .bridge_env import AGENT, make_env
from .conftest import manifest as fake_manifest

pytestmark = pytest.mark.req("REQ05", "REQ18")


class _Arrow:
    """Duck-typed Arrow array: dora hands operators ``pyarrow`` arrays."""

    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def to_pylist(self) -> list[Any]:
        return self.items


async def _core():  # type: ignore[no-untyped-def]
    m = fake_manifest()
    adapter = FakeRobotAdapter(m, m.adapter.config)
    env = await make_env(adapter, fake_skill_set().skills)
    return env, SkillOperatorCore(env.runtime, principal=AGENT,
                                  delegation_id=env.delegation.delegation_id)


async def test_command_becomes_a_proposal_and_result_maps_goal_status() -> None:
    env, core = await _core()
    out: list[tuple[str, Any, dict[str, Any]]] = []
    try:
        cmd = {"robot_id": "carrier-1", "skill_id": "navigate_to", "args": {"x": 0.4, "y": 0.0}}
        await core.handle({"type": "INPUT", "id": "command", "value": _Arrow([cmd]),
                           "metadata": {"goal_id": "g1"}}, lambda *a: out.append(a))
        assert out[0][0] == "status" and out[0][2]["goal_id"] == "g1"
        handle = env.runtime.handle(out[0][2]["air_command_id"])
        await handle.wait(5)
        await core.handle({"type": "INPUT", "id": "tick", "value": None, "metadata": {}},
                          lambda *a: out.append(a))
        port, data, meta = out[-1]
        assert port == "result" and meta["goal_status"] == "succeeded"
        assert meta["goal_id"] == "g1" and json.loads(data)["state"] == "verified"
    finally:
        await env.runtime.stop()


async def test_rejected_and_invalid_commands_are_aborted_never_succeeded() -> None:
    env, core = await _core()
    out: list[tuple[str, Any, dict[str, Any]]] = []
    try:
        bad = json.dumps({"robot_id": "carrier-1", "skill_id": "navigate_to",
                          "args": {"x": 999.0, "y": 0.0}}).encode()
        await core.handle({"type": "INPUT", "id": "command", "value": bad,
                           "metadata": {"request_id": "r1"}}, lambda *a: out.append(a))
        await core.handle({"type": "INPUT", "id": "tick"}, lambda *a: out.append(a))
        assert out[-1][2]["goal_status"] == "aborted" and out[-1][2]["air_state"] == "rejected"
        await core.handle({"type": "INPUT", "id": "command",
                           "value": b'{"robot_id": "carrier-1", "skill_id": "x", "shell": "rm"}'},
                          lambda *a: out.append(a))
        assert out[-1][2]["air_state"] == "invalid"
        assert await core.handle({"type": "STOP"}, lambda *a: None) == 1
    finally:
        await env.runtime.stop()


async def test_cancel_by_goal_id() -> None:
    env, core = await _core()
    out: list[tuple[str, Any, dict[str, Any]]] = []
    try:
        cmd = {"robot_id": "carrier-1", "skill_id": "navigate_to", "args": {"x": 40.0, "y": 0.0}}
        env.adapter.speed = 0.05            # type: ignore[attr-defined]
        await core.handle({"type": "INPUT", "id": "command", "value": json.dumps(cmd),
                           "metadata": {"goal_id": "g2"}}, lambda *a: out.append(a))
        await core.handle({"type": "INPUT", "id": "cancel", "value": "g2"},
                          lambda *a: out.append(a))
        assert out[-1][0] == "result" and out[-1][2]["goal_status"] == "canceled"
    finally:
        await env.runtime.stop()


def test_make_operator_runs_on_its_own_loop() -> None:
    from agentic_robotics import Runtime, SkillRegistry

    def build() -> SkillOperatorCore:
        return SkillOperatorCore(Runtime(SkillRegistry(fake_skill_set().skills)),
                                 principal=AGENT, delegation_id="none")

    op = make_operator(build)()
    out: list[Any] = []
    status = op.on_event({"type": "INPUT", "id": "command", "value": b"[]"},
                         lambda *a: out.append(a))
    assert int(status) == 0 and out[0][2]["air_state"] == "invalid"


DATAFLOW = {"nodes": [
    {"id": "planner", "path": "planner.py", "inputs": {"tick": "dora/timer/millis/100",
                                                         "result": "air/result"},
     "outputs": ["command"]},
    {"id": "air", "operator": {"python": "air_operator.py",
                               "inputs": {"command": "planner/command",
                                          "tick": "dora/timer/millis/20"},
                               "outputs": ["status", "result"]}},
]}


def test_dataflow_validation() -> None:
    assert validate_dataflow(DATAFLOW, air_nodes=["air"]) == []
    broken = {"nodes": [
        {"id": "planner", "path": "p.py", "inputs": {"x": "ghost/out", "t": "dora/timer/hours/1"},
         "outputs": ["command", "command"]},
        {"id": "air", "operator": {"python": "a.py", "inputs": {"command": "planner/nope",
                                                                  "shell": "planner/command"},
                                   "outputs": ["status"]}},
        {"id": "air"},
    ]}
    problems = validate_dataflow(broken, air_nodes=["air", "missing"])
    joined = "\n".join(problems)
    for needle in ("unknown node 'ghost'", "unknown dora source", "duplicate outputs",
                   "has no output 'nope'", "needs input 'tick'", "unknown AIR operator inputs",
                   "must declare outputs", "duplicate node id", "'missing' is not in"):
        assert needle in joined, (needle, problems)
    assert validate_dataflow({}) == ["dataflow has no 'nodes' list"]
