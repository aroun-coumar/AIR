"""MCP server: dual-era protocol, proposals through Runtime.submit, human denial."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set
from agentic_robotics.mcp import MODERN, Err, McpSkillServer, Proposal, allow_skills, deny_all

from .bridge_env import AGENT, make_env
from .conftest import manifest as fake_manifest

pytestmark = pytest.mark.req("REQ05", "REQ17", "REQ18")

META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"}}


async def _server(approver: Any = deny_all, **kw: Any):  # type: ignore[no-untyped-def]
    m = fake_manifest()
    adapter = FakeRobotAdapter(m, m.adapter.config)
    env = await make_env(adapter, fake_skill_set().skills)
    return env, McpSkillServer(env.runtime, principal=AGENT,
                               delegation_id=env.delegation.delegation_id,
                               approver=approver, **kw)


def _req(method: str, params: dict[str, Any] | None = None, rid: int = 1,
         modern: bool = True) -> dict[str, Any]:
    p = dict(params or {})
    if modern:
        p["_meta"] = META
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


async def test_legacy_handshake_and_modern_discover() -> None:
    env, srv = await _server()
    try:
        r = await srv.handle(_req("tools/list", modern=False))
        assert r["error"]["code"] == Err.INVALID_PARAMS                # no handshake yet
        init = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18",
                                            "capabilities": {},
                                            "clientInfo": {"name": "c", "version": "1"}}})
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert "tools" in init["result"]["capabilities"]
        assert await srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) \
            is None
        legacy = await srv.handle(_req("tools/list", modern=False))
        assert "resultType" not in legacy["result"]
        assert (await srv.handle(_req("ping", modern=False)))["result"] == {}
        disc = await srv.handle(_req("server/discover"))
        assert disc["result"]["resultType"] == "complete"
        assert MODERN in disc["result"]["supportedVersions"]
        modern = await srv.handle(_req("tools/list"))
        assert modern["result"]["resultType"] == "complete"
        assert {"ttlMs", "cacheScope"} <= set(modern["result"])
        bad = await srv.handle(_req("tools/list", {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "1900-01-01",
            "io.modelcontextprotocol/clientCapabilities": {}}}, modern=False))
        assert bad["error"]["code"] == -32022
        assert bad["error"]["data"]["requested"] == "1900-01-01"
        nocaps = await srv.handle(_req("tools/list", {"_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN}}, modern=False))
        assert nocaps["error"]["code"] == Err.INVALID_PARAMS
    finally:
        await env.runtime.stop()


async def test_tool_list_is_derived_from_skill_specs_and_deterministic() -> None:
    env, srv = await _server()
    try:
        tools = srv.tools()
        assert tools == srv.tools()
        names = [t["name"] for t in tools]
        assert "skill.navigate_to" in names and "air.command_status" in names
        assert not any("shell" in n or "publish" in n or "connect" in n for n in names)
        nav = next(t for t in tools if t["name"] == "skill.navigate_to")
        assert nav["inputSchema"]["properties"]["robot_id"]["enum"] == ["carrier-1"]
        assert nav["inputSchema"]["additionalProperties"] is False
        assert nav["annotations"]["destructiveHint"] is True
        assert all(len(t["name"]) <= 128 and all(c.isalnum() or c in "_-." for c in t["name"])
                   for t in tools)
    finally:
        await env.runtime.stop()


async def test_protocol_errors_are_separate_from_tool_errors() -> None:
    env, srv = await _server()
    try:
        unknown = await srv.handle(_req("tools/call", {"name": "shell.exec", "arguments": {}}))
        assert unknown["error"]["code"] == Err.INVALID_PARAMS
        assert (await srv.handle(_req("no/such")))["error"]["code"] == Err.METHOD_NOT_FOUND
        assert json.loads(await srv.handle_line("{not json"))["error"]["code"] == Err.PARSE
        bad = await srv.handle(_req("tools/call", {"name": "skill.navigate_to",
                                                   "arguments": {"robot_id": "carrier-1",
                                                                 "x": "far"}}))
        assert bad["result"]["isError"] is True and bad["result"]["resultType"] == "complete"
        assert "problems" in bad["result"]["structuredContent"]
    finally:
        await env.runtime.stop()


async def test_unattended_server_denies_actuation_and_records_it() -> None:
    env, srv = await _server()
    try:
        r = await srv.handle(_req("tools/call", {"name": "skill.navigate_to", "arguments": {
            "robot_id": "carrier-1", "x": 1.0, "y": 0.0}}))
        assert r["result"]["isError"] is True and r["result"]["structuredContent"]["denied"]
        assert env.adapter.executed == []                          # type: ignore[attr-defined]
        kinds = [e.kind for e in env.runtime.trace]
        assert "mcp_approval" in kinds and "submitted" not in kinds
    finally:
        await env.runtime.stop()


async def test_approved_call_is_a_proposal_verified_only_by_the_runtime() -> None:
    seen: list[Proposal] = []

    async def human(p: Proposal) -> bool:
        seen.append(p)
        return p.skill_id == "navigate_to"

    env, srv = await _server(approver=human)
    try:
        r = await srv.handle(_req("tools/call", {"name": "skill.navigate_to", "arguments": {
            "robot_id": "carrier-1", "x": 0.5, "y": 0.0}}))
        body = r["result"]["structuredContent"]
        assert r["result"]["isError"] is False and body["proposal"] is True
        assert body["state"] == "accepted" and body["verified"] is False
        assert seen[0].principal == AGENT and seen[0].args == {"x": 0.5, "y": 0.0}
        await env.runtime.handle(body["command_id"]).wait(5)
        st = await srv.handle(_req("tools/call", {"name": "air.command_status",
                                                  "arguments": {"command_id":
                                                                body["command_id"]}}))
        assert st["result"]["structuredContent"]["verified"] is True
        foreign = await srv.handle(_req("tools/call", {"name": "air.command_status",
                                                       "arguments": {"command_id": "cmd_x"}}))
        assert foreign["result"]["isError"] is True
    finally:
        await env.runtime.stop()


async def test_admission_still_applies_after_approval() -> None:
    env, srv = await _server(approver=allow_skills("navigate_to"))
    try:
        env.runtime.authority.revoke(env.delegation.delegation_id, "operator withdrew")
        r = await srv.handle(_req("tools/call", {"name": "skill.navigate_to", "arguments": {
            "robot_id": "carrier-1", "x": 0.5, "y": 0.0}}))
        assert r["result"]["isError"] is True or r.get("error")
    finally:
        await env.runtime.stop()


async def test_approval_timeout_and_errors_deny() -> None:
    async def slow(_: Proposal) -> bool:
        await asyncio.sleep(1)
        return True

    def broken(_: Proposal) -> bool:
        raise RuntimeError("approval UI crashed")

    for approver in (slow, broken):
        env, srv = await _server(approver=approver, approval_timeout_s=0.05)
        try:
            r = await srv.handle(_req("tools/call", {"name": "skill.navigate_to", "arguments": {
                "robot_id": "carrier-1", "x": 0.5, "y": 0.0}}))
            assert r["result"]["structuredContent"]["denied"] is True
        finally:
            await env.runtime.stop()


async def test_cancel_needs_no_approval() -> None:
    env, srv = await _server(approver=allow_skills("navigate_to"))
    try:
        env.adapter.speed = 0.05                              # type: ignore[attr-defined]
        r = await srv.handle(_req("tools/call", {"name": "skill.navigate_to", "arguments": {
            "robot_id": "carrier-1", "x": 40.0, "y": 0.0}}))
        cid = r["result"]["structuredContent"]["command_id"]
        await asyncio.sleep(0.02)
        c = await srv.handle(_req("tools/call", {"name": "air.cancel_command",
                                                 "arguments": {"command_id": cid}}))
        assert c["result"]["structuredContent"]["state"] == "cancelled"
    finally:
        await env.runtime.stop()


async def test_stdio_framing() -> None:
    env, srv = await _server()
    try:
        lines = "\n".join(json.dumps(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})) + "\n"
        out, err = io.StringIO(), io.StringIO()
        await srv.serve_stdio(io.StringIO(lines), out, err)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        assert [r["id"] for r in replies] == [1, 2]
        assert replies[0]["result"]["protocolVersion"] == "2025-11-25"
    finally:
        await env.runtime.stop()


def test_cli_tools(capsys: pytest.CaptureFixture[str]) -> None:
    from pathlib import Path

    from agentic_robotics.cli import main

    root = Path(__file__).resolve().parents[1]
    code = main(["mcp", "tools", "--skills", str(root / "examples/skills/manifest.json"),
                 "--manifest", str(root / "examples/robots/carrier.manifest.json")])
    assert code == 0
    names = [t["name"] for t in json.loads(capsys.readouterr().out)]
    assert "skill.navigate_to" in names


def test_doctor_never_raises(capsys: pytest.CaptureFixture[str],
                             monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_robotics import doctor
    from agentic_robotics.cli import main

    assert main(["doctor"]) == 0
    assert "environment:" in capsys.readouterr().out
    assert main(["doctor", "--json"]) == 0
    checks = json.loads(capsys.readouterr().out)
    assert {c["group"] for c in checks} >= {"environment", "air", "optional", "tools"}

    def boom() -> None:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(doctor.platform, "platform", boom)
    assert any(c.status == "fail" for c in doctor.run_checks())
    monkeypatch.setattr(doctor, "run_checks", boom)
    assert main(["doctor"]) == 0
