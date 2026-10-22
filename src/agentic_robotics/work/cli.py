"""``air work`` and ``air models``: run agent work in sandboxes, review proposals."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..agents.providers.catalog import ModelCatalog, build_router
from ..provenance.project import Project
from .agent import WorkAgent, decide, proposals
from .approval import ApprovalRequest, Decision
from .contracts import ProposalStatus, SideEffect, WorkTask
from .execpolicy import DEFAULT_COMMAND_POLICY, CommandPolicy
from .sandbox import (
    NetworkMode,
    SandboxPolicy,
    bwrap_argv,
    landrun_argv,
    probe_adapters,
    seatbelt_profile,
)

__requirements__ = ("AIR01", "REQ02", "REQ18")


def _catalog(args: argparse.Namespace, project: Project) -> ModelCatalog:
    path = Path(args.models) if args.models else project.air_dir / "models.json"
    if not path.exists():
        raise SystemExit(f"no model catalog at {path} (see `air models --help`)")
    return ModelCatalog.load(path)


def cmd_models(args: argparse.Namespace) -> int:
    project = Project.open(args.project)
    for m in _catalog(args, project).models:
        egress = "data leaves device" if m.data_leaves_device else "data stays on device"
        where = f"{m.location.value}" + (f" ({m.robot_id})" if m.robot_id else "")
        print(f"{m.name:<20} {m.provider:<18} {m.model:<24} {where:<22} {egress}")
    return 0


def cmd_work_run(args: argparse.Namespace) -> int:
    project = Project.open(args.project)
    task = WorkTask.load(args.task)
    allowed = {SideEffect(s) for s in (args.approve or [])}

    async def approver(req: ApprovalRequest) -> Decision:
        if req.side_effect in allowed:
            return Decision(True, args.approver, "pre-approved on the command line")
        return Decision(False, "policy:default-deny", "not pre-approved (--approve)")

    agent = WorkAgent(project, build_router(_catalog(args, project)), approver=approver,
                      principal=args.principal, unattended=args.unattended)
    result = asyncio.run(agent.run(task))
    print(f"{task.task_id}: {result.status.value} (model calls {result.model_calls}, "
          f"tool calls {result.tool_calls}, served by {', '.join(result.served_by) or '-'})")
    for p in result.proposals:
        print(f"  proposal {p.proposal_id}: [{p.kind.value}] {p.title}")
    for problem in result.problems:
        print(f"  problem: {problem}", file=sys.stderr)
    if result.final_text:
        print(result.final_text)
    return 0 if result.status.value == "completed" else 1


def cmd_work_proposals(args: argparse.Namespace) -> int:
    status = ProposalStatus(args.status) if args.status else None
    for p in proposals(Project.open(args.project), status):
        print(f"{p.proposal_id}  {p.status.value:<9} {p.kind.value:<18} {p.title}"
              f"  (task {p.task_id}, by {p.produced_by})")
    return 0


def cmd_work_decide(args: argparse.Namespace) -> int:
    p = decide(Project.open(args.project), args.proposal_id, by=args.by,
               approve=args.decision == "approve", reason=args.reason or "")
    print(f"{p.proposal_id}: {p.status.value}")
    return 0


def _rules(paths: list[str] | None, builtin: bool) -> CommandPolicy:
    policy = DEFAULT_COMMAND_POLICY if builtin else CommandPolicy()
    return policy.merged(*(CommandPolicy.load(p) for p in paths or []))


def cmd_rules_check(args: argparse.Namespace) -> int:
    argv = [a for a in args.command if a != "--"]
    if not argv:
        raise SystemExit("give the command after --")
    ev = _rules(args.rules, not args.no_builtin).evaluate(argv)
    print(json.dumps(ev.to_dict(), indent=2 if args.pretty else None))
    return {"allow": 0, "prompt": 3, "forbidden": 4}[ev.decision.value]


def cmd_rules_validate(args: argparse.Namespace) -> int:
    policy = CommandPolicy.load(args.rules)  # examples are checked while loading
    n = sum(len(r.match) + len(r.not_match) for r in policy.rules)
    print(f"{args.rules}: {len(policy.rules)} rules, {n} examples hold")
    return 0


def _policy(path: str | None) -> SandboxPolicy:
    return SandboxPolicy.load(path) if path else SandboxPolicy()


def cmd_sandbox_probe(args: argparse.Namespace) -> int:
    rows = probe_adapters(_policy(args.policy))
    if args.json:
        print(json.dumps([{"adapter": r.name, "available": r.available,
                           "honours_policy": r.honours_policy, "problems": list(r.problems),
                           "capabilities": r.capabilities.to_dict() if r.capabilities else None}
                          for r in rows], indent=2))
        return 0
    for r in rows:
        if r.capabilities is None:
            print(f"{r.name:<11} unavailable: {'; '.join(r.problems)}")
            continue
        caps = r.capabilities
        verdict = "honours the policy" if r.honours_policy else \
            f"cannot honour it ({'; '.join(r.problems)})"
        abi = f", landlock ABI v{caps.landlock_abi}" if caps.landlock_abi else ""
        print(f"{r.name:<11} {caps.isolation.value}/{caps.placement.value}{abi}: {verdict}")
        print(f"{'':<11} enforces: {', '.join(sorted(caps.enforced))}")
        print(f"{'':<11} does not: {', '.join(caps.unenforced()) or '-'}")
    return 0


def cmd_sandbox_preview(args: argparse.Namespace) -> int:
    """Print the wrapper command (and Seatbelt profile) an adapter would run, without
    running anything. Works on any host."""
    policy = _policy(args.policy)
    cmd = [a for a in args.command if a != "--"] or ["python", "job.py"]
    ws = Path(args.workspace)
    env = {"HOME": str(ws), "PATH": "/usr/bin:/bin"}
    if args.adapter == "seatbelt":
        port = 3128 if policy.network is NetworkMode.PROXY else None
        print(seatbelt_profile(policy, ws, proxy_port=port), end="")
        print(json.dumps(["sandbox-exec", "-p", "<profile above>", *cmd]))
        return 0
    if args.adapter == "bubblewrap":
        argv = bwrap_argv("bwrap", policy, ws, cmd, env, proxy_dir=Path("/run/air-proxy"),
                          proxy_socket="/run/air-proxy/proxy.sock")
    else:
        argv = landrun_argv("landrun", policy, ws, cmd, env, abi=args.abi, proxy_port=3128)
    print(json.dumps(argv))
    return 0


def add_sandbox_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    s = sub.add_parser("sandbox", help="what each sandbox adapter enforces on this host")
    ss = s.add_subparsers(dest="action", required=True)
    pr = ss.add_parser("probe", help="list adapters, what they enforce, and whether they "
                                     "can honour a policy")
    pr.add_argument("--policy", help="SandboxPolicy JSON (default: the default policy)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(fn=cmd_sandbox_probe)
    pv = ss.add_parser("preview", help="print the command an adapter would run")
    pv.add_argument("--adapter", choices=["bubblewrap", "landlock", "seatbelt"],
                    required=True)
    pv.add_argument("--policy")
    pv.add_argument("--workspace", default="/tmp/air-sbx-preview")
    pv.add_argument("--abi", type=int, default=None, help="Landlock ABI to assume")
    pv.add_argument("command", nargs=argparse.REMAINDER)
    pv.set_defaults(fn=cmd_sandbox_preview)


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    m = sub.add_parser("models", help="list the project's model catalog (.air/models.json)")
    m.add_argument("--project", default=".")
    m.add_argument("--models", help="model catalog JSON (default .air/models.json)")
    m.set_defaults(fn=cmd_models)

    w = sub.add_parser("work", help="agent work: research, development, simulation, "
                                    "evaluation, maintenance, diagnosis")
    ws = w.add_subparsers(dest="action", required=True)
    r = ws.add_parser("run", help="run a WorkTask in a sandbox")
    r.add_argument("task", help="WorkTask JSON ($schema ar.work_task/1.0)")
    r.add_argument("--project", default=".")
    r.add_argument("--models")
    r.add_argument("--principal", default="agent:work")
    r.add_argument("--approve", action="append", choices=[s.value for s in SideEffect],
                   help="pre-approve a side effect for this run (repeatable); "
                        "everything requiring approval is denied otherwise")
    r.add_argument("--approver", default="cli:operator")
    r.add_argument("--unattended", action="store_true",
                   help="dont_ask mode: every approval ask becomes a deny")
    r.set_defaults(fn=cmd_work_run)
    rules = ws.add_parser("rules", help="command policy rules for the `run` tool")
    rs = rules.add_subparsers(dest="rules_action", required=True)
    rc = rs.add_parser("check", help="evaluate a command (exit 0 allow, 3 prompt, "
                                     "4 forbidden)")
    rc.add_argument("--rules", action="append", help="ar.command_policy JSON (repeatable)")
    rc.add_argument("--no-builtin", action="store_true", help="ignore the built-in rules")
    rc.add_argument("--pretty", action="store_true")
    rc.add_argument("command", nargs=argparse.REMAINDER)
    rc.set_defaults(fn=cmd_rules_check)
    rv = rs.add_parser("validate", help="load a rules file and check its examples")
    rv.add_argument("rules")
    rv.set_defaults(fn=cmd_rules_validate)
    p = ws.add_parser("proposals", help="list proposals")
    p.add_argument("--project", default=".")
    p.add_argument("--status", choices=[s.value for s in ProposalStatus])
    p.set_defaults(fn=cmd_work_proposals)
    d = ws.add_parser("decide", help="approve or reject a proposal")
    d.add_argument("proposal_id")
    d.add_argument("decision", choices=["approve", "reject"])
    d.add_argument("--by", required=True)
    d.add_argument("--reason")
    d.add_argument("--project", default=".")
    d.set_defaults(fn=cmd_work_decide)
    add_sandbox_commands(sub)
