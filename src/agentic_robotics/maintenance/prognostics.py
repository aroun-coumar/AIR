"""Prognostics: time-of-event estimates with uncertainty, gating capability on p5.

:class:`PrognosticEstimate` takes the *shape* of a prognostics result described for NASA
progpy (event state, a time-of-event distribution, the future-loading assumption, a
prediction horizon). progpy is under NOSA 1.3, which is not GPL-compatible, so nothing
here imports, links or copies it: the estimator below is a clean-room, stdlib-only
linear degradation fit with a residual bootstrap (ADR 0015).

Rules:

- an estimate is a distribution, never a point: quantiles (``p5``, ``p50``, ``p95``) of
  the time until the event, plus the share of bootstrap samples that never reach the
  event within the horizon (**censoring**); a censored quantile is a lower bound;
- capability is gated on **p5**, not the mean: a component whose p5 time-of-event is
  shorter than the mission is RESTRICTED; one that has reached its event is UNAVAILABLE;
  a horizon shorter than the mission is not evidence of health;
- the loading assumption is stated (here: the observed trend continues).
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..contracts.capability import CapabilityReport, ProfileStatus
from ..core.errors import SchemaError
from ..core.ids import new_id
from ..core.schema import Contract, contract
from ..health.gating import Gater, downgrade, worse
from ..health.model import HealthLevel
from ..health.readiness import CheckResult, ReadinessContext

__requirements__ = ("REQ16", "G13")

Direction = Literal["below", "above"]
METHOD = "air.linear_bootstrap/1"


@dataclass(frozen=True)
class Interval:
    low: float
    mean: float
    high: float


@dataclass(frozen=True)
class LoadingAssumption:
    kind: str = "observed_trend"
    description: str = "usage continues as in the observed window (linear in time)"
    parameters: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeOfEvent:
    """Seconds from ``estimated_at`` until the event. A quantile named in
    ``censored_quantiles`` did not reach the event within the horizon: its value is the
    horizon and means "at least this long"."""

    quantiles: dict[str, float]
    censored_quantiles: tuple[str, ...] = ()
    censored_fraction: float = 0.0
    mean_s: float | None = None          # only when nothing is censored
    samples: int = 0


@contract("prognostic_estimate", version="1.0")
class PrognosticEstimate(Contract):
    estimate_id: str
    robot_id: str
    component_id: str
    event: str                           # e.g. "state_of_health below 80 %"
    signal: str
    threshold: float
    direction: Direction
    estimated_at: float
    event_state: Interval                # 1 = as first observed, 0 = event reached
    time_of_event: TimeOfEvent
    horizon_s: float
    loading: LoadingAssumption = field(default_factory=LoadingAssumption)
    method: str = METHOD
    seed: int | None = None
    inputs_n: int = 0
    validation: dict[str, float] = field(default_factory=dict)

    @property
    def censored(self) -> bool:
        return self.time_of_event.censored_fraction > 0

    def quantile(self, name: str) -> tuple[float, bool]:
        """``(seconds, is_lower_bound)`` for ``p5``, ``p50``, ..."""
        if name not in self.time_of_event.quantiles:
            raise SchemaError("quantile not estimated", quantile=name,
                              available=sorted(self.time_of_event.quantiles))
        return (self.time_of_event.quantiles[name],
                name in self.time_of_event.censored_quantiles)

    def reached(self) -> bool:
        return self.event_state.mean <= 0.0 or self.time_of_event.quantiles.get("p50") == 0.0

    def gate_status(self, mission_s: float) -> tuple[ProfileStatus, str]:
        """Capability status for a mission of ``mission_s`` seconds, decided on p5."""
        if self.reached():
            return ProfileStatus.UNAVAILABLE, f"{self.event} reached"
        p5, lower_bound = self.quantile("p5")
        if lower_bound:
            if self.horizon_s >= mission_s:
                return ProfileStatus.AVAILABLE, ""
            return (ProfileStatus.RESTRICTED,
                    f"prognostic horizon {self.horizon_s:g} s shorter than mission "
                    f"{mission_s:g} s")
        if p5 < mission_s:
            return (ProfileStatus.RESTRICTED,
                    f"p5 time to {self.event} {p5:.4g} s < mission {mission_s:g} s")
        return ProfileStatus.AVAILABLE, ""


# -- estimator ----------------------------------------------------------------------------
def _fit(ts: Sequence[float], xs: Sequence[float]) -> tuple[float, float]:
    n = len(ts)
    mt, mx = sum(ts) / n, sum(xs) / n
    stt = sum((t - mt) ** 2 for t in ts)
    if stt == 0:
        raise SchemaError("degradation series needs at least two distinct times")
    slope = sum((t - mt) * (x - mx) for t, x in zip(ts, xs, strict=True)) / stt
    return mx - slope * mt, slope


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return math.inf
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    a, b = sorted_values[lo], sorted_values[hi]
    if math.isinf(a) or math.isinf(b):
        return b if pos - lo > 0 else a
    return a + (b - a) * (pos - lo)


@dataclass
class LinearDegradationEstimator:
    """Least-squares trend of a degrading signal with a residual bootstrap.

    ``direction="below"``: the event is the signal falling below ``threshold`` (state of
    health, remaining capacity); ``"above"``: rising above it (wear, temperature)."""

    threshold: float
    direction: Direction = "below"
    horizon_s: float = 30 * 86400.0
    n_boot: int = 400
    seed: int = 0
    quantiles: tuple[float, ...] = (0.05, 0.5, 0.95)

    def __post_init__(self) -> None:
        if self.direction not in ("below", "above"):
            raise SchemaError("direction must be below or above", direction=self.direction)
        if self.horizon_s <= 0 or self.n_boot < 10:
            raise SchemaError("horizon_s must be positive and n_boot >= 10")
        if 0.05 not in self.quantiles:
            raise SchemaError("p5 is required: capability is gated on it")

    def _toe(self, a: float, b: float, now: float) -> float:
        current = a + b * now
        sign = 1.0 if self.direction == "below" else -1.0
        if sign * (current - self.threshold) <= 0:
            return 0.0                                      # already past the threshold
        if sign * b >= 0:
            return math.inf                                 # not degrading towards it
        toe = (self.threshold - a) / b - now
        return toe if toe <= self.horizon_s else math.inf

    def estimate(self, series: Iterable[tuple[float, float]], *, robot_id: str,
                 component_id: str, signal: str, now: float | None = None,
                 event: str | None = None) -> PrognosticEstimate:
        points = sorted((float(t), float(x)) for t, x in series)
        if len(points) < 3:
            raise SchemaError("need at least three points to estimate a trend",
                              points=len(points))
        ts, xs = [p[0] for p in points], [p[1] for p in points]
        at = ts[-1] if now is None else float(now)
        a, b = _fit(ts, xs)
        residuals = [x - (a + b * t) for t, x in points]
        rng = random.Random(self.seed)
        toes, states = [], []
        start = a + b * ts[0]
        span = start - self.threshold

        def state(a_: float, b_: float) -> float:
            if span == 0:
                return 0.0
            return max(0.0, min(1.0, (a_ + b_ * at - self.threshold) / span))

        for _ in range(self.n_boot):
            ys = [a + b * t + rng.choice(residuals) for t in ts]
            try:
                a_k, b_k = _fit(ts, ys)
            except SchemaError:
                continue
            toes.append(self._toe(a_k, b_k, at))
            states.append(state(a_k, b_k))
        toes.sort()
        states.sort()
        quants: dict[str, float] = {}
        censored_q: list[str] = []
        for q in self.quantiles:
            name = f"p{round(q * 100):d}"
            value = _quantile(toes, q)
            if math.isinf(value):
                value = self.horizon_s
                censored_q.append(name)
            quants[name] = round(value, 6)
        censored = sum(math.isinf(t) for t in toes) / len(toes)
        finite = [t for t in toes if not math.isinf(t)]
        sym = "<" if self.direction == "below" else ">"
        return PrognosticEstimate(
            estimate_id=new_id("prog"), robot_id=robot_id, component_id=component_id,
            event=event or f"{signal} {sym} {self.threshold:g}", signal=signal,
            threshold=self.threshold, direction=self.direction, estimated_at=at,
            event_state=Interval(round(_quantile(states, 0.05), 6),
                                 round(state(a, b), 6), round(_quantile(states, 0.95), 6)),
            time_of_event=TimeOfEvent(
                quantiles=quants, censored_quantiles=tuple(censored_q),
                censored_fraction=round(censored, 6),
                mean_s=round(sum(finite) / len(finite), 6) if finite and not censored else None,
                samples=len(toes)),
            horizon_s=self.horizon_s,
            loading=LoadingAssumption(parameters={"slope_per_s": b, "intercept": a,
                                                  "window_s": ts[-1] - ts[0]}),
            seed=self.seed, inputs_n=len(points))


def prediction_error(estimate: PrognosticEstimate, observed_event_at: float
                     ) -> dict[str, float]:
    """After service: how the estimate compared with when the event actually happened."""
    actual = observed_event_at - estimate.estimated_at
    p50 = estimate.time_of_event.quantiles.get("p50", math.nan)
    lo = estimate.time_of_event.quantiles.get("p5", math.nan)
    hi = estimate.time_of_event.quantiles.get("p95", math.inf)
    return {"actual_s": actual, "error_p50_s": p50 - actual,
            "relative_error_p50": (p50 - actual) / actual if actual else math.nan,
            "within_p5_p95": 1.0 if lo <= actual <= hi else 0.0}


# -- gating, readiness, triggers ----------------------------------------------------------
def prognostic_gater(estimates: Iterable[PrognosticEstimate],
                     bindings: Mapping[str, Iterable[str]], mission_s: float) -> Gater:
    """A :class:`~agentic_robotics.health.HealthGate` source: ``bindings`` maps a
    capability (or ``*``) to the component ids whose estimates gate it."""
    ests = list(estimates)
    bound = {cap: set(cids) for cap, cids in bindings.items()}

    def gater(report: CapabilityReport, now: float) -> CapabilityReport:
        changes: dict[str, tuple[ProfileStatus, list[str], list[str]]] = {}
        for e in ests:
            if e.robot_id != report.robot_id:
                continue
            status, why = e.gate_status(mission_s)
            if status is ProfileStatus.AVAILABLE:
                continue
            for cap, cids in bound.items():
                if e.component_id in cids:
                    old = changes.get(cap, (ProfileStatus.AVAILABLE, [], []))
                    changes[cap] = (worse(old[0], status),
                                    old[1] + [f"prognostic:{e.component_id}:{why}"],
                                    old[2] + [f"prognostic:{e.estimate_id}"])
        return downgrade(report, changes)

    return gater


@dataclass
class PrognosticCheck:
    """Readiness: a p5 time-of-event shorter than the mission blocks ``modes``."""

    estimates: Sequence[PrognosticEstimate]
    mission_s: float
    modes: tuple[str, ...] = ("*",)
    check_id: str = "prognostics"
    order: int = 40

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        for e in self.estimates:
            if e.robot_id != ctx.robot_id:
                continue
            status, why = e.gate_status(self.mission_s)
            if status is ProfileStatus.UNAVAILABLE:
                yield CheckResult(self.check_id, HealthLevel.ERROR, why, subject=e.component_id,
                                  blocks=("*",), failsafe=(f"prognostic:{e.component_id}",))
            elif status is ProfileStatus.RESTRICTED:
                yield CheckResult(self.check_id, HealthLevel.WARN, why, subject=e.component_id,
                                  blocks=self.modes)


def threshold_crossings(estimates: Iterable[PrognosticEstimate], lead_s: float
                        ) -> list[PrognosticEstimate]:
    """Estimates whose p5 time-of-event falls within ``lead_s`` (maintenance triggers)."""
    out = []
    for e in estimates:
        p5, lower_bound = e.quantile("p5")
        if e.reached() or (not lower_bound and p5 <= lead_s):
            out.append(e)
    return out


def record_estimate(project: Any, estimate: PrognosticEstimate) -> None:
    """Ledger entry ``prognostic.estimate`` (subject: the component)."""
    project.ledger.append("prognostic.estimate", estimate.component_id,
                          {"robot_id": estimate.robot_id, "estimate": estimate.to_dict()})


def estimates_from_ledger(project: Any) -> list[PrognosticEstimate]:
    """The latest estimate per (robot, component, event) recorded in the ledger."""
    latest: dict[tuple[str, str, str], PrognosticEstimate] = {}
    for entry in project.ledger.query(kind="prognostic.estimate"):
        e = PrognosticEstimate.from_dict(entry.data["estimate"])
        latest[(e.robot_id, e.component_id, e.event)] = e
    return list(latest.values())
