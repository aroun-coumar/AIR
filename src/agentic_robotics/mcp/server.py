"""An MCP server for AIR skills over stdio JSON-RPC (W6.3, ADR 0019). Standard library only.

Verified against ``modelcontextprotocol/modelcontextprotocol`` (HEAD 80041ab):
``docs/specification/2026-07-28/`` (``server/tools.mdx``, ``server/discover.mdx``,
``basic/index.mdx``, ``basic/versioning.mdx``, ``basic/transports/stdio.mdx``,
``changelog.mdx``) and ``docs/specification/2025-06-18/server/tools.mdx``.

It is a *dual-era* server: an ``initialize`` request selects the legacy handshake
(2025-06-18 or 2025-11-25) for the stdio process; a request carrying
``_meta["io.modelcontextprotocol/protocolVersion"]`` is served statelessly per 2026-07-28
(``server/discover``, ``resultType``, ``ttlMs``/``cacheScope`` on ``tools/list``,
``-32022`` UnsupportedProtocolVersion). stdio messages are single lines; logs go to stderr.

What makes this an AIR server and not a robot remote control:

- **A tool call is a proposal.** ``skill.<id>`` builds a :class:`~..contracts.execution.
  Command` under this server's delegation and calls ``Runtime.submit``. The answer is the
  handle state (``accepted``/``rejected`` ...), never "success"; ``air.command_status``
  reports VERIFIED only when the runtime verified it from observations.
- **A human can always deny.** Every tool whose skill has side effects asks the injected
  approver first; no approver, an error or no decision before the timeout is a denial,
  and the denial is traced. ``air.cancel_command`` needs no approval: stopping is
  always allowed.
- **Annotations are hints only.** The server derives ``readOnlyHint``/``destructiveHint``
  from skill specs for display; nothing it does depends on annotations, and nothing a
  client sends can widen authority.
- **Protocol errors stay separate from tool errors.** Unknown tool or malformed params is
  a JSON-RPC error; bad arguments, a denial or an admission rejection is a result with
  ``isError: true``.
- **Excluded by design** (ros-mcp-server / ROSA counter-examples): no shell, no generic
  topic/service publishing, no tool that chooses or opens a robot connection (robots are
  fixed when the server is built), no name-based ``getattr`` dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, TextIO

from .. import __version__
from ..contracts.execution import Command, CommandState
from ..contracts.skill import ParamSpec, SkillSpec
from ..core.errors import FrameworkError
from ..core.ids import new_id

__requirements__ = ("REQ05", "REQ17", "REQ18")

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18")
SUPPORTED = (MODERN, *LEGACY)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"


class Err:
    PARSE = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL = -32603
    UNSUPPORTED_VERSION = -32022


@dataclass(frozen=True)
class Proposal:
    """What a human approver is asked about. Built by the server, not by the client."""

    proposal_id: str
    tool: str
    robot_id: str
    skill_id: str
    args: dict[str, Any]
    side_effects: tuple[str, ...]
    principal: str
    delegation_id: str


ApproverResult = bool | Awaitable[bool]
Approver = Callable[[Proposal], ApproverResult]


def deny_all(_: Proposal) -> bool:
    """The default: unattended means every actuating call is denied."""
    return False


def allow_skills(*skill_ids: str) -> Approver:
    """Pre-approval given by a human operator at launch, for these skills only."""
    allowed = frozenset(skill_ids)
    return lambda p: p.skill_id in allowed


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def param_schema(p: ParamSpec) -> dict[str, Any]:
    out: dict[str, Any] = {"type": p.type}
    if p.minimum is not None:
        out["minimum"] = p.minimum
    if p.maximum is not None:
        out["maximum"] = p.maximum
    desc = " ".join(x for x in (p.description, f"[{p.unit}]" if p.unit else "") if x)
    if desc:
        out["description"] = desc
    return out


class McpSkillServer:
    def __init__(self, runtime: Any, *, principal: str, delegation_id: str,
                 approver: Approver = deny_all, approval_timeout_s: float = 60.0,
                 name: str = "air-skills", instructions: str | None = None) -> None:
        self.runtime = runtime
        self.principal = principal
        self.delegation_id = delegation_id
        self.approver = approver
        self.approval_timeout = approval_timeout_s
        self.name = name
        self.instructions = instructions or (
            "Tools submit proposals to the AIR runtime. Admission, authority and a human "
            "approver decide; poll air.command_status for the verified outcome.")
        self.legacy_version: str | None = None
        self.submitted: set[str] = set()

    # -- tool catalogue ----------------------------------------------------------------
    def robots(self) -> list[str]:
        d = self.runtime.authority.get(self.delegation_id)
        ids = sorted(r.robot_id for r in self.runtime.robots)
        return ids if "*" in d.robots else [r for r in ids if r in d.robots]

    def _skills(self) -> list[SkillSpec]:
        d = self.runtime.authority.get(self.delegation_id)
        robots = [self.runtime.robot(r) for r in self.robots()]
        out = []
        for spec in sorted(self.runtime.skills, key=lambda s: s.skill_id):
            if "*" not in d.skills and spec.skill_id not in d.skills:
                continue
            if any(spec.skill_id in r.adapter.supported_skills() for r in robots):
                out.append(spec)
        return out

    def tools(self) -> list[dict[str, Any]]:
        robots = self.robots()
        tools = []
        for spec in self._skills():
            props: dict[str, Any] = {"robot_id": {"type": "string", "enum": robots}}
            props.update({p.name: param_schema(p) for p in spec.inputs})
            required = ["robot_id", *(p.name for p in spec.inputs if p.required)]
            acting = bool(spec.side_effects)
            tools.append({
                "name": f"skill.{spec.skill_id}",
                "title": spec.skill_id.replace("_", " "),
                "description": (f"{spec.description} Submits a proposal to the AIR runtime "
                                "(admission, authority"
                                + (" and human approval" if acting else "")
                                + " apply); the reply is the command state, not success."),
                "inputSchema": {"type": "object", "properties": props, "required": required,
                                "additionalProperties": False},
                "annotations": {"title": spec.skill_id, "readOnlyHint": not acting,
                                "destructiveHint": acting, "idempotentHint": False,
                                "openWorldHint": True},
            })
        tools.append({"name": "air.command_status", "title": "command status",
                      "description": "State of a command this server submitted. VERIFIED "
                                     "means the runtime verified the outcome from observations.",
                      "inputSchema": {"type": "object", "additionalProperties": False,
                                      "properties": {"command_id": {"type": "string"}},
                                      "required": ["command_id"]},
                      "annotations": {"readOnlyHint": True, "openWorldHint": False}})
        tools.append({"name": "air.cancel_command", "title": "cancel command",
                      "description": "Request that a command this server submitted stops. "
                                     "Never needs approval.",
                      "inputSchema": {"type": "object", "additionalProperties": False,
                                      "properties": {"command_id": {"type": "string"}},
                                      "required": ["command_id"]},
                      "annotations": {"readOnlyHint": False, "destructiveHint": False,
                                      "openWorldHint": True}})
        return tools

    # -- JSON-RPC --------------------------------------------------------------------
    async def handle_line(self, line: str) -> str | None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return _dump(_error(None, Err.PARSE, "Parse error"))
        reply = await self.handle(msg)
        return None if reply is None else _dump(reply)

    async def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or \
                not isinstance(msg.get("method"), str):
            rid = msg.get("id") if isinstance(msg, dict) else None
            return _error(rid, Err.INVALID_REQUEST, "Invalid Request")
        if "id" not in msg:
            return None                      # notifications (e.g. notifications/initialized)
        rid, method = msg["id"], msg["method"]
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return _error(rid, Err.INVALID_PARAMS, "params must be an object")
        try:
            result = await self._dispatch(method, params)
        except _ProtocolError as exc:
            return _error(rid, exc.code, exc.message, exc.data)
        except Exception as exc:  # never crash the stdio loop
            return _error(rid, Err.INTERNAL, "Internal error", {"error": repr(exc)})
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _era(self, params: dict[str, Any]) -> str:
        meta = params.get("_meta") or {}
        if META_VERSION in meta:
            version = meta[META_VERSION]
            if version != MODERN:
                raise _ProtocolError(Err.UNSUPPORTED_VERSION, "Unsupported protocol version",
                                     {"supported": list(SUPPORTED), "requested": version})
            if META_CAPS not in meta:
                raise _ProtocolError(Err.INVALID_PARAMS,
                                     f"missing _meta field {META_CAPS}")
            return MODERN
        if self.legacy_version is None:
            raise _ProtocolError(Err.INVALID_PARAMS, "send initialize first, or carry "
                                 f"_meta[{META_VERSION!r}] on every request")
        return self.legacy_version

    def _complete(self, era: str, result: dict[str, Any]) -> dict[str, Any]:
        if era == MODERN:
            result = {"resultType": "complete", **result,
                      "_meta": {META_SERVER: {"name": self.name, "version": __version__}}}
        return result

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.legacy_version = asked if asked in LEGACY else LEGACY[0]
            return {"protocolVersion": self.legacy_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": __version__},
                    "instructions": self.instructions}
        if method == "server/discover":
            return {"resultType": "complete", "supportedVersions": list(SUPPORTED),
                    "capabilities": {"tools": {}},
                    "_meta": {META_SERVER: {"name": self.name, "version": __version__}},
                    "instructions": self.instructions, "ttlMs": 60000, "cacheScope": "private"}
        if method == "ping" and self.legacy_version is not None:
            return {}
        if method == "tools/list":
            era = self._era(params)
            result: dict[str, Any] = {"tools": self.tools()}
            if era == MODERN:
                result.update({"ttlMs": 5000, "cacheScope": "private"})
            return self._complete(era, result)
        if method == "tools/call":
            era = self._era(params)
            name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise _ProtocolError(Err.INVALID_PARAMS, "tools/call needs a name and "
                                     "an arguments object")
            return self._complete(era, await self._call(name, args))
        raise _ProtocolError(Err.METHOD_NOT_FOUND, f"Method not found: {method}")

    # -- tools -----------------------------------------------------------------------
    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "air.command_status":
            return self._status(args)
        if name == "air.cancel_command":
            return await self._cancel(args)
        specs = {f"skill.{s.skill_id}": s for s in self._skills()}
        spec = specs.get(name)
        if spec is None:
            raise _ProtocolError(Err.INVALID_PARAMS, f"Unknown tool: {name}")
        robot_id = args.get("robot_id")
        skill_args = {k: v for k, v in args.items() if k != "robot_id"}
        problems = [] if robot_id in self.robots() else [f"robot_id: must be one of "
                                                         f"{self.robots()}"]
        problems += spec.validate_args(skill_args)
        if problems:
            return _tool_error("invalid arguments: " + "; ".join(problems),
                               {"problems": problems})
        proposal = Proposal(new_id("prop"), name, str(robot_id), spec.skill_id, skill_args,
                            spec.side_effects, self.principal, self.delegation_id)
        if spec.side_effects:
            approved, why = await self._ask(proposal)
            self.runtime.trace.record("mcp_approval", proposal.proposal_id,
                                      approved=approved, reason=why, **asdict(proposal))
            if not approved:
                return _tool_error(f"denied: {why}; nothing was submitted",
                                   {"proposal_id": proposal.proposal_id, "denied": True})
        cmd = Command(command_id=new_id("cmd"), robot_id=proposal.robot_id,
                      skill_id=spec.skill_id, issued_by=self.principal,
                      delegation_id=self.delegation_id,
                      authority_revision=self.runtime.authority.revision, args=skill_args)
        try:
            handle = self.runtime.submit(cmd)
        except FrameworkError as exc:
            return _tool_error(str(exc), {"proposal_id": proposal.proposal_id})
        self.submitted.add(cmd.command_id)
        self.runtime.trace.record("mcp_submitted", cmd.command_id, tool=name,
                                  proposal_id=proposal.proposal_id)
        body: dict[str, Any] = {"proposal": True, "proposal_id": proposal.proposal_id,
                                "command_id": cmd.command_id, "state": handle.state.value,
                                "verified": False}
        if handle.rejection is not None:
            body["rejection"] = {"reason": handle.rejection.reason,
                                 "message": handle.rejection.message}
            return _tool_error(f"rejected by AIR admission: {handle.rejection.reason}", body)
        return _tool_ok(f"submitted {cmd.command_id} ({handle.state.value}); this is not "
                        "completion: poll air.command_status", body)

    async def _ask(self, proposal: Proposal) -> tuple[bool, str]:
        try:
            decision = self.approver(proposal)
            if inspect.isawaitable(decision):
                decision = await asyncio.wait_for(decision, self.approval_timeout)
        except TimeoutError:
            return False, "no decision before the approval timeout"
        except Exception as exc:
            return False, f"approver failed ({exc!r})"
        return (True, "approved") if decision is True else (False, "denied by the approver")

    def _own(self, args: dict[str, Any]) -> Any:
        cid = args.get("command_id")
        if not isinstance(cid, str) or cid not in self.submitted:
            return None
        return self.runtime.find(cid)

    def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        handle = self._own(args)
        if handle is None:
            return _tool_error("unknown command_id (only commands this server submitted)", {})
        body: dict[str, Any] = {"command_id": handle.command_id, "state": handle.state.value,
                                "verified": handle.state is CommandState.VERIFIED,
                                "done": handle.done}
        if handle.result is not None:
            body.update({"reason": handle.result.reason, "message": handle.result.message})
        return _tool_ok(json.dumps(body), body)

    async def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        handle = self._own(args)
        if handle is None:
            return _tool_error("unknown command_id (only commands this server submitted)", {})
        try:
            res = await self.runtime.cancel(handle.command_id, reason="mcp_cancel")
        except FrameworkError as exc:
            return _tool_error(str(exc), {"command_id": handle.command_id})
        body = {"command_id": handle.command_id, "state": res.state.value,
                "stopped_confirmed": res.observed_effects.get("stopped_confirmed")}
        return _tool_ok(json.dumps(body), body)

    # -- stdio -----------------------------------------------------------------------
    async def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None,
                          stderr: TextIO | None = None) -> None:
        """Serve newline-delimited JSON-RPC until stdin closes. Logs go to stderr only."""
        inp, out, err = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
        while True:
            line = await asyncio.to_thread(inp.readline)
            if not line:
                return
            if not line.strip():
                continue
            reply = await self.handle_line(line)
            if reply is not None:
                out.write(reply + "\n")
                out.flush()
            err.flush()


def _tool_ok(text: str, structured: Mapping[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "structuredContent": dict(structured),
            "isError": False}


def _tool_error(text: str, structured: Mapping[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "structuredContent": dict(structured),
            "isError": True}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)   # never an embedded newline


__all__ = ["LEGACY", "MODERN", "SUPPORTED", "Approver", "Err", "McpSkillServer", "Proposal",
           "allow_skills", "deny_all", "param_schema"]
