"""Provider-agnostic model layer: OpenAI-compatible (local/open models), Claude via the
official SDK, robot-hosted models, and policy routing. All over real local HTTP."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentic_robotics import Robot, ScriptedModel, SkillRegistry, TaskAgent
from agentic_robotics.agents.providers import (
    AnthropicProvider,
    ChatModelAdapter,
    Location,
    ModelPolicy,
    ModelProfile,
    ModelRequestError,
    ModelRouter,
    ModelUnavailable,
    OpenAICompatibleProvider,
    ScriptedChat,
    StopReason,
    ToolDefinition,
    ToolOutcome,
    claude_profile,
    say,
)
from agentic_robotics.contracts import AdapterRef, ComponentManifest
from agentic_robotics.core import MonotonicClock
from agentic_robotics.integrations.fake import fake_skill_set
from agentic_robotics.runtime import Trace

from .httpfake import FakeServer

pytestmark = pytest.mark.req("AIR02", "REQ05", "REQ08", "REQ12", "F07")
READ = ToolDefinition("read_file", "Read a file", {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    "additionalProperties": False})


def local(endpoint: str, **kw: Any) -> ModelProfile:
    return ModelProfile(name=kw.pop("name", "local"), provider="openai_compatible",
                        model="qwen2.5:7b", location=kw.pop("location", Location.LOCAL_HOST),
                        data_leaves_device=False, endpoint=endpoint, **kw)


def chat_response(message: dict[str, Any], finish: str) -> dict[str, Any]:
    return {"model": "qwen2.5:7b", "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


# -- OpenAI-compatible (Ollama, vLLM, llama.cpp, LM Studio, ...) ---------------------------------
async def test_openai_compatible_tool_calling_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_KEY", "sk-local-test")
    tool_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}}]}
    with FakeServer([(200, chat_response(tool_msg, "tool_calls")),
                     (200, chat_response({"role": "assistant", "content": "done"}, "stop"))
                     ]) as srv:
        provider = OpenAICompatibleProvider(local(srv.url + "/v1", api_key_env="LOCAL_LLM_KEY"))
        conv = provider.start("sys", [READ])
        turn = await conv.send_user("go")
        assert turn.stop is StopReason.TOOL_USE
        assert turn.tool_calls[0].name == "read_file"
        assert turn.tool_calls[0].arguments == {"path": "a.txt"}
        final = await conv.send_tool_results([ToolOutcome("call_1", "contents")])
        assert final.text == "done" and final.stop is StopReason.END

    first, second = srv.requests
    assert first["path"] == "/v1/chat/completions"
    assert first["headers"]["authorization"] == "Bearer sk-local-test"
    body = first["body"]
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["parameters"]["required"] == ["path"]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    # The native transcript is append-only: assistant message echoed, then the tool result.
    assert second["body"]["messages"][2] == tool_msg
    assert second["body"]["messages"][3] == {"role": "tool", "tool_call_id": "call_1",
                                             "content": "contents"}


async def test_models_without_native_tools_use_the_text_protocol() -> None:
    reply = json.dumps({"tool_calls": [{"name": "read_file", "arguments": {"path": "x"}}]})
    with FakeServer([(200, chat_response({"role": "assistant", "content": reply}, "stop"))
                     ]) as srv:
        conv = OpenAICompatibleProvider(local(srv.url, supports_tools=False)).start("sys", [READ])
        turn = await conv.send_user("go")
    assert turn.tool_calls[0].arguments == {"path": "x"}
    assert "tools" not in srv.requests[0]["body"]
    assert "reply with ONLY a JSON object" in srv.requests[0]["body"]["messages"][0]["content"]


async def test_openai_compatible_retries_transient_and_rejects_permanent_errors() -> None:
    ok = chat_response({"role": "assistant", "content": "hi"}, "stop")
    with FakeServer([(503, {"error": "busy"}), (200, ok)]) as srv:
        provider = OpenAICompatibleProvider(local(srv.url), max_retries=1)
        assert (await provider.start("s", []).send_user("x")).text == "hi"
    with FakeServer([(400, {"error": "bad model"})]) as srv, \
            pytest.raises(ModelRequestError):
        await OpenAICompatibleProvider(local(srv.url)).start("s", []).send_user("x")
    with pytest.raises(ModelUnavailable):
        await OpenAICompatibleProvider(local("http://127.0.0.1:9"), max_retries=0) \
            .start("s", []).send_user("x")


# -- Claude via the official SDK -----------------------------------------------------------------
def claude_message(content: list[dict[str, Any]], stop: str, **extra: Any) -> dict[str, Any]:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 7}, **extra}


async def test_claude_request_shape_and_tool_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-test")
    with FakeServer([
        (200, claude_message([{"type": "tool_use", "id": "toolu_1", "name": "read_file",
                               "input": {"path": "a"}}], "tool_use")),
        (200, claude_message([{"type": "text", "text": "all done"}], "end_turn")),
    ]) as srv:
        provider = AnthropicProvider(claude_profile(endpoint=srv.url,
                                                    api_key_env="TEST_ANTHROPIC_KEY"),
                                     max_retries=0)
        conv = provider.start("system prompt", [READ])
        turn = await conv.send_user("go")
        assert turn.tool_calls[0].call_id == "toolu_1"
        assert turn.tool_calls[0].arguments == {"path": "a"}
        final = await conv.send_tool_results([ToolOutcome("toolu_1", "file text")])
        assert final.text == "all done" and final.stop is StopReason.END

    first, second = srv.requests
    assert first["path"].startswith("/v1/messages")
    sent = first["headers"]  # SDK 2.x sends the key as a bearer token
    assert sent.get("x-api-key") == "sk-ant-test" or \
        sent.get("authorization") == "Bearer sk-ant-test"
    assert "server-side-fallback-2026-07-01" in first["headers"]["anthropic-beta"]
    body = first["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "high"}
    assert "thinking" not in body and "tool_choice" not in body
    assert body["tools"][0]["input_schema"]["required"] == ["path"]
    msgs = second["body"]["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file text",
         "is_error": False}]}


async def test_claude_refusal_and_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-test")
    refusal = claude_message([], "refusal", stop_details={
        "type": "refusal", "category": "cyber", "explanation": "x"})
    with FakeServer([(200, refusal),
                     (429, {"type": "error", "error": {"type": "rate_limit_error",
                                                       "message": "slow down"}})]) as srv:
        profile = claude_profile(endpoint=srv.url, api_key_env="TEST_ANTHROPIC_KEY")
        turn = await AnthropicProvider(profile, max_retries=0).start("s", []).send_user("x")
        assert turn.stop is StopReason.REFUSAL and turn.refusal_category == "cyber"
        with pytest.raises(ModelUnavailable):
            await AnthropicProvider(profile, max_retries=0).start("s", []).send_user("x")


# -- routing, placement and robot-hosted intelligence ------------------------------------------
def router_with(*chats: ScriptedChat, trace: Trace | None = None) -> ModelRouter:
    r = ModelRouter(trace=trace)
    for c in chats:
        r.register(c)
    return r


def test_routing_keeps_data_close_and_honours_egress() -> None:
    cloud = ScriptedChat([], name="claude", location=Location.CLOUD)
    host = ScriptedChat([], name="workstation", location=Location.LOCAL_HOST)
    robot = ScriptedChat([], name="duck-llm", location=Location.ON_ROBOT, robot_id="duck-a")
    other = ScriptedChat([], name="other-llm", location=Location.ON_ROBOT, robot_id="duck-b")
    r = router_with(cloud, host, robot, other)
    names = lambda pol: [p.profile.name for p in r.candidates(pol)]  # noqa: E731
    assert names(ModelPolicy(robot_id="duck-a"))[0] == "duck-llm"
    assert names(ModelPolicy.on_robot_only("duck-a")) == ["duck-llm"]
    assert "claude" not in names(ModelPolicy.no_cloud())
    assert "claude" not in names(ModelPolicy(allow_data_egress=False))
    assert names(ModelPolicy(prefer=("claude",)))[0] == "claude"
    with pytest.raises(ModelRequestError, match="no registered model"):
        r.start(ModelPolicy.on_robot_only("duck-z"), "s", [])


async def test_router_falls_back_on_first_turn_then_pins() -> None:
    trace = Trace(MonotonicClock())
    robot = ScriptedChat([], name="duck-llm", location=Location.ON_ROBOT, robot_id="duck-a",
                         unavailable=True)
    host = ScriptedChat([say("from host"), say("again")], name="workstation")
    conv = router_with(robot, host, trace=trace).start(ModelPolicy(robot_id="duck-a"), "s", [])
    assert (await conv.send_user("hi")).text == "from host"
    assert conv.profile.name == "workstation"
    kinds = trace.kinds()
    assert kinds == ["model_fallback", "model_call"]
    call = trace.for_subject("workstation")[0].data
    assert call["location"] == "local_host" and call["data_leaves_device"] is False
    assert "hi" not in json.dumps(call)  # content is never traced
    host.unavailable = True
    with pytest.raises(ModelUnavailable):  # pinned: no silent mid-conversation switch
        await conv.send_tool_results([])


def test_robot_hosted_models_register_from_the_adapter() -> None:
    robot = Robot.from_manifest(ComponentManifest(
        robot_id="duck-a", name="duck", hardware_revision="1",
        adapter=AdapterRef(kind="fake", config={"hosted_models": [
            {"name": "duck-a-llm", "model": "qwen2.5:3b",
             "endpoint": "http://duck-a.local:11434/v1"}]})))
    r = ModelRouter()
    (provider,) = r.register_robot(robot)
    p = provider.profile
    assert (p.location, p.robot_id, p.data_leaves_device) == (Location.ON_ROBOT, "duck-a", False)
    assert [c.profile.name for c in r.candidates(ModelPolicy.on_robot_only("duck-a"))] == \
        ["duck-a-llm"]


async def test_any_chat_provider_can_drive_the_task_agent() -> None:
    plan = json.dumps({"steps": [{"skill_id": "navigate_to", "robot_id": "carrier-1",
                                  "args": {"x": 1, "y": 0}}]})
    adapter = ChatModelAdapter(router_with(ScriptedChat([say(plan)], name="local")))
    skills = SkillRegistry(fake_skill_set().skills)
    robot = Robot.from_manifest(ComponentManifest(
        robot_id="carrier-1", name="c", hardware_revision="1", adapter=AdapterRef(kind="fake")))
    robot.adapter.bind_clock(MonotonicClock())
    agent = TaskAgent(adapter, skills, MonotonicClock())
    assert (await agent.plan("go", [robot])).steps[0].skill_id == "navigate_to"
    assert isinstance(ScriptedModel(["x"]).name, str)
