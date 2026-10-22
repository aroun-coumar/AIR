"""``air health``: derive expected items, aggregate readings, readiness, PSI, diagnosis.

Also registers ``air incident`` and ``air prognose`` (stage 5 operations, ADR 0015).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..contracts.manifest import ComponentManifest
from ..core.errors import SchemaError
from ..core.schema import load_any
from .aggregate import HealthAggregator
from .diagnosis import DiagnosisTable, builtin_table, exercise
from .model import HealthLevel, HealthManifest, HealthReport, HealthStatus, derive_manifest
from .probes import PsiProbe
from .readiness import HealthCheck, ManifestCheck, ReadinessContext, ReadinessGate

__requirements__ = ("REQ13", "REQ14", "REQ18")


def _catalog(project: str) -> Any:
    from ..hardware.catalog import project_catalog
    return project_catalog(project)


def _load_manifest(path: str, project: str, timeout_s: float
                   ) -> tuple[HealthManifest, ComponentManifest | None]:
    doc = load_any(json.loads(Path(path).read_text("utf-8")))
    if isinstance(doc, HealthManifest):
        return doc, None
    if isinstance(doc, ComponentManifest):
        return derive_manifest(doc, _catalog(project), timeout_s=timeout_s), doc
    raise SchemaError("expected a health_manifest or component_manifest", schema=doc.schema_id())


def aggregate_readings(hm: HealthManifest, readings: dict[str, Any], now: float
                       ) -> HealthReport:
    """``{name: number | {"value", "unit", "at"} | {"level", "message", "at"}}``; a
    ``"lost"`` key lists items or components whose transport reported them gone."""
    agg = HealthAggregator(hm)
    for name, raw in readings.items():
        if name == "lost":
            for target in raw:
                agg.lost(str(target), at=now)
            continue
        if isinstance(raw, dict) and "level" in raw:
            agg.report_status(HealthStatus(name, HealthLevel(raw["level"]),
                                           str(raw.get("message", "")),
                                           observed_at=float(raw.get("at", now)),
                                           source="cli"))
        elif isinstance(raw, dict):
            agg.observe(name, float(raw["value"]), at=float(raw.get("at", now)),
                        unit=raw.get("unit"), source="cli")
        else:
            agg.observe(name, float(raw), at=now, source="cli")
    return agg.report(now)


def _readings(args: argparse.Namespace) -> dict[str, Any]:
    if not args.readings:
        return {}
    data = json.loads(Path(args.readings).read_text("utf-8"))
    if not isinstance(data, dict):
        raise SchemaError("readings must be a JSON object")
    return data


def cmd_derive(args: argparse.Namespace) -> int:
    cm = ComponentManifest.load(args.manifest)
    hm = derive_manifest(cm, _catalog(args.project), timeout_s=args.timeout,
                         robot_owned_recovery=args.recovery or ())
    text = hm.to_json(indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {len(hm.expected)} expected item(s), {len(hm.gates)} gate(s) to "
              f"{args.out}")
    else:
        print(text)
    return 0


def _project(args: argparse.Namespace) -> Any:
    from ..provenance.project import Project
    return Project.open(args.project)


def _triage(project: Any) -> Any:
    from ..triage.book import TriageBook, load_policy
    from ..triage.operations import OperationsTriage, operations_policy
    return OperationsTriage(TriageBook(project, policy=operations_policy(load_policy(project))))


def cmd_check(args: argparse.Namespace) -> int:
    hm, _ = _load_manifest(args.manifest, args.project, args.timeout)
    now = args.now if args.now is not None else time.time()
    report = aggregate_readings(hm, _readings(args), now)
    print(report.to_json(indent=2) if args.json else report.render())
    if args.record or args.triage:
        project = _project(args)
        if args.record:
            project.ledger.append("health.report", report.robot_id,
                                  {"level": report.level.value, "report": report.to_dict()})
        if args.triage:
            results = _triage(project).health(report)
            print(f"triage: {len(results)} signal(s)", file=sys.stderr)
    return 1 if report.level.bad else 0


def cmd_ready(args: argparse.Namespace) -> int:
    hm, cm = _load_manifest(args.manifest, args.project, args.timeout)
    now = args.now if args.now is not None else time.time()
    report = aggregate_readings(hm, _readings(args), now)
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    checks: list[Any] = [HealthCheck()]
    if cm is not None:
        checks.append(ManifestCheck(cm, _catalog(args.project)))
    gate = ReadinessGate(checks, modes=modes)
    ctx = ReadinessContext(hm.robot_id, now, health=report)
    ready = gate.evaluate(ctx)
    if args.json:
        print(ready.to_json(indent=2))
    else:
        for mode in ready.modes:
            print(f"{mode:<16} {'READY' if ready.ready(mode) else 'BLOCKED'}")
        for r in ready.results:
            print(f"  {r.check_id}: {r.level.value} {r.subject} {r.message}"
                  + (f"  blocks {','.join(r.blocks)}" if r.blocks else ""))
        for flag in ready.failsafe:
            print(f"  failsafe {flag}")
    if args.record or args.triage:
        project = _project(args)
        if args.record:
            project.ledger.append("readiness.evaluated", hm.robot_id,
                                  {"mode": args.mode, "ready": ready.ready(args.mode),
                                   "report": ready.to_dict()})
        if args.triage:
            _triage(project).readiness(ready)
    return 0 if ready.ready(args.mode) else 1


def cmd_psi(args: argparse.Namespace) -> int:
    probe = PsiProbe(Path(args.root)) if args.root else PsiProbe()
    statuses = probe.statuses(time.time())
    if args.json:
        print(json.dumps([{"name": s.name, "level": s.level.value, "message": s.message,
                           "values": s.values} for s in statuses], indent=2))
    else:
        for s in statuses:
            vals = " ".join(f"{k}={v:g}" for k, v in s.values.items())
            print(f"{s.name:<20} {s.level.value:<6} {vals} {s.message}")
    return 0


def cmd_diagnosis(args: argparse.Namespace) -> int:
    table = DiagnosisTable.load(args.table) if args.table else builtin_table()
    failed = 0
    for e in table.entries:
        line = (f"{e.fault_id:<28} threshold {e.threshold:<3} {e.severity.value:<8} "
                f"delay {e.delay_s:<5g} {e.failure_class or '-':<26} {e.description}")
        if args.exercise:
            problems = exercise(e)
            failed += bool(problems)
            line = ("FAIL " if problems else "ok   ") + line
            line += "".join(f"\n       {p}" for p in problems)
        print(line)
    return 1 if failed else 0


def add_commands(sub: Any) -> None:
    h = sub.add_parser("health", help="health: expected items, aggregation, readiness, PSI, "
                                      "diagnosis table")
    hs = h.add_subparsers(dest="health_command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".", help="project root (catalog, ledger)")
        p.add_argument("--timeout", type=float, default=5.0,
                       help="seconds without a report before an item is STALE")
        return p

    d = common(hs.add_parser("derive", help="derive the expected-item manifest from a "
                                            "ComponentManifest and the hardware catalog"))
    d.add_argument("manifest")
    d.add_argument("--recovery", action="append",
                   help="skill id of recovery the robot owns (never commanded by AIR)")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_derive)

    for name, fn, help_ in (("check", cmd_check, "aggregate readings into a health report "
                                                  "(exit 1 on ERROR or STALE)"),
                            ("ready", cmd_ready, "readiness gate before activation "
                                                  "(exit 1 if the mode is blocked)")):
        c = common(hs.add_parser(name, help=help_))
        c.add_argument("manifest", help="health_manifest or component_manifest JSON")
        c.add_argument("--readings", help='JSON {"<component>.<signal>": value | '
                                          '{"value", "unit", "at"} | {"level", "message"}, '
                                          '"lost": [...]}')
        c.add_argument("--now", type=float, help="evaluation time (default: now)")
        c.add_argument("--json", action="store_true")
        c.add_argument("--record", action="store_true", help="append to the project ledger")
        c.add_argument("--triage", action="store_true", help="feed problems to triage")
        if name == "ready":
            c.add_argument("--mode", required=True)
            c.add_argument("--modes", default="manual,autonomous,maintenance")
        c.set_defaults(fn=fn)

    p = hs.add_parser("psi", help="Linux pressure stall information (unsupported elsewhere)")
    p.add_argument("--root", help="directory with cpu/memory/io (default /proc/pressure)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_psi)

    t = hs.add_parser("diagnosis", help="the diagnosis table (fault id, debounce, severity, "
                                        "delay)")
    t.add_argument("--table", help="diagnosis_table JSON (default: built-in)")
    t.add_argument("--exercise", action="store_true",
                   help="drive every entry through its life (exit 1 on a mismatch)")
    t.set_defaults(fn=cmd_diagnosis)

    from ..incidents.cli import add_commands as add_incident_commands
    from ..maintenance.cli import add_commands as add_prognose_commands
    add_incident_commands(sub)
    add_prognose_commands(sub)
