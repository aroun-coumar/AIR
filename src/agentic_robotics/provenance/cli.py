"""``air`` provenance, traceability and debugging commands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from .explain import explain, from_ledger, from_trace_dicts, timeline
from .model import MaintenanceRecord, Replacement, RequirementSet, RequirementStatus
from .project import Project, file_digest
from .traceability import (
    TestEvidence,
    baseline_requirements,
    build_matrix,
    check,
    render_markdown,
    rows_to_json,
    scan_code,
)

__requirements__ = ("REQ02", "REQ14", "REQ15", "REQ16", "REQ18")


def _project(args: argparse.Namespace) -> Project:
    return Project.open(args.project)


def _kv(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


# -- init ------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    reqs = None
    if args.requirements:
        reqs = RequirementSet.load(args.requirements)
    elif args.baseline:
        reqs = baseline_requirements()
    p = Project.init(args.project, name=args.name, requirements=reqs)
    print(f"initialized AIR project {p.name!r} at {p.air_dir}")
    if reqs:
        print(f"requirements: {reqs.name} ({len(reqs.requirements)} items)")
    return 0


# -- ledger ----------------------------------------------------------------------------
def cmd_ledger_verify(args: argparse.Namespace) -> int:
    r = _project(args).ledger.verify()
    if r.ok:
        print(f"ledger OK: {r.entries} entries, head {r.head[:16]}")
        return 0
    print(f"ledger BROKEN at seq {r.first_bad_seq}: {r.problem}")
    return 1


def cmd_ledger_log(args: argparse.Namespace) -> int:
    entries = _project(args).ledger.query(kind=args.kind, subject=args.subject,
                                          trace_id=args.trace, prefix=args.prefix)
    for e in entries[-args.limit:]:
        if args.json:
            print(json.dumps(e.to_dict(), default=str))
        else:
            trace = f" trace={e.trace_id}" if e.trace_id else ""
            print(f"{e.seq:6d} {e.kind:<32} {e.subject:<36}{trace}")
    return 0


# -- activities and maintenance ----------------------------------------------------------
def cmd_activity_record(args: argparse.Namespace) -> int:
    p = _project(args)
    with p.activity(args.kind, args.title, requirements=args.req or (),
                    params=_kv(args.param), actors=args.actor or ()) as act:
        for u in args.used or []:
            act.use(u)
        for g in args.generated or []:
            act.generate(g)
        for n in args.note or []:
            act.note(n)
        for k, v in _kv(args.metric).items():
            act.metric(k, v)
        if args.failed:
            act.fail(args.failed)
        act.outcome = args.outcome or ""
    assert act.result is not None
    print(f"recorded {act.result.kind.value} activity {act.activity_id} "
          f"({act.result.status.value})")
    return 0


def cmd_activity_list(args: argparse.Namespace) -> int:
    for a in _project(args).activities(kind=args.kind, requirement=args.req):
        reqs = ",".join(a.requirements)
        print(f"{a.activity_id}  {a.kind.value:<12} {a.status.value:<10} {a.title}"
              + (f"  [{reqs}]" if reqs else ""))
    return 0


def cmd_maintenance_record(args: argparse.Namespace) -> int:
    p = _project(args)
    replacements = []
    for spec in args.replace or []:
        old, _, new = spec.partition("=")
        replacements.append(Replacement(removed=old, installed=new))
    checks = {k: v in (True, "pass", "ok", "true") for k, v in _kv(args.check).items()}
    record = MaintenanceRecord(
        record_id=args.record_id or f"mnt-{len(p.activities(kind='maintenance')) + 1:04d}",
        robot_id=args.robot, components=tuple(args.component), reason=args.reason,
        maintenance_type=args.type, procedure=args.procedure or "",
        performed_by=tuple(args.by or ()), findings=tuple(args.finding or ()),
        replacements=tuple(replacements),
        calibration_invalidated=tuple(args.invalidate or ()),
        return_to_service_checks=checks)
    act = p.record_maintenance(record)
    print(f"recorded maintenance {record.record_id} as {act.activity_id}: {act.outcome}")
    return 0


# -- lineage -----------------------------------------------------------------------------
def _resolve_entity(p: Project, ref: str) -> list[str]:
    path = Path(ref)
    if path.is_file():
        digest = file_digest(path)
        return [eid for eid, e in p.entities().items() if e.digest == digest]
    ents = p.entities()
    if ref in ents:
        return [ref]
    return [eid for eid in ents if eid.split("@")[0].endswith(ref) or ref in eid]


def _print_tree(node: dict[str, Any], indent: int = 0) -> None:
    pad = "  " * indent
    print(f"{pad}{node['entity']}")
    for g in node.get("generated_by", []):
        print(f"{pad}  <- {g['kind']} {g['activity']}: {g['title']} ({g['status']})")
        for u in g.get("used", []):
            _print_tree(u, indent + 3)
        for d in g.get("derived_from", []):
            print(f"{pad}      derived from:")
            _print_tree(d, indent + 4)
    if "replaces" in node:
        print(f"{pad}  replaces:")
        _print_tree(node["replaces"], indent + 2)
    for u in node.get("used_in", []):
        print(f"{pad}  -> used in {u['activity']}")
        for g in u.get("generated", []):
            _print_tree(g, indent + 3)
    for d in node.get("derivatives", []):
        print(f"{pad}  -> derivative:")
        _print_tree(d, indent + 2)
    if "replaced_by" in node:
        print(f"{pad}  -> replaced by {node['replaced_by']}")


def _graph(args: argparse.Namespace, upstream: bool) -> int:
    p = _project(args)
    ids = _resolve_entity(p, args.entity)
    if not ids:
        print(f"no recorded entity matches {args.entity!r}", file=sys.stderr)
        return 1
    for eid in ids:
        tree = p.lineage(eid) if upstream else p.impact(eid)
        if args.json:
            print(json.dumps(tree, indent=2))
        else:
            _print_tree(tree)
    return 0


# -- trace debugging ---------------------------------------------------------------------
def _events(args: argparse.Namespace) -> list[Any]:
    if args.file:
        rows = [json.loads(ln) for ln in Path(args.file).read_text("utf-8").splitlines()
                if ln.strip()]
        return from_trace_dicts(rows)
    return from_ledger(_project(args).ledger)


def cmd_trace_show(args: argparse.Namespace) -> int:
    lines = timeline(_events(args), subject=args.subject, trace_id=args.trace)
    print("\n".join(lines) if lines else "no matching events")
    return 0


def cmd_trace_explain(args: argparse.Namespace) -> int:
    print("\n".join(explain(_events(args), args.subject)))
    return 0


# -- requirements ------------------------------------------------------------------------
def cmd_requirements_matrix(args: argparse.Namespace) -> int:
    p = _project(args)
    reqs = p.requirements
    if reqs is None:
        print("project has no requirements.json (use `air init --baseline` or "
              "`air requirements set FILE`)", file=sys.stderr)
        return 2
    roots = [Path(s) for s in (args.source or ["src"])]
    code = scan_code([r if r.is_absolute() else p.root / r for r in roots], p.root)
    evidence = TestEvidence.load(Path(args.evidence) if args.evidence
                                 else p.air_dir / "test-evidence.json")
    counts: dict[str, int] = {}
    for a in p.activities():
        for r in a.requirements:
            counts[r] = counts.get(r, 0) + 1
    rows = build_matrix(reqs, code, evidence, counts)
    text = rows_to_json(rows) if args.format == "json" else render_markdown(rows, args.kind)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    if args.check:
        problems = check(rows, reqs, code, evidence)
        if not evidence.tests:
            problems.append("no test evidence found (run pytest --air-evidence ...)")
        for prob in problems:
            print(f"CHECK FAIL: {prob}", file=sys.stderr)
        if problems:
            return 1
        print("traceability check passed", file=sys.stderr)
    return 0


def cmd_requirements_claim(args: argparse.Namespace) -> int:
    """Change claimed statuses; each change is itself a ledger entry."""
    p = _project(args)
    reqs = p.requirements
    if reqs is None:
        print("project has no requirements.json", file=sys.stderr)
        return 2
    wanted: dict[str, RequirementStatus] = {}
    for item in args.claims:
        rid, _, status = item.partition("=")
        if reqs.get(rid) is None:
            print(f"unknown requirement {rid}", file=sys.stderr)
            return 2
        wanted[rid] = RequirementStatus(status)
    updated = []
    for r in reqs.requirements:
        if r.req_id in wanted and wanted[r.req_id] != r.status:
            p.ledger.append("requirement.status_changed", r.req_id,
                            {"from": r.status.value, "to": wanted[r.req_id].value,
                             "by": args.by, "reason": args.reason})
            r = dataclasses.replace(r, status=wanted[r.req_id])
        updated.append(r)
    p.set_requirements(reqs.evolve(requirements=tuple(updated)))
    print(f"updated {len(wanted)} claim(s)")
    return 0


def cmd_requirements_set(args: argparse.Namespace) -> int:
    _project(args).set_requirements(RequirementSet.load(args.file))
    print("requirements updated")
    return 0


# -- context -----------------------------------------------------------------------------
def cmd_context(args: argparse.Namespace) -> int:
    from .context_pack import build_context_pack

    text = build_context_pack(args.project, package=args.package, evidence=args.evidence)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    def proj(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".", help="project directory (default: .)")
        return p

    i = proj(sub.add_parser("init", help="initialize an AIR provenance project here"))
    i.add_argument("--name")
    i.add_argument("--baseline", action="store_true",
                   help="seed requirements with the framework baseline catalog")
    i.add_argument("--requirements", help="seed requirements from a RequirementSet JSON")
    i.set_defaults(fn=cmd_init)

    lg = sub.add_parser("ledger", help="inspect and verify the evidence ledger")
    lgs = lg.add_subparsers(dest="action", required=True)
    proj(lgs.add_parser("verify", help="check hash-chain integrity")).set_defaults(
        fn=cmd_ledger_verify)
    ll = proj(lgs.add_parser("log", help="list ledger entries"))
    ll.add_argument("--kind")
    ll.add_argument("--prefix", help="kind prefix, e.g. activity. or runtime.")
    ll.add_argument("--subject")
    ll.add_argument("--trace")
    ll.add_argument("--limit", type=int, default=50)
    ll.add_argument("--json", action="store_true")
    ll.set_defaults(fn=cmd_ledger_log)

    a = sub.add_parser("activity", help="record or list research/dev/sim/... activities")
    as_ = a.add_subparsers(dest="action", required=True)
    ar = proj(as_.add_parser("record", help="record a completed activity"))
    ar.add_argument("--kind", required=True,
                    help="research, development, simulation, experiment, evaluation, "
                         "release, deployment, execution, maintenance, calibration, "
                         "incident, learning, decision")
    ar.add_argument("--title", required=True)
    ar.add_argument("--req", action="append", help="requirement id (repeatable)")
    ar.add_argument("--used", action="append", help="input file or entity id (repeatable)")
    ar.add_argument("--generated", action="append", help="output file (repeatable)")
    ar.add_argument("--note", action="append")
    ar.add_argument("--actor", action="append")
    ar.add_argument("--param", action="append", help="key=value (JSON values allowed)")
    ar.add_argument("--metric", action="append", help="key=value")
    ar.add_argument("--outcome")
    ar.add_argument("--failed", metavar="REASON", help="record the activity as failed")
    ar.set_defaults(fn=cmd_activity_record)
    al = proj(as_.add_parser("list"))
    al.add_argument("--kind")
    al.add_argument("--req")
    al.set_defaults(fn=cmd_activity_list)

    m = sub.add_parser("maintenance", help="record maintenance with component lineage")
    ms = m.add_subparsers(dest="action", required=True)
    mr = proj(ms.add_parser("record"))
    mr.add_argument("--robot", required=True)
    mr.add_argument("--component", action="append", required=True)
    mr.add_argument("--reason", required=True)
    mr.add_argument("--type", default="corrective",
                    help="corrective, scheduled, usage, condition, predictive")
    mr.add_argument("--procedure")
    mr.add_argument("--record-id")
    mr.add_argument("--by", action="append", help="technician or mechanism (repeatable)")
    mr.add_argument("--finding", action="append")
    mr.add_argument("--replace", action="append", metavar="OLD=NEW")
    mr.add_argument("--invalidate", action="append", help="calibration invalidated")
    mr.add_argument("--check", action="append", metavar="NAME=pass|fail",
                    help="return-to-service check result")
    mr.set_defaults(fn=cmd_maintenance_record)

    for name, upstream, help_ in (("lineage", True, "where an entity came from"),
                                  ("impact", False, "what depends on an entity")):
        g = proj(sub.add_parser(name, help=help_))
        g.add_argument("entity", help="entity id, name, or a file path")
        g.add_argument("--json", action="store_true")
        g.set_defaults(fn=lambda a, up=upstream: _graph(a, up))

    t = sub.add_parser("trace", help="debug runtime traces (file or project ledger)")
    ts = t.add_subparsers(dest="action", required=True)
    tsh = proj(ts.add_parser("show", help="timeline"))
    tsh.add_argument("--file", help="runtime *.trace.jsonl (default: project ledger)")
    tsh.add_argument("--subject")
    tsh.add_argument("--trace")
    tsh.set_defaults(fn=cmd_trace_show)
    te = proj(ts.add_parser("explain", help="explain a command/plan outcome"))
    te.add_argument("subject", help="command id, plan id, ...")
    te.add_argument("--file")
    te.set_defaults(fn=cmd_trace_explain)

    rq = sub.add_parser("requirements", help="requirements traceability")
    rqs = rq.add_subparsers(dest="action", required=True)
    rm = proj(rqs.add_parser("matrix", help="requirement -> code -> tests -> activities"))
    rm.add_argument("--evidence", help="pytest evidence JSON (default .air/test-evidence.json)")
    rm.add_argument("--source", action="append", help="source root to scan (default src)")
    rm.add_argument("--kind", action="append",
                    help="requirement, gate, foundation, domain (default all)")
    rm.add_argument("--format", choices=["md", "json"], default="md")
    rm.add_argument("--out")
    rm.add_argument("--check", action="store_true",
                    help="fail if any claimed status is not backed by evidence")
    rm.set_defaults(fn=cmd_requirements_matrix)
    rc = proj(rqs.add_parser("claim", help="change claimed statuses (recorded in ledger)"))
    rc.add_argument("claims", nargs="+", metavar="ID=STATUS",
                    help="planned, in_progress, implemented, verified")
    rc.add_argument("--by", default="unspecified")
    rc.add_argument("--reason", default="")
    rc.set_defaults(fn=cmd_requirements_claim)
    rset = proj(rqs.add_parser("set", help="replace the project's requirement set"))
    rset.add_argument("file")
    rset.set_defaults(fn=cmd_requirements_set)

    c = proj(sub.add_parser("context", help="generate a context pack for people and agents"))
    c.add_argument("--out")
    c.add_argument("--package")
    c.add_argument("--evidence")
    c.set_defaults(fn=cmd_context)
