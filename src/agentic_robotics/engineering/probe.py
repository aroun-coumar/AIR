"""Reproducibility probe: determinism is *measured*, never taken from a declaration or a
seed alone (W3.2; Gymnasium ``check_env``).

The probe runs the same seeded episode on two fresh backends and again on the first one,
and reports one of three outcomes: ``exact`` (bit-identical), ``within_tolerance`` or
``differs``. It also checks that the RNG state after ``reset(seed)`` matches, that the
backend does not reuse observation buffers (a reused buffer silently rewrites history),
and, when snapshots are declared, that restore actually reproduces the continuation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import field
from enum import StrEnum
from typing import Any

from ..core.schema import Contract, contract
from ..provenance.model import ActivityKind
from ..provenance.project import Project
from .backends import BackendFactory, DeterminismClass, ExperimentBackend, TimePolicy
from .runner import TraceLike

__requirements__ = ("F05", "G04", "REQ04")


class Reproducibility(StrEnum):
    EXACT = "exact"
    WITHIN_TOLERANCE = "within_tolerance"
    DIFFERS = "differs"


@contract("reproducibility_report", version="1.0")
class ReproducibilityReport(Contract):
    backend: str
    backend_version: str
    platform: str
    seed: int
    steps: int
    tolerance: float
    outcome: Reproducibility
    max_abs_diff: float = 0.0
    declared_determinism: DeterminismClass = DeterminismClass.UNKNOWN
    rng_state_match: bool | None = None
    reset_repeatable: bool = False
    buffer_reuse: bool = False
    restore_check: str = "not_supported"
    findings: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (self.outcome != Reproducibility.DIFFERS and not self.buffer_reuse
                and self.rng_state_match is not False and self.reset_repeatable
                and self.restore_check in ("match", "not_supported"))


def _diff(a: Any, b: Any) -> float:
    if isinstance(a, bool) or isinstance(b, bool):
        return 0.0 if a == b else math.inf
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) and math.isnan(b):
            return 0.0
        d = abs(float(a) - float(b))
        return d if not math.isnan(d) else math.inf
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if set(a) != set(b):
            return math.inf
        return max((_diff(a[k], b[k]) for k in a), default=0.0)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return math.inf
        return max((_diff(x, y) for x, y in zip(a, b, strict=True)), default=0.0)
    return 0.0 if a == b else math.inf


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _episode(backend: ExperimentBackend, seed: int, steps: int,
             config: Mapping[str, Any]) -> tuple[list[Any], list[Any], Any]:
    """Returns (copies taken immediately, the raw returned objects, RNG state after reset)."""
    first = backend.reset(seed)
    state = backend.rng_state() if backend.capabilities.supports_rng_state else None
    raw = [first]
    copies = [json.loads(json.dumps(first))]
    for _ in range(steps):
        obs = backend.step(config)
        raw.append(obs)
        copies.append(json.loads(json.dumps(obs)))
    return copies, raw, state


def _strip(obs: list[Any], lossy: tuple[str, ...]) -> list[Any]:
    return [{k: v for k, v in o.items() if k not in lossy} if isinstance(o, Mapping) else o
            for o in obs]


def probe_reproducibility(factory: BackendFactory, time_policy: TimePolicy, *,
                          scenario: Mapping[str, Any] | None = None,
                          config: Mapping[str, Any] | None = None, seed: int = 0,
                          steps: int = 50, tolerance: float = 1e-9,
                          features: tuple[str, ...] = (),
                          trace: TraceLike | None = None) -> ReproducibilityReport:
    scenario = dict(scenario or {})
    config = dict(config or {})
    findings: list[str] = []
    a, b = factory(), factory()
    try:
        info = a.load(scenario, time_policy, features=features)
        b.load(scenario, time_policy, features=features)
        caps = a.capabilities
        run1, raw1, state1 = _episode(a, seed, steps, config)
        run2, _, state2 = _episode(b, seed, steps, config)
        # buffer reuse: the objects the backend handed out must not have changed later,
        # and no two steps may return the very same object
        reused = any(not _same(r, c) for r, c in zip(raw1, run1, strict=True)) or \
            len({id(r) for r in raw1}) < len(raw1)
        if reused:
            findings.append("backend reuses observation buffers")
        run3, _, state3 = _episode(a, seed, steps, config)
        reset_ok = _same(run1, run3)
        if not reset_ok:
            findings.append("reset(seed) on the same instance does not repeat the episode")
        rng_match: bool | None = None
        if caps.supports_rng_state:
            rng_match = _same(state1, state2) and _same(state1, state3)
            if not rng_match:
                findings.append("RNG state after reset(seed) differs between runs")
        if not caps.seeded_rngs:
            findings.append("backend declares no seeded RNG")
        restore = "not_supported"
        if caps.supports_snapshot and steps >= 2:
            k = steps // 2
            a.reset(seed)
            for _ in range(k):
                a.step(config)
            snap = a.snapshot()
            tail1 = [json.loads(json.dumps(a.step(config))) for _ in range(steps - k)]
            a.restore(snap)
            tail2 = [json.loads(json.dumps(a.step(config))) for _ in range(steps - k)]
            lossy = tuple(snap.lossy) + tuple(caps.restore_lossy)
            restore = "match" if _same(_strip(tail1, lossy), _strip(tail2, lossy)) \
                else "differs"
            if restore == "differs":
                findings.append(f"restore at {snap.level.value} level does not reproduce "
                                "the continuation")
        if _same(run1, run2) and reset_ok:
            outcome, diff = Reproducibility.EXACT, 0.0
        else:
            diff = max(_diff(run1, run2), _diff(run1, run3))
            outcome = (Reproducibility.WITHIN_TOLERANCE if diff <= tolerance
                       else Reproducibility.DIFFERS)
        if (outcome != Reproducibility.EXACT and caps.determinism in (
                DeterminismClass.BITWISE, DeterminismClass.BITWISE_SAME_HOST)):
            findings.append(f"declared {caps.determinism.value} but measured {outcome.value}")
        report = ReproducibilityReport(
            backend=info.backend, backend_version=info.version, platform=info.platform,
            seed=seed, steps=steps, tolerance=tolerance, outcome=outcome,
            max_abs_diff=diff if math.isfinite(diff) else -1.0,
            declared_determinism=caps.determinism, rng_state_match=rng_match,
            reset_repeatable=reset_ok, buffer_reuse=reused, restore_check=restore,
            findings=tuple(findings), config=config)
    finally:
        a.close()
        b.close()
    if trace is not None:
        trace.record("reproducibility_probe", report.backend, outcome=report.outcome.value,
                     ok=report.ok, findings=list(report.findings))
    return report


def record_probe(project: Project, report: ReproducibilityReport) -> str:
    """Record a probe as a simulation activity that generates the report."""
    with project.activity(ActivityKind.SIMULATION,
                          f"reproducibility probe of {report.backend}@{report.backend_version}",
                          requirements=("F05",),
                          params={"backend": report.backend, "seed": report.seed,
                                  "steps": report.steps, "platform": report.platform}) as act:
        act.generate(report.to_json().encode(), kind="reproducibility_report",
                     name=f"{report.backend}@{report.backend_version}.probe")
        act.metric("outcome", report.outcome.value)
        act.metric("ok", report.ok)
        for f in report.findings:
            act.note(f)
        act.outcome = report.outcome.value if report.ok else (
            f"{report.outcome.value}; " + "; ".join(report.findings))
    return act.activity_id
