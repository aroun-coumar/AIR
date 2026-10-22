"""``air experiment``: run, probe, golden runs, search, re-measure, validity, invalidate."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from ..core.errors import SchemaError
from ..core.ids import new_id
from ..provenance.project import Project
from .backends import TimePolicy, available_backends, make_backend
from .evaluation import assess_validity, invalidate, invalidations, record_validity
from .golden import GoldenStore, measure_golden, record_golden_check, remeasure
from .probe import probe_reproducibility, record_probe
from .runner import ExperimentSpec, experiment_records, run_experiment
from .search import (
    Constraint,
    Direction,
    GridSampler,
    Objective,
    Param,
    RandomSampler,
    Sampler,
    Search,
)

__requirements__ = ("REQ02", "REQ04", "F05", "G03", "G04")


def _value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _kv(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SchemaError("expected key=value", item=item)
        out[key] = _value(raw)
    return out


def _seeds(text: str | None) -> list[int]:
    return [int(s) for s in text.split(",") if s.strip()] if text else []


def _tp(args: argparse.Namespace) -> TimePolicy:
    return TimePolicy(physics_dt=args.physics_dt, control_dt=args.control_dt,
                      lockstep=not args.free_running)


def _factory(name: str) -> Any:
    factories = available_backends()
    if name not in factories:
        raise SchemaError("unknown experiment backend", name=name, available=sorted(factories))
    return factories[name]


_COND = re.compile(r"^([A-Za-z_][\w.]*)\s*(<=|>=|<|>)\s*(-?[\d.eE+-]+)$")


def _conditions(items: list[str] | None) -> list[tuple[str, str, float]]:
    out = []
    for item in items or []:
        m = _COND.match(item.strip())
        if not m:
            raise SchemaError("expected metric<=bound (or >=, <, >)", item=item)
        out.append((m.group(1), m.group(2), float(m.group(3))))
    return out


def cmd_backends(_: argparse.Namespace) -> int:
    for name in sorted(available_backends()):
        b = make_backend(name)
        c = b.capabilities
        print(f"{name:20s} {b.name}@{b.version}  determinism={c.determinism.value}  "
              f"restore={c.restore_level.value}  features={','.join(c.features) or '-'}")
        b.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    spec = ExperimentSpec(
        experiment_id=args.id or new_id("exp"), hypothesis=args.hypothesis,
        time_policy=_tp(args), scenario_name=args.scenario_name,
        scenario=_kv(args.scenario), config=_kv(args.config), seed=args.seed,
        episodes=args.episodes, steps=args.steps, features=tuple(args.feature or ()),
        device=args.device,
        acceptance={m: (op, b) for m, op, b in _conditions(args.accept)})
    project = Project.open(args.project) if args.record else None
    backend = make_backend(args.backend)
    try:
        run = run_experiment(backend, spec, project=project)
    finally:
        backend.close()
    r = run.record
    if args.json:
        print(r.to_json(indent=2))
        return 0
    print(f"{r.experiment_id}  {r.backend}@{r.backend_version} on {r.platform}  "
          f"runner {r.runner_version}")
    assert r.seeds is not None
    print(f"  seed {r.seeds.root} ({r.seeds.source.value}); episodes {len(r.seeds.per_episode)}")
    for k, v in sorted(r.results.items()):
        print(f"  {k:14s} {v:.6g}")
    print(f"  fingerprint {r.physics_fingerprint}")
    for lim in r.limitations:
        print(f"  limitation: {lim}")
    print(f"  accepted: {r.accepted}")
    return 0 if r.accepted is not False else 1


def cmd_probe(args: argparse.Namespace) -> int:
    report = probe_reproducibility(_factory(args.backend), _tp(args),
                                   scenario=_kv(args.scenario), config=_kv(args.config),
                                   seed=args.seed, steps=args.steps, tolerance=args.tolerance)
    if args.record:
        record_probe(Project.open(args.project), report)
    print(f"{report.backend}@{report.backend_version} on {report.platform}: "
          f"{report.outcome.value} (declared {report.declared_determinism.value}, "
          f"max diff {report.max_abs_diff:g})")
    print(f"  rng state match {report.rng_state_match}  reset repeatable "
          f"{report.reset_repeatable}  buffer reuse {report.buffer_reuse}  restore "
          f"{report.restore_check}")
    for f in report.findings:
        print(f"  finding: {f}")
    return 0 if report.ok else 1


def cmd_golden(args: argparse.Namespace) -> int:
    store = GoldenStore(args.dir)
    factory = _factory(args.backend)
    if args.golden_action == "record":
        g = measure_golden(factory, args.name, _tp(args), scenario=_kv(args.scenario),
                           config=_kv(args.config), seed=args.seed, steps=args.steps)
        print(f"recorded {store.save(g)}  {g.fingerprint}")
        return 0
    checks = store.check(factory, [args.name] if args.name else None)
    if args.record:
        record_golden_check(Project.open(args.project), checks)
    for c in checks:
        print(f"{c.status.value:15s} {c.name}  " + "; ".join(c.reasons))
    if not checks:
        print("no golden runs recorded for this backend")
    return 0 if checks and all(c.ok for c in checks) else 1


def _param(text: str) -> Param:
    name, _, spec = text.partition("=")
    if ":" in spec:
        lo, _, hi = spec.partition(":")
        is_int = all(re.fullmatch(r"-?\d+", x) for x in (lo, hi))
        return Param(name, kind="int" if is_int else "float", low=float(lo), high=float(hi))
    if "," in spec:
        return Param(name, kind="choice", choices=tuple(_value(c) for c in spec.split(",")))
    raise SchemaError("expected name=low:high or name=a,b,c", param=text)


def cmd_search(args: argparse.Namespace) -> int:
    objectives = []
    for o in args.objective:
        metric, _, d = o.partition(":")
        objectives.append(Objective(metric, Direction.MAXIMIZE if d.startswith("max")
                                    else Direction.MINIMIZE))
    constraints = []
    for m, op, b in _conditions(args.constraint):
        if op not in ("<=", ">="):
            raise SchemaError("constraints use <= or >=", constraint=m)
        constraints.append(Constraint(m, "<=" if op == "<=" else ">=", b))
    sampler: Sampler
    if args.sampler == "grid":
        sampler = GridSampler()
    elif args.sampler == "optuna":
        from .optuna_adapter import OptunaSampler
        sampler = OptunaSampler(directions=[o.direction for o in objectives],
                                seed=args.sampler_seed)
    else:
        sampler = RandomSampler(args.sampler_seed)
    project = Project.open(args.project) if args.record else None
    search = Search(args.id or new_id("srch"), factory=_factory(args.backend),
                    time_policy=_tp(args), space=[_param(p) for p in args.param],
                    objectives=objectives, constraints=constraints,
                    scenarios={args.scenario_name: _kv(args.scenario)},
                    seeds=_seeds(args.seeds), sampler=sampler, steps=args.steps,
                    max_trials=args.max_trials, project=project)
    record = search.run(args.candidates)
    counts: dict[str, int] = {}
    for t in record.trials:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    print(f"{record.search_id}: {record.status} {record.status_reason}  "
          f"{len(record.candidates)} candidates, trials {counts}")
    by_id = {c.candidate: c for c in record.candidates}
    for cid in record.pareto:
        c = by_id[cid]
        print(f"  pareto {cid} {json.dumps(c.config)} " +
              " ".join(f"{k}={v:.4g}" for k, v in c.values.items()))
    held = _seeds(args.held_out)
    if held:
        sel = search.select_release(record, held)
        print(f"release: {sel.rationale}")
        for cid in sel.selected:
            print(f"  selected {cid} {json.dumps(by_id[cid].config)}")
        for cid, why in sel.rejected.items():
            print(f"  rejected {cid}: {why}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = Project.open(args.project)
    bad = invalidations(project)
    for r in sorted(experiment_records(project), key=lambda r: r.experiment_id):
        flag = "INVALID " if r.experiment_id in bad else ""
        status = r.status or {True: "accepted", False: "rejected", None: "inconclusive"}[
            r.accepted]
        print(f"{flag}{r.experiment_id}  {status:10s} {r.backend}@{r.backend_version} "
              f"{r.platform}  {r.status_reason}")
    return 0


def cmd_remeasure(args: argparse.Namespace) -> int:
    project = Project.open(args.project)
    record = next((r for r in experiment_records(project)
                   if r.experiment_id == args.experiment_id), None)
    if record is None:
        raise SchemaError("no such experiment record", experiment_id=args.experiment_id)
    result = remeasure(_factory(args.backend or record.backend), record, project=project)
    print(f"{record.experiment_id}: comparable {result.comparable}, fingerprint match "
          f"{result.fingerprint_match}  " + "; ".join(result.reasons))
    for k, v in sorted(result.measured.items()):
        print(f"  {k:14s} stored {result.stored.get(k)!s:>22}  measured {v}")
    return 0 if result.fingerprint_match else 1


def cmd_invalidate(args: argparse.Namespace) -> int:
    seq = invalidate(Project.open(args.project), args.subject, by=args.by,
                     reason=args.reason, evidence=args.evidence or ())
    print(f"{args.subject} marked invalid (ledger seq {seq}); nothing was deleted")
    return 0


def cmd_validity(args: argparse.Namespace) -> int:
    v = assess_validity(args.benchmark, {k: float(x) for k, x in _kv(args.sim).items()},
                        {k: float(x) for k, x in _kv(args.real).items()},
                        max_mmrv=args.max_mmrv, min_pearson=args.min_pearson,
                        min_policies=args.min_policies, backend=args.backend or "",
                        backend_version=args.backend_version or "",
                        real_evidence=args.evidence or ())
    if args.record:
        record_validity(Project.open(args.project), v)
    print(f"{v.benchmark}: {'VALID' if v.valid else 'NOT VALID'}  MMRV {v.mmrv:.4f}  "
          f"Pearson {v.pearson if v.pearson is None else round(v.pearson, 4)}")
    for r in v.reasons:
        print(f"  {r}")
    return 0 if v.valid else 1


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    e = sub.add_parser("experiment", help="experiments, reproducibility, golden runs, "
                                          "search and evaluation validity")
    es = e.add_subparsers(dest="experiment_action", required=True)

    def proj(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".", help="project directory (default: .)")
        return p

    def sim(p: argparse.ArgumentParser, record: bool = True) -> argparse.ArgumentParser:
        p.add_argument("--backend", default="pointmass")
        p.add_argument("--physics-dt", type=float, default=0.01)
        p.add_argument("--control-dt", type=float, default=0.05)
        p.add_argument("--free-running", action="store_true",
                       help="not lockstep (recorded as non-reproducible)")
        p.add_argument("--scenario", action="append", metavar="KEY=VALUE")
        p.add_argument("--config", action="append", metavar="KEY=VALUE")
        p.add_argument("--seed", type=int)
        p.add_argument("--steps", type=int, default=100)
        if record:
            p.add_argument("--record", action="store_true",
                           help="record into the project ledger")
        return proj(p)

    es.add_parser("backends", help="list experiment backends and declared capabilities"
                  ).set_defaults(fn=cmd_backends)

    r = sim(es.add_parser("run", help="run an experiment and print its record"))
    r.add_argument("--id")
    r.add_argument("--hypothesis", default="ad-hoc experiment")
    r.add_argument("--scenario-name", default="default")
    r.add_argument("--episodes", type=int, default=1)
    r.add_argument("--feature", action="append")
    r.add_argument("--device", default="cpu")
    r.add_argument("--accept", action="append", metavar="METRIC<=BOUND")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_run)

    p = sim(es.add_parser("probe", help="measure reproducibility (exact / within "
                                        "tolerance / differs)"))
    p.add_argument("--tolerance", type=float, default=1e-9)
    p.set_defaults(fn=cmd_probe, seed=0, steps=50)

    g = es.add_parser("golden", help="per-platform golden runs")
    gs = g.add_subparsers(dest="golden_action", required=True)
    for action in ("record", "check"):
        ga = sim(gs.add_parser(action))
        ga.add_argument("--dir", default="golden")
        ga.add_argument("--name", required=action == "record")
        ga.set_defaults(fn=cmd_golden, seed=0)

    s = sim(es.add_parser("search", help="constrained search; trials go to the ledger"))
    s.add_argument("--id")
    s.add_argument("--param", action="append", required=True,
                   metavar="NAME=LOW:HIGH|NAME=A,B")
    s.add_argument("--objective", action="append", required=True, metavar="METRIC[:min|max]")
    s.add_argument("--constraint", action="append", metavar="METRIC<=BOUND")
    s.add_argument("--seeds", default="1,2,3")
    s.add_argument("--held-out", help="held-out seeds for release selection")
    s.add_argument("--candidates", type=int, default=10)
    s.add_argument("--max-trials", type=int)
    s.add_argument("--sampler", choices=["random", "grid", "optuna"], default="random")
    s.add_argument("--sampler-seed", type=int, default=0)
    s.add_argument("--scenario-name", default="default")
    s.set_defaults(fn=cmd_search)

    proj(es.add_parser("list", help="experiment records in the project")).set_defaults(
        fn=cmd_list)

    rm = proj(es.add_parser("remeasure", help="re-run a stored experiment; never trust "
                                               "stored scores"))
    rm.add_argument("experiment_id")
    rm.add_argument("--backend")
    rm.set_defaults(fn=cmd_remeasure)

    iv = proj(es.add_parser("invalidate", help="mark a result invalid (with provenance)"))
    iv.add_argument("subject", help="experiment id, validity id or benchmark name")
    iv.add_argument("--by", required=True)
    iv.add_argument("--reason", required=True)
    iv.add_argument("--evidence", action="append")
    iv.set_defaults(fn=cmd_invalidate)

    v = proj(es.add_parser("validity", help="sim-to-real validity of a benchmark"))
    v.add_argument("--benchmark", required=True)
    v.add_argument("--sim", action="append", required=True, metavar="POLICY=SCORE")
    v.add_argument("--real", action="append", required=True, metavar="POLICY=SCORE")
    v.add_argument("--evidence", action="append", help="reference to real-robot results")
    v.add_argument("--max-mmrv", type=float, default=0.1)
    v.add_argument("--min-pearson", type=float, default=0.7)
    v.add_argument("--min-policies", type=int, default=3)
    v.add_argument("--backend")
    v.add_argument("--backend-version")
    v.add_argument("--record", action="store_true")
    v.set_defaults(fn=cmd_validity)
