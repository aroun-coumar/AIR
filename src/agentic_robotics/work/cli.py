"""``air work`` and ``air models``: run agent work in sandboxes, review proposals."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..agents.providers.catalog import ModelCatalog, build_router
from ..provenance.project import Project
from .agent import ApprovalRequest, Decision, WorkAgent, decide, proposals
from .contracts import ProposalStatus, SideEffect, WorkTask

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
                      principal=args.principal)
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
    r.set_defaults(fn=cmd_work_run)
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
