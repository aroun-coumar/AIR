"""Physics fingerprints and golden runs (W3.3; Microduck arena ``golden.py``).

A golden run is one fixed, seeded episode measured on one *platform*, with one pinned
simulator version and one runner version. It is re-measured, not trusted: a check runs
the episode again and compares the fingerprint. Goldens and results are compared only
within the same platform, backend version and runner version; across them, the answer is
"not comparable", never a number. :func:`remeasure` re-runs a stored experiment from its
record instead of trusting its stored scores (``arena recover``).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, _load, contract
from ..provenance.model import ActivityKind, ExperimentRecord
from ..provenance.project import Project
from .backends import RUNNER_VERSION, BackendFactory, BackendInfo, TimePolicy, platform_key
from .runner import ExperimentSpec, physics_fingerprint, run_episode, run_experiment

__requirements__ = ("REQ02", "G03", "F05")


@contract("golden_run", version="1.0")
class GoldenRun(Contract):
    name: str
    platform: str
    backend: str
    backend_version: str
    runner_version: str
    time_policy: TimePolicy
    seed: int
    steps: int
    fingerprint: str
    scenario: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    recorded_at: float = 0.0


class GoldenStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"
    MISSING = "missing"


@dataclass(frozen=True)
class GoldenCheck:
    name: str
    status: GoldenStatus
    reasons: tuple[str, ...] = ()
    measured: str = ""
    expected: str = ""
    metric_deltas: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == GoldenStatus.MATCH


def comparability(a: Mapping[str, str], b: Mapping[str, str]) -> tuple[str, ...]:
    """Reasons ``a`` and ``b`` (dicts with platform, backend, backend_version,
    runner_version) must not be compared; empty means comparable."""
    return tuple(f"{k} differs: {a.get(k)!r} vs {b.get(k)!r}"
                 for k in ("platform", "backend", "backend_version", "runner_version")
                 if a.get(k) != b.get(k))


def _key(record: ExperimentRecord) -> dict[str, str]:
    return {"platform": record.platform, "backend": record.backend,
            "backend_version": record.backend_version, "runner_version": record.runner_version}


class NotComparable(SchemaError):
    code = "not_comparable"


def compare_results(a: ExperimentRecord, b: ExperimentRecord) -> dict[str, float]:
    """Metric deltas ``b - a``; raises :class:`NotComparable` across platforms, simulator
    versions or runner versions."""
    reasons = comparability(_key(a), _key(b))
    if reasons:
        raise NotComparable("results are not comparable", reasons=list(reasons))
    return {k: float(b.results[k]) - float(a.results[k]) for k in a.results
            if k in b.results and isinstance(a.results[k], (int, float))
            and isinstance(b.results[k], (int, float))}


def measure_golden(factory: BackendFactory, name: str, time_policy: TimePolicy, *,
                   scenario: Mapping[str, Any] | None = None,
                   config: Mapping[str, Any] | None = None, seed: int = 0,
                   steps: int = 100) -> GoldenRun:
    backend = factory()
    try:
        info = backend.load(dict(scenario or {}), time_policy)
        traj, metrics, _ = run_episode(backend, dict(config or {}), seed, steps)
    finally:
        backend.close()
    return GoldenRun(name=name, platform=info.platform, backend=info.backend,
                     backend_version=info.version, runner_version=RUNNER_VERSION,
                     time_policy=time_policy, seed=seed, steps=steps,
                     fingerprint=physics_fingerprint(traj), scenario=dict(scenario or {}),
                     config=dict(config or {}), metrics=metrics, recorded_at=time.time())


def check_golden(factory: BackendFactory, golden: GoldenRun) -> GoldenCheck:
    """Re-measure ``golden`` here and compare, only if this is the same platform,
    backend version and runner version."""
    probe = factory()
    here = {"platform": platform_key(), "backend": probe.name,
            "backend_version": probe.version, "runner_version": RUNNER_VERSION}
    probe.close()
    reasons = comparability(here, {"platform": golden.platform, "backend": golden.backend,
                                   "backend_version": golden.backend_version,
                                   "runner_version": golden.runner_version})
    if reasons:
        return GoldenCheck(golden.name, GoldenStatus.NOT_COMPARABLE, reasons,
                           expected=golden.fingerprint)
    fresh = measure_golden(factory, golden.name, golden.time_policy, scenario=golden.scenario,
                           config=golden.config, seed=golden.seed, steps=golden.steps)
    deltas = {k: fresh.metrics[k] - v for k, v in golden.metrics.items() if k in fresh.metrics}
    status = GoldenStatus.MATCH if fresh.fingerprint == golden.fingerprint \
        else GoldenStatus.MISMATCH
    return GoldenCheck(golden.name, status,
                       () if status == GoldenStatus.MATCH else ("fingerprint differs",),
                       measured=fresh.fingerprint, expected=golden.fingerprint,
                       metric_deltas=deltas)


class GoldenStore:
    """Golden runs as files: ``<root>/<platform>/<backend>@<version>/<name>.json``.
    Commit them; each platform has its own."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, g: GoldenRun) -> Path:
        return self.root / g.platform / f"{g.backend}@{g.backend_version}" / f"{g.name}.json"

    def save(self, golden: GoldenRun) -> Path:
        p = self.path(golden)
        p.parent.mkdir(parents=True, exist_ok=True)
        golden.save(p)
        return p

    def all(self) -> list[GoldenRun]:
        if not self.root.exists():
            return []
        return [GoldenRun.load(p) for p in sorted(self.root.glob("*/*/*.json"))]

    def find(self, name: str, platform: str, backend: str, version: str) -> GoldenRun | None:
        p = self.root / platform / f"{backend}@{version}" / f"{name}.json"
        return GoldenRun.load(p) if p.exists() else None

    def check(self, factory: BackendFactory, names: list[str] | None = None
              ) -> list[GoldenCheck]:
        """Check every golden recorded for *this* platform and this backend version;
        a name with goldens only elsewhere is ``missing`` here, never cross-compared."""
        probe = factory()
        backend, version = probe.name, probe.version
        probe.close()
        here = platform_key()
        wanted = names or sorted({g.name for g in self.all() if g.backend == backend})
        out = []
        for name in wanted:
            g = self.find(name, here, backend, version)
            if g is None:
                out.append(GoldenCheck(name, GoldenStatus.MISSING, (
                    f"no golden for platform {here!r} and {backend}@{version}",)))
            else:
                out.append(check_golden(factory, g))
        return out


