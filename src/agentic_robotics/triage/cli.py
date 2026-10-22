"""``air triage``: sweep, list, decide and silence triage items."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from ..provenance.project import Project
from .book import TriageBook, policy_path
from .collectors import ALL_CHECKS, sweep
from .contracts import Priority, Severity, TriageItem
from .policy import Matcher, default_policy

__requirements__ = ("REQ14", "REQ15", "REQ17")

_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", text)
    if not m:
        raise SystemExit(f"bad duration {text!r} (use e.g. 45s, 30m, 2h, 1d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def _when(t: float | None) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M") if t else "-"


def _book(args: argparse.Namespace) -> TriageBook:
    return TriageBook(Project.open(args.project))


def _line(i: TriageItem, now: float) -> str:
    flags = ("OVERDUE " if i.overdue(now) else "") + ("STALE " if i.stale else "") + \
        ("locked " if i.priority_locked else "") + \
        (f"suppressed by {i.suppressed_by} " if i.suppressed_by else "")
    who = i.assignee or i.owner
    return (f"{i.item_id}  {i.severity.value:<9} {i.priority.value} {i.state.value:<11} "
            f"x{i.count:<4} {who:<22} {flags}{i.title}")


def cmd_sweep(args: argparse.Namespace) -> int:
    book = _book(args)
    report = sweep(book, checks=args.check or ALL_CHECKS,
                   evidence_path=Path(args.evidence) if args.evidence else None,
                   sources=[Path(s) for s in args.source] if args.source else None)
    print(report.summary())
    for i in report.escalated:
        print(f"  escalated {i.item_id} to {i.owner}: {i.title}")
    open_protected = [i for i in book.items() if i.severity.protected]
    return 1 if args.strict and open_protected else 0


def cmd_list(args: argparse.Namespace) -> int:
    book = _book(args)
    items = book.items(include_closed=args.all, include_suppressed=args.suppressed,
                       severity=Severity(args.severity) if args.severity else None,
                       owner=args.owner, overdue=args.overdue)
    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=2))
        return 0
    now = book.now()
    for i in items:
        print(_line(i, now))
    print(f"{len(items)} item(s)", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    book = _book(args)
    i = book.get(args.item_id)
    print(_line(i, book.now()))
    print(f"  kind {i.kind}  source {i.source}  subject {i.subject}  rule {i.rule}")
    print(f"  robot {i.robot_id or '-'}  requirements {', '.join(i.requirements) or '-'}")
    print(f"  first seen {_when(i.first_seen)}  last seen {_when(i.last_seen)}  "
          f"ack due {_when(i.ack_due)}  resolve due {_when(i.resolve_due)}")
    print(f"  escalation chain {' -> '.join((i.owner, *i.escalation))} "
          f"(level {i.escalation_level})")
    if i.labels:
        print("  labels " + ", ".join(f"{k}={v}" for k, v in sorted(i.labels.items())))
    print("  evidence " + (", ".join(i.evidence) or "-"))
    print("  history:")
    for h in book.history(i.item_id):
        if h["kind"] == "triage.transition":
            print(f"    {_when(h['ts'])} {h['from']} -> {h['to']} by {h['by']}: {h['reason']}")
        elif h["kind"] == "triage.occurred":
            print(f"    {_when(h['ts'])} seen again (count {h['set'].get('count')})")
        elif h["kind"] == "triage.suppressed":
            print(f"    {_when(h['ts'])} suppressed_by={h['set'].get('suppressed_by')}")
        else:
            print(f"    {_when(h['ts'])} {h['kind']}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    book = _book(args)
    action = args.triage_action
    if action == "ack":
        item = book.acknowledge(args.item_id, args.by, args.reason or "")
    elif action == "assign":
        item = book.assign(args.item_id, args.assignee, args.by, args.reason or "")
    elif action == "start":
        item = book.start(args.item_id, args.by, args.reason or "")
    elif action == "resolve":
        item = book.resolve(args.item_id, args.by, args.reason, args.evidence or ())
    elif action == "dismiss":
        item = book.dismiss(args.item_id, args.by, args.reason)
    elif action == "prioritize":
        item = book.set_priority(args.item_id, Priority(args.priority), args.by, args.reason)
        print(f"{item.item_id}: priority {item.priority.value} (locked)")
        return 0
    elif action == "snooze":
        item = book.snooze(args.item_id, args.by, args.reason,
                           for_s=parse_duration(args.duration) if args.duration else None,
                           wake_after_count=args.count)
    else:
        item = book.reopen(args.item_id, args.by, args.reason)
    print(f"{item.item_id}: {item.state.value}")
    return 0


def cmd_silence(args: argparse.Namespace) -> int:
    book = _book(args)
    if args.silence_action == "add":
        s = book.silence([Matcher.parse(x) for x in args.match],
                         duration_s=parse_duration(args.duration), by=args.by,
                         comment=args.comment)
        print(f"{s.silence_id} until {_when(s.ends_at)} "
              "(safety and integrity items are never silenced)")
    elif args.silence_action == "end":
        book.unsilence(args.silence_id, args.by)
        print(f"{args.silence_id} ended")
    else:
        for s in book.silences(active_only=not args.all):
            print(f"{s.silence_id}  {_when(s.starts_at)} .. {_when(s.ends_at)}  "
                  f"{' '.join(map(str, s.matchers))}  by {s.created_by}: {s.comment}")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    project = Project.open(args.project)
    if args.write:
        path = policy_path(project)
        if path.exists() and not args.force:
            raise SystemExit(f"{path} exists (use --force to overwrite)")
        default_policy().save(path)
        print(f"wrote {path}")
        return 0
    policy = TriageBook(project).policy
    for r in (*policy.rules, policy.default):
        dl = "/".join(f"{int(v // 60)}m" if v else "-" for v in (r.ack_within_s,
                                                                    r.resolve_within_s))
        match = " ".join(map(str, r.matchers))
        print(f"{r.name:<24} {','.join(r.kinds):<42} {match:<26} {r.severity.value:<9} "
              f"{r.priority.value} {r.owner:<18} ack/resolve {dl}")
    for inh in policy.inhibitions:
        print(f"inhibit {inh.name}: {' '.join(map(str, inh.source))} hides "
              f"{' '.join(map(str, inh.target))} when equal {','.join(inh.equal)}")
    return 0


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    t = sub.add_parser("triage", help="one queue for everything AIR finds wrong: sweep, "
                                      "list, acknowledge, assign, resolve, silence")
    ts = t.add_subparsers(dest="triage_action", required=True)

    def proj(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".")
        return p

    sw = proj(ts.add_parser("sweep", help="collect signals, auto-resolve cleared "
                                          "conditions, escalate overdue items"))
    sw.add_argument("--check", action="append", choices=list(ALL_CHECKS))
    sw.add_argument("--evidence", help="test evidence JSON (default .air/test-evidence.json)")
    sw.add_argument("--source", action="append", help="source roots for requirement claims")
    sw.add_argument("--strict", action="store_true",
                    help="exit 1 while open safety or integrity items exist")
    sw.set_defaults(fn=cmd_sweep)

    ls = proj(ts.add_parser("list", help="open items, most severe first"))
    ls.add_argument("--all", action="store_true", help="include resolved and dismissed")
    ls.add_argument("--suppressed", action="store_true", help="include silenced/inhibited")
    ls.add_argument("--severity", choices=[s.value for s in Severity])
    ls.add_argument("--owner", help="owner role or assignee")
    ls.add_argument("--overdue", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    sh = proj(ts.add_parser("show", help="one item with its history"))
    sh.add_argument("item_id", help="item id or unique prefix")
    sh.set_defaults(fn=cmd_show)

    for name, help_ in (("ack", "acknowledge (stops escalation)"),
                        ("assign", "make someone responsible"),
                        ("start", "mark work started"),
                        ("resolve", "fixed (safety items need --evidence)"),
                        ("dismiss", "not a problem / won't fix (needs --reason)"),
                        ("reopen", "reopen a dismissed item"),
                        ("prioritize", "set and lock the priority (needs --reason)"),
                        ("snooze", "defer until a time or more occurrences "
                                   "(never safety or integrity)")):
        d = proj(ts.add_parser(name, help=help_))
        d.add_argument("item_id")
        if name == "assign":
            d.add_argument("assignee")
        if name == "prioritize":
            d.add_argument("priority", choices=[p.value for p in Priority])
        if name == "snooze":
            d.add_argument("--for", dest="duration", help="e.g. 4h, 2d")
            d.add_argument("--count", type=int, help="wake after this many more occurrences")
        d.add_argument("--by", required=True, help="principal making the decision")
        d.add_argument("--reason", required=name in ("resolve", "dismiss", "reopen",
                                                     "prioritize", "snooze"))
        if name == "resolve":
            d.add_argument("--evidence", action="append",
                           help="evidence reference, e.g. ledger:412 (repeatable)")
        d.set_defaults(fn=cmd_decide)

    si = ts.add_parser("silence", help="temporarily suppress matching items "
                                       "(never safety or integrity)")
    sis = si.add_subparsers(dest="silence_action", required=True)
    sa = proj(sis.add_parser("add"))
    sa.add_argument("--match", action="append", required=True,
                    help="matcher: name=value, name!=value, name=~regex, name!~regex")
    sa.add_argument("--for", dest="duration", required=True, help="e.g. 30m, 2h, 1d")
    sa.add_argument("--by", required=True)
    sa.add_argument("--comment", required=True)
    sa.set_defaults(fn=cmd_silence)
    se = proj(sis.add_parser("end"))
    se.add_argument("silence_id")
    se.add_argument("--by", required=True)
    se.set_defaults(fn=cmd_silence)
    sl = proj(sis.add_parser("list"))
    sl.add_argument("--all", action="store_true")
    sl.set_defaults(fn=cmd_silence)

    ru = proj(ts.add_parser("rules", help="show the routing rules in effect"))
    ru.add_argument("--write", action="store_true",
                    help="write the built-in policy to .air/triage.json to customize it")
    ru.add_argument("--force", action="store_true")
    ru.set_defaults(fn=cmd_rules)
