"""``air prognose``: estimate time-to-event from a degradation series; list estimates."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from ..core.errors import SchemaError
from .prognostics import LinearDegradationEstimator, estimates_from_ledger, record_estimate

__requirements__ = ("REQ16", "REQ18")


def _duration(text: str) -> float:
    from ..triage.cli import parse_duration
    return parse_duration(text)


def load_series(path: str) -> list[tuple[float, float]]:
    """CSV ``t,x`` rows (a header row is skipped) or JSON ``[[t, x], ...]``."""
    p = Path(path)
    text = p.read_text("utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        return [(float(t), float(x)) for t, x in data]
    out = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 2:
            continue
        try:
            out.append((float(row[0]), float(row[1])))
        except ValueError:
            if out:
                raise SchemaError("bad series row", row=row) from None
    return out


def _project(args: argparse.Namespace) -> Any:
    from ..provenance.project import Project
    return Project.open(args.project)


def cmd_estimate(args: argparse.Namespace) -> int:
    est = LinearDegradationEstimator(threshold=args.threshold, direction=args.direction,
                                     horizon_s=_duration(args.horizon), n_boot=args.n_boot,
                                     seed=args.seed)
    e = est.estimate(load_series(args.series), robot_id=args.robot,
                     component_id=args.component, signal=args.signal, now=args.now)
    if args.record:
        record_estimate(_project(args), e)
    if args.json:
        print(e.to_json(indent=2))
    else:
        q = e.time_of_event
        print(f"{e.robot_id}/{e.component_id}: {e.event}")
        print(f"  event state {e.event_state.mean:.3f} "
              f"[{e.event_state.low:.3f}, {e.event_state.high:.3f}]")
        for name, v in q.quantiles.items():
            bound = " (lower bound: beyond horizon)" if name in q.censored_quantiles else ""
            print(f"  {name:<4} time to event {v / 3600:.4g} h{bound}")
        print(f"  censored {q.censored_fraction:.0%} of {q.samples} samples; horizon "
              f"{e.horizon_s / 3600:.4g} h; loading: {e.loading.description}")
    if args.mission:
        status, why = e.gate_status(_duration(args.mission))
        print(f"  gate for a {args.mission} mission: {status.value}" + (f" ({why})" if why
                                                                         else ""))
        return 0 if status.value == "available" else 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = _project(args)
    ests = estimates_from_ledger(project)
    mission = _duration(args.mission) if args.mission else None
    for e in ests:
        p5, lb = e.quantile("p5")
        gate = ""
        if mission is not None:
            status, why = e.gate_status(mission)
            gate = f"  {status.value}" + (f" ({why})" if why else "")
        print(f"{e.robot_id}/{e.component_id}  {e.event}  p5 {'>=' if lb else ''}"
              f"{p5 / 3600:.4g} h{gate}")
    if args.triage:
        from ..triage.book import TriageBook, load_policy
        from ..triage.operations import OperationsTriage, operations_policy
        sink = OperationsTriage(TriageBook(project, policy=operations_policy(
            load_policy(project))))
        results = sink.prognostics(ests, lead_s=_duration(args.lead))
        print(f"triage: {len(results)} signal(s)", file=sys.stderr)
    print(f"{len(ests)} estimate(s)", file=sys.stderr)
    return 0


def add_commands(sub: Any) -> None:
    p = sub.add_parser("prognose", help="prognostics: time-to-event with uncertainty, "
                                        "gated on p5")
    ps = p.add_subparsers(dest="prognose_command", required=True)

    e = ps.add_parser("estimate", help="fit a degradation series (linear trend, bootstrap)")
    e.add_argument("--series", required=True, help="CSV t,x or JSON [[t, x], ...] (t in s)")
    e.add_argument("--threshold", type=float, required=True)
    e.add_argument("--direction", choices=["below", "above"], default="below")
    e.add_argument("--robot", required=True)
    e.add_argument("--component", required=True)
    e.add_argument("--signal", required=True)
    e.add_argument("--horizon", default="30d", help="prediction horizon, e.g. 30d")
    e.add_argument("--mission", help="mission length to gate on, e.g. 8h (exit 1 unless "
                                     "available)")
    e.add_argument("--now", type=float, help="estimate time (default: last sample)")
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--n-boot", type=int, default=400)
    e.add_argument("--json", action="store_true")
    e.add_argument("--record", action="store_true", help="append to the project ledger")
    e.add_argument("--project", default=".")
    e.set_defaults(fn=cmd_estimate)

    ls = ps.add_parser("list", help="latest recorded estimates")
    ls.add_argument("--project", default=".")
    ls.add_argument("--mission", help="show the gate for this mission length")
    ls.add_argument("--triage", action="store_true",
                    help="feed estimates whose p5 falls inside --lead to triage")
    ls.add_argument("--lead", default="7d")
    ls.set_defaults(fn=cmd_list)
