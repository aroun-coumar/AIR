"""``air mcp``: list the MCP tools for a skill set, or serve AIR skills over stdio."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

__requirements__ = ("REQ18",)


async def _build(args: argparse.Namespace) -> Any:
    from ..contracts.authority import Delegation
    from ..robot import Robot
    from ..runtime import Runtime
    from ..skills import SkillRegistry
    from .server import McpSkillServer, allow_skills

    skills = SkillRegistry.load(args.skills)
    rt = Runtime(skills, root_principals=["operator:mcp-launcher"])
    for path in args.manifest:
        rt.register(Robot.from_manifest(path))
    await rt.start()
    d = rt.authority.grant(Delegation(
        delegation_id="dlg_mcp", grantor="operator:mcp-launcher", grantee=args.principal,
        robots=tuple(r.robot_id for r in rt.robots), valid_from=rt.clock.now(),
        reason="air mcp serve"))
    return McpSkillServer(rt, principal=args.principal, delegation_id=d.delegation_id,
                          approver=allow_skills(*args.allow))


def _tools(args: argparse.Namespace) -> int:
    async def go() -> int:
        server = await _build(args)
        print(json.dumps(server.tools(), indent=2))
        await server.runtime.stop()
        return 0
    return asyncio.run(go())


def _serve(args: argparse.Namespace) -> int:
    async def go() -> int:
        server = await _build(args)
        print(f"air mcp: serving {len(server.tools())} tools on stdio; pre-approved skills: "
              f"{sorted(args.allow) or 'none (all actuating calls are denied)'}",
              file=sys.stderr)
        try:
            await server.serve_stdio()
        finally:
            await server.runtime.stop()
        return 0
    return asyncio.run(go())


def add_commands(sub: Any) -> None:
    p = sub.add_parser("mcp", help="MCP server exposing AIR skills as proposals (stdio)")
    ps = p.add_subparsers(dest="mcp_command", required=True)
    for name, fn, text in (("tools", _tools, "print the tool list"),
                           ("serve", _serve, "serve JSON-RPC on stdin/stdout")):
        c = ps.add_parser(name, help=text)
        c.add_argument("--skills", required=True, help="SkillSet JSON")
        c.add_argument("--manifest", action="append", default=[],
                       help="robot ComponentManifest JSON (repeatable; adapter must be "
                            "registered)")
        c.add_argument("--principal", default="agent:mcp-client")
        c.add_argument("--allow", action="append", default=[], metavar="SKILL",
                       help="skill a human operator pre-approves for this session; every "
                            "other actuating call is denied")
        c.set_defaults(fn=fn)


__all__ = ["add_commands"]
