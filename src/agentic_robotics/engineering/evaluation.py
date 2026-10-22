"""Evaluation validity: a simulator benchmark counts as evidence only after its
sim-to-real validity is recorded (SimplerEnv), and results can be marked invalid later,
with provenance, instead of being deleted (inspect_ai, lm-eval-harness).

Validity is measured on policies scored both in simulation and on real robots:
- **MMRV** (mean maximum rank violation): for each policy, the largest real-score gap to
  another policy that the simulator ranks the other way round, averaged. 0 is perfect.
- **Pearson correlation** of sim and real scores.

    v = assess_validity("pick-cube", sim={"a": .8, "b": .5, "c": .3},
                        real={"a": .7, "b": .4, "c": .2})
    record_validity(project, v)
    ok, why = usable_as_evidence(project, record, "pick-cube")
    invalidate(project, record.experiment_id, by="person:ana", reason="wrong asset")
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import field
from typing import Any

from ..core.errors import SchemaError
from ..core.ids import new_id
from ..core.schema import Contract, contract
from ..provenance.model import ActivityKind, ExperimentRecord
from ..provenance.project import Project

__requirements__ = ("REQ02", "G03", "F05")

INVALIDATED = "evidence.invalidated"


def mmrv(sim: Sequence[float], real: Sequence[float]) -> float:
    """Mean maximum rank violation (SimplerEnv's definition, computed in stdlib)."""
    if len(sim) != len(real) or len(sim) < 2:
        raise SchemaError("MMRV needs two equal-length score lists of at least 2")
    n = len(sim)
    worst = []
    for i in range(n):
        violations = [abs(real[i] - real[j]) for j in range(n)
                      if j != i and (sim[i] < sim[j]) != (real[i] < real[j])]
        worst.append(max(violations, default=0.0))
    return sum(worst) / n


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Pearson correlation; ``None`` when undefined (a constant series)."""
    if len(x) != len(y) or len(x) < 2:
        raise SchemaError("correlation needs two equal-length lists of at least 2")
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


@contract("benchmark_validity", version="1.0")
class BenchmarkValidity(Contract):
    validity_id: str
    benchmark: str
    sim_scores: dict[str, float]
    real_scores: dict[str, float]
    mmrv: float
    pearson: float | None = None
    max_mmrv: float = 0.1
    min_pearson: float = 0.7
    min_policies: int = 3
    valid: bool = False
    reasons: tuple[str, ...] = ()
    backend: str = ""
    backend_version: str = ""
    platform: str = ""
    real_evidence: tuple[str, ...] = ()
    assessed_at: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)


def assess_validity(benchmark: str, sim: Mapping[str, float], real: Mapping[str, float],
                    *, max_mmrv: float = 0.1, min_pearson: float = 0.7,
                    min_policies: int = 3, backend: str = "", backend_version: str = "",
                    platform: str = "", real_evidence: Sequence[str] = ()
                    ) -> BenchmarkValidity:
    """Compare sim and real scores of the same policies; only policies with both count."""
    policies = sorted(set(sim) & set(real))
    reasons = []
    if len(policies) < max(2, min_policies):
        reasons.append(f"only {len(policies)} policies scored in both sim and real "
                       f"(need {max(2, min_policies)})")
    if not real_evidence:
        reasons.append("no evidence reference for the real-robot scores")
    m, r = math.inf, None
    if len(policies) >= 2:
        s = [float(sim[p]) for p in policies]
        q = [float(real[p]) for p in policies]
        m, r = mmrv(s, q), pearson(s, q)
        if m > max_mmrv:
            reasons.append(f"MMRV {m:.4f} > {max_mmrv}")
        if r is None:
            reasons.append("correlation undefined (constant scores)")
        elif r < min_pearson:
            reasons.append(f"Pearson {r:.4f} < {min_pearson}")
    return BenchmarkValidity(
        validity_id=new_id("val"), benchmark=benchmark,
        sim_scores={p: float(sim[p]) for p in policies},
        real_scores={p: float(real[p]) for p in policies},
        mmrv=m if math.isfinite(m) else -1.0, pearson=r, max_mmrv=max_mmrv,
        min_pearson=min_pearson, min_policies=min_policies, valid=not reasons,
        reasons=tuple(reasons), backend=backend, backend_version=backend_version,
        platform=platform, real_evidence=tuple(real_evidence), assessed_at=time.time())


def record_validity(project: Project, validity: BenchmarkValidity,
                    requirements: Sequence[str] = ("G03",)) -> str:
    """Record the assessment as an evaluation activity; returns the activity id."""
    with project.activity(ActivityKind.EVALUATION,
                          f"sim-to-real validity of {validity.benchmark}",
                          requirements=requirements,
                          params={"validity_id": validity.validity_id,
                                  "benchmark": validity.benchmark,
                                  "backend": validity.backend,
                                  "backend_version": validity.backend_version}) as act:
        for ref in validity.real_evidence:
            act.note(f"real-robot evidence: {ref}")
        act.generate(validity.to_json().encode(), kind="benchmark_validity",
                     name=validity.validity_id)
        act.metric("mmrv", validity.mmrv)
        act.metric("pearson", validity.pearson)
        act.metric("valid", validity.valid)
        act.outcome = "valid" if validity.valid else "not valid: " + "; ".join(
            validity.reasons)
    return act.activity_id


def invalidate(project: Project, subject: str, *, by: str, reason: str,
               evidence: Sequence[str] = ()) -> int:
    """Mark an experiment, trial, validity assessment or benchmark invalid. Nothing is
    deleted; the ledger records who, why and on what evidence. Returns the ledger seq."""
    if not by or not reason.strip():
        raise SchemaError("invalidation needs a principal and a reason")
    entry = project.ledger.append(INVALIDATED, subject, {
        "by": by, "reason": reason, "evidence": list(evidence)})
    return entry.seq


def invalidations(project: Project) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in project.ledger.query(kind=INVALIDATED):
        out.setdefault(e.subject, []).append({"seq": e.seq, "ts": e.ts, **e.data})
    return out


def validity_assessments(project: Project, benchmark: str | None = None
                         ) -> list[BenchmarkValidity]:
    out = []
    for ref in project.entities().values():
        if ref.kind == "benchmark_validity" and ref.digest and \
                project.artifacts.has(ref.digest):
            v = BenchmarkValidity.load(project.artifacts.path(ref.digest))
            if benchmark is None or v.benchmark == benchmark:
                out.append(v)
    return sorted(out, key=lambda v: v.assessed_at)


def usable_as_evidence(project: Project, record: ExperimentRecord, benchmark: str
                       ) -> tuple[bool, tuple[str, ...]]:
    """A benchmark score is evidence only if the result is not invalidated and the
    benchmark has a valid, non-invalidated validity assessment for the same backend
    version."""
    bad = invalidations(project)
    reasons = []
    if record.experiment_id in bad:
        reasons.append(f"experiment {record.experiment_id} was invalidated: "
                       f"{bad[record.experiment_id][-1]['reason']}")
    if benchmark in bad:
        reasons.append(f"benchmark {benchmark} was invalidated: {bad[benchmark][-1]['reason']}")
    usable = [v for v in validity_assessments(project, benchmark)
              if v.valid and v.validity_id not in bad
              and (not v.backend_version or v.backend_version == record.backend_version)
              and (not v.backend or v.backend == record.backend)]
    if not usable:
        reasons.append(f"no valid sim-to-real assessment recorded for {benchmark!r} on "
                       f"{record.backend}@{record.backend_version}")
    return not reasons, tuple(reasons)