def record_golden_check(project: Project, checks: list[GoldenCheck]) -> str:
    """Record a golden-run check as an evaluation activity."""
    with project.activity(ActivityKind.EVALUATION, "golden-run check",
                          requirements=("G03",),
                          params={"platform": platform_key(),
                                  "runner_version": RUNNER_VERSION}) as act:
        for c in checks:
            act.metric(c.name, c.status.value)
        bad = [c.name for c in checks if c.status in (GoldenStatus.MISMATCH,
                                                      GoldenStatus.MISSING)]
        if bad:
            act.fail("golden runs not reproduced: " + ", ".join(bad))
        act.outcome = "reproduced" if not bad else "not reproduced"
    return act.activity_id


def spec_from_record(record: ExperimentRecord) -> ExperimentSpec:
    """Rebuild the spec an ExperimentRecord (1.1) was produced from."""
    if not record.backend_info:
        raise SchemaError("record has no backend_info; cannot re-measure",
                          experiment_id=record.experiment_id)
    info = BackendInfo.from_dict(record.backend_info)
    if info.time_policy is None:
        raise SchemaError("record has no time policy", experiment_id=record.experiment_id)
    seeds = record.seeds
    episodes = int(record.numerical_settings.get("episodes", 1))
    acceptance = {k: (str(v[0]), float(v[1])) for k, v in record.acceptance_criteria.items()}
    return ExperimentSpec(
        experiment_id=record.experiment_id, hypothesis=record.hypothesis,
        time_policy=_load(TimePolicy, record.backend_info["time_policy"], "time_policy"),
        scenario_name=record.scenario, scenario=dict(record.scenario_params),
        config=dict(record.config), seed=seeds.root if seeds else record.seed,
        episodes=episodes, steps=int(record.numerical_settings.get("steps", 100)),
        features=info.features, device=info.requested_device,
        requirement_ids=record.requirement_ids, acceptance=acceptance,
        budget=dict(record.budget), candidate=record.candidate)


@dataclass(frozen=True)
class Remeasurement:
    experiment_id: str
    comparable: bool
    fingerprint_match: bool
    reasons: tuple[str, ...]
    stored: dict[str, Any]
    measured: dict[str, Any]


def remeasure(factory: BackendFactory, record: ExperimentRecord, *,
              project: Project | None = None) -> Remeasurement:
    """Re-run ``record`` from its config and seeds and compare with what was stored."""
    backend = factory()
    try:
        fresh = run_experiment(backend, spec_from_record(record)).record
    finally:
        backend.close()
    reasons = comparability(_key(record), _key(fresh))
    match = not reasons and fresh.physics_fingerprint == record.physics_fingerprint
    result = Remeasurement(record.experiment_id, not reasons, match,
                           reasons if reasons else (() if match else ("fingerprint differs",)),
                           dict(record.results), dict(fresh.results))
    if project is not None:
        with project.activity(ActivityKind.EVALUATION, f"re-measure {record.experiment_id}",
                              requirements=("F05",),
                              params={"experiment_id": record.experiment_id,
                                      "platform": fresh.platform}) as act:
            for k, v in fresh.results.items():
                act.metric(k, v)
            act.metric("fingerprint_match", match)
            act.outcome = "reproduced" if match else (
                "not comparable" if reasons else "not reproduced")
    return result
