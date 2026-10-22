"""``air incident``: open, list, propose hypotheses, hand off and close incidents.

Bounded tests and recovery command a robot, so they run in code against a live
``Runtime`` (``IncidentPipeline.run_test`` / ``recover``), not from this CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .model import Detection, Incident
from .pipeline import IncidentPipeline

__requirements__ = ("REQ14", "REQ18")


def _pipeline(args: argparse.Namespace) -> IncidentPipeline:
    from ..provenance.project import Project
    return IncidentPipeline(project=Project.open(args.project))


def _line(i: Incident) -> str:
    flag = "NOVEL " if i.novel else ""
    return (f"{i.incident_id}  {i.state.value:<13} {i.robot_id:<14} x{i.occurrences:<3} "
            f"{flag}{i.summary}")


def _triage(pipe: IncidentPipeline, inc: Incident) -> None:
    from ..triage.book import TriageBook, load_policy
    from ..triage.operations import OperationsTriage, operations_policy
    project = pipe.project
    OperationsTriage(TriageBook(project, policy=operations_policy(load_policy(project)))
                     ).incident(inc)


def cmd_list(args: argparse.Namespace) -> int:
    items = _pipeline(args).incidents(active_only=not args.all)
    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=2))
    else:
        for i in items:
            print(_line(i))
        print(f"{len(items)} incident(s)", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    i = _pipeline(args).get(args.incident_id)
    print(_line(i))
    print(f"  detector {i.detector}  signal {i.signal}  component {i.component_id or '-'}")
    print(f"  known classes: {', '.join(i.known_classes) or '- (novel)'}")
    for h in i.hypotheses:
        auth = "" if h.authoritative else " [non-authoritative]"
        print(f"  hypothesis {h.hypothesis_id} {h.status.value}{auth} by {h.proposed_by}: "
              f"{h.statement}")
    for t in i.tests:
        print(f"  test {t.test_id} -> {t.outcome.value}: {t.reason}")
        for c in t.commands:
            print(f"    {c}")
    for r in i.recovery:
        print(f"  recovery {r}")
    if i.handed_off_to:
        print(f"  handed off to {i.handed_off_to}")
    if i.resolution:
        print(f"  resolution: {i.resolution}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    pipe = _pipeline(args)
    inc = pipe.open(Detection(args.detector, args.robot, args.signal, args.summary,
                              observed_at=time.time(), component_id=args.component,
                              failure_class=args.failure_class,
                              evidence=tuple(args.evidence or ())))
    if args.triage:
        _triage(pipe, inc)
    print(_line(inc))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    h = _pipeline(args).propose(args.incident_id, args.statement, proposed_by=args.by,
                                failure_class=args.failure_class)
    note = "" if h.authoritative else " (non-authoritative: agent proposal)"
    print(f"{h.hypothesis_id}{note}")
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    pipe = _pipeline(args)
    inc = pipe.handoff(args.incident_id, to=args.to, by=args.by, reason=args.reason)
    if args.triage:
        _triage(pipe, inc)
    print(_line(inc))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    inc = _pipeline(args).close(args.incident_id, by=args.by, resolution=args.resolution,
                                learned=not args.not_learned)
    print(_line(inc))
    return 0


def add_commands(sub: Any) -> None:
    p = sub.add_parser("incident", help="incidents: detection, hypotheses, handoff, closing")
    ps = p.add_subparsers(dest="incident_command", required=True)

    def proj(x: argparse.ArgumentParser) -> argparse.ArgumentParser:
        x.add_argument("--project", default=".")
        return x

    ls = proj(ps.add_parser("list", help="active incidents (novel first)"))
    ls.add_argument("--all", action="store_true", help="include recovered, handed off, closed")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    sh = proj(ps.add_parser("show", help="one incident with hypotheses and tests"))
    sh.add_argument("incident_id")
    sh.set_defaults(fn=cmd_show)

    op = proj(ps.add_parser("open", help="record a detection as an incident"))
    op.add_argument("--robot", required=True)
    op.add_argument("--detector", required=True)
    op.add_argument("--signal", required=True)
    op.add_argument("--summary", required=True)
    op.add_argument("--component")
    op.add_argument("--failure-class", help="domain-pack failure id, e.g. D02.over_discharge")
    op.add_argument("--evidence", action="append")
    op.add_argument("--triage", action="store_true")
    op.set_defaults(fn=cmd_open)

    pr = proj(ps.add_parser("propose", help="add a hypothesis (agent ones are marked)"))
    pr.add_argument("incident_id")
    pr.add_argument("--statement", required=True)
    pr.add_argument("--by", required=True)
    pr.add_argument("--failure-class")
    pr.set_defaults(fn=cmd_propose)

    ho = proj(ps.add_parser("handoff", help="hand the incident to a person"))
    ho.add_argument("incident_id")
    ho.add_argument("--to", required=True)
    ho.add_argument("--by", required=True)
    ho.add_argument("--reason", required=True)
    ho.add_argument("--triage", action="store_true")
    ho.set_defaults(fn=cmd_handoff)

    cl = proj(ps.add_parser("close", help="close with a resolution (learns the signature)"))
    cl.add_argument("incident_id")
    cl.add_argument("--by", required=True)
    cl.add_argument("--resolution", required=True)
    cl.add_argument("--not-learned", action="store_true",
                    help="do not treat this signature as known afterwards")
    cl.set_defaults(fn=cmd_close)
