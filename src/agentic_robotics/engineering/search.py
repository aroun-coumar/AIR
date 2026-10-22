"""Constrained search where AIR is the system of record (W3.4).

- One search is one :class:`SearchRecord` (sampler, its seed and version: optimizers do
  not persist them). One trial is one :class:`ExperimentRecord` in the project ledger.
- A trial is keyed by ``(config, scenario, seed, budget)``; the same key is never run
  twice in a search.
- Every candidate is evaluated on the *same* scenarios and seeds, so candidates are
  compared like for like. A candidate with any missing, failed or pruned trial gets no
  aggregate and cannot be on the Pareto set.
- Every trial ends in a terminal status with a reason: ``complete``, ``failed``,
  ``pruned`` or ``abandoned`` (budget exhausted, candidate pruned).
- Feasible means every constraint value is <= 0 (Optuna's convention). The Pareto set is
  computed over feasible candidates only.
- Release candidates are picked by :meth:`Search.select_release`: the Pareto set is
  re-run on held-out seeds, and only candidates that stay feasible are kept.

Samplers: :class:`RandomSampler` and :class:`GridSampler` (stdlib) and, behind the
``[optuna]`` extra, :class:`~agentic_robotics.engineering.optuna_adapter.OptunaSampler`.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from ..core.errors import FrameworkError, SchemaError
from ..core.schema import Contract, contract
from ..provenance.canonical import canonical_bytes
from ..provenance.model import ActivityKind, ExperimentRecord
from ..provenance.project import ActivityRecorder, Project, bytes_digest
from .backends import BackendFactory, TimePolicy
from .runner import ExperimentSpec, TraceLike, record_experiment_record, run_experiment

__requirements__ = ("REQ04", "G04", "F05")


class Direction(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class TrialStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    PRUNED = "pruned"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Param:
    name: str
    kind: Literal["float", "int", "choice"] = "float"
    low: float | None = None
    high: float | None = None
    choices: tuple[Any, ...] = ()
    log: bool = False
    #: Points per axis for grid search (float/int).
    grid_points: int = 3

    def __post_init__(self) -> None:
        if self.kind == "choice":
            if not self.choices:
                raise SchemaError("a choice parameter needs choices", param=self.name)
        elif self.low is None or self.high is None or self.low > self.high:
            raise SchemaError("a numeric parameter needs low <= high", param=self.name)
        if self.log and (self.low is None or self.low <= 0):
            raise SchemaError("log scale needs low > 0", param=self.name)

    def grid(self) -> list[Any]:
        if self.kind == "choice":
            return list(self.choices)
        lo, hi = float(self.low or 0.0), float(self.high or 0.0)
        n = max(1, self.grid_points)
        if n == 1:
            pts = [lo]
        elif self.log:
            pts = [math.exp(math.log(lo) + (math.log(hi) - math.log(lo)) * i / (n - 1))
                   for i in range(n)]
        else:
            pts = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        return sorted({round(p) for p in pts}) if self.kind == "int" else pts


@dataclass(frozen=True)
class Objective:
    metric: str
    direction: Direction = Direction.MINIMIZE


@dataclass(frozen=True)
class Constraint:
    """``metric op bound``; its value is the violation (feasible when <= 0)."""

    metric: str
    op: Literal["<=", ">="]
    bound: float

    def value(self, metric_value: float) -> float:
        return metric_value - self.bound if self.op == "<=" else self.bound - metric_value


@dataclass(frozen=True)
class TrialSummary:
    trial_key: str
    experiment_id: str
    candidate: str
    scenario: str
    seed: int
    status: TrialStatus
    reason: str = ""
    values: dict[str, float] = field(default_factory=dict)
    activity_id: str = ""


@dataclass(frozen=True)
class CandidateSummary:
    candidate: str
    config: dict[str, Any]
    status: TrialStatus
    reason: str = ""
    trials: int = 0
    complete: int = 0
    values: dict[str, float] = field(default_factory=dict)
    constraints: dict[str, float] = field(default_factory=dict)
    feasible: bool = False


@contract("search_record", version="1.0")
class SearchRecord(Contract):
    search_id: str
    title: str
    objectives: tuple[Objective, ...]
    space: tuple[Param, ...]
    sampler: str
    sampler_seed: int | None = None
    sampler_version: str = ""
    constraints: tuple[Constraint, ...] = ()
    scenarios: tuple[str, ...] = ()
    seeds: tuple[int, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    max_trials: int | None = None
    status: Literal["running", "finished", "abandoned"] = "running"
    status_reason: str = ""
    trials: tuple[TrialSummary, ...] = ()
    candidates: tuple[CandidateSummary, ...] = ()
    pareto: tuple[str, ...] = ()
    activity_id: str = ""


@contract("release_selection", version="1.0")
class ReleaseSelection(Contract):
    """Release candidates: the Pareto set re-validated on held-out seeds."""

    search_id: str
    held_out_seeds: tuple[int, ...]
    considered: tuple[str, ...]
    revalidated: tuple[CandidateSummary, ...] = ()
    rejected: dict[str, str] = field(default_factory=dict)
    selected: tuple[str, ...] = ()
    rationale: str = ""
    activity_id: str = ""


class Sampler(Protocol):
    name: str
    seed: int | None
    version: str

    def propose(self, space: Sequence[Param],
                history: Sequence[CandidateSummary]) -> dict[str, Any] | None: ...

    def observe(self, config: Mapping[str, Any], result: CandidateSummary,
                objectives: Sequence[Objective]) -> None: ...


class RandomSampler:
    name = "random"
    version = "1"

    def __init__(self, seed: int | None = 0) -> None:
        self.seed = seed
        self._rng = random.Random(seed)

    def propose(self, space: Sequence[Param],
                history: Sequence[CandidateSummary]) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for p in space:
            if p.kind == "choice":
                out[p.name] = self._rng.choice(list(p.choices))
            elif p.kind == "int":
                out[p.name] = self._rng.randint(int(p.low or 0), int(p.high or 0))
            elif p.log:
                lo, hi = math.log(float(p.low or 1.0)), math.log(float(p.high or 1.0))
                out[p.name] = math.exp(self._rng.uniform(lo, hi))
            else:
                out[p.name] = self._rng.uniform(float(p.low or 0.0), float(p.high or 0.0))
        return out

    def observe(self, config: Mapping[str, Any], result: CandidateSummary,
                objectives: Sequence[Objective]) -> None:
        return None


class GridSampler:
    name = "grid"
    version = "1"
    seed: int | None = None

    def __init__(self) -> None:
        self._it: Iterator[tuple[Any, ...]] | None = None
        self._names: list[str] = []

    def propose(self, space: Sequence[Param],
                history: Sequence[CandidateSummary]) -> dict[str, Any] | None:
        if self._it is None:
            self._names = [p.name for p in space]
            self._it = itertools.product(*(p.grid() for p in space))
        point = next(self._it, None)
        return None if point is None else dict(zip(self._names, point, strict=True))

    def observe(self, config: Mapping[str, Any], result: CandidateSummary,
                objectives: Sequence[Objective]) -> None:
        return None


def _digest(value: Any) -> str:
    return bytes_digest(canonical_bytes(value)).split(":")[1]


def candidate_id(config: Mapping[str, Any]) -> str:
    return "cand-" + _digest(dict(config))[:12]


def trial_key(config: Mapping[str, Any], scenario: str, seed: int,
              budget: Mapping[str, Any]) -> str:
    """Trials are identified by ``(config, scenario, seed, budget)``."""
    return "trial-" + _digest({"config": dict(config), "scenario": scenario, "seed": seed,
                               "budget": dict(budget)})[:16]


def dominates(a: Mapping[str, float], b: Mapping[str, float],
              objectives: Sequence[Objective]) -> bool:
    better = False
    for o in objectives:
        x, y = a[o.metric], b[o.metric]
        if o.direction == Direction.MAXIMIZE:
            x, y = -x, -y
        if x > y:
            return False
        if x < y:
            better = True
    return better


def pareto_front(candidates: Sequence[CandidateSummary],
                 objectives: Sequence[Objective]) -> list[CandidateSummary]:
    """Non-dominated feasible, complete candidates (Optuna ``best_trials`` semantics)."""
    pool = [c for c in candidates if c.feasible and c.status == TrialStatus.COMPLETE]
    return [c for c in pool
            if not any(dominates(o.values, c.values, objectives) for o in pool if o is not c)]


#: ``pruner(candidate_trials_so_far) -> reason or None``.
Pruner = Callable[[Sequence[TrialSummary]], str | None]


class Search:
    """Run a constrained search on one backend. All trials go to ``project`` if given."""

    def __init__(self, search_id: str, *, factory: BackendFactory, time_policy: TimePolicy,
                 space: Sequence[Param], objectives: Sequence[Objective],
                 scenarios: Mapping[str, Mapping[str, Any]], seeds: Sequence[int],
                 sampler: Sampler | None = None, constraints: Sequence[Constraint] = (),
                 steps: int = 100, budget: Mapping[str, Any] | None = None,
                 max_trials: int | None = None, pruner: Pruner | None = None,
                 project: Project | None = None, trace: TraceLike | None = None,
                 requirement_ids: Sequence[str] = ("REQ04", "G04"),
                 title: str = "") -> None:
        if not objectives:
            raise SchemaError("a search needs at least one objective")
        if not seeds or not scenarios:
            raise SchemaError("a search needs scenarios and seeds")
        if len(set(seeds)) != len(seeds):
            raise SchemaError("duplicate seeds", seeds=list(seeds))
        self.search_id = search_id
        self.factory = factory
        self.time_policy = time_policy
        self.space = tuple(space)
        self.objectives = tuple(objectives)
        self.constraints = tuple(constraints)
        self.scenarios = {k: dict(v) for k, v in scenarios.items()}
        self.seeds = tuple(seeds)
        self.sampler: Sampler = sampler or RandomSampler(0)
        self.steps = steps
        self.budget = dict(budget or {"steps": steps})
        self.max_trials = max_trials
        self.pruner = pruner
        self.project = project
        self.trace = trace
        self.requirement_ids = tuple(requirement_ids)
        self.title = title or f"search {search_id}"
        self._done: dict[str, TrialSummary] = {}

    # -- trials ------------------------------------------------------------------------
    def _spec(self, config: Mapping[str, Any], scenario: str, seed: int,
              key: str) -> ExperimentSpec:
        return ExperimentSpec(
            experiment_id=f"{self.search_id}/{key}", hypothesis=f"{self.title}: trial {key}",
            time_policy=self.time_policy, scenario_name=scenario,
            scenario=self.scenarios[scenario], config=dict(config), seed=seed, episodes=1,
            steps=self.steps, requirement_ids=self.requirement_ids, budget=self.budget,
            candidate=candidate_id(config))

    def _record(self, record: ExperimentRecord, parent: str | None,
                samples: bytes | None = None) -> str:
        if self.project is None:
            return ""
        return record_experiment_record(self.project, record, samples=samples, parent=parent)

    def _terminal(self, config: Mapping[str, Any], scenario: str, seed: int,
                  status: TrialStatus, reason: str, parent: str | None) -> TrialSummary:
        key = trial_key(config, scenario, seed, self.budget)
        record = ExperimentRecord(
            experiment_id=f"{self.search_id}/{key}", hypothesis=f"{self.title}: trial {key}",
            requirement_ids=self.requirement_ids, candidate=candidate_id(config),
            scenario=scenario, seed=seed, config=dict(config),
            scenario_params=self.scenarios[scenario], search_id=self.search_id,
            trial_key=key, status=status.value, status_reason=reason, budget=self.budget)
        t = TrialSummary(trial_key=key, experiment_id=record.experiment_id,
                         candidate=record.candidate, scenario=scenario, seed=seed,
                         status=status, reason=reason,
                         activity_id=self._record(record, parent))
        self._finish(t)
        return t

    def _finish(self, t: TrialSummary) -> None:
        self._done[t.trial_key] = t
        if self.trace is not None:
            self.trace.record("trial_finished", t.experiment_id, status=t.status.value,
                              reason=t.reason, candidate=t.candidate)

    def run_trial(self, config: Mapping[str, Any], scenario: str, seed: int,
                  parent: str | None = None,
                  prior: Sequence[TrialSummary] = ()) -> TrialSummary:
        """Run one keyed trial. ``prior`` are this candidate's earlier trials, given to
        the pruner; a pruned trial keeps its measured values and is recorded as pruned."""
        key = trial_key(config, scenario, seed, self.budget)
        if key in self._done:
            return self._done[key]
        spec = self._spec(config, scenario, seed, key)
        backend = self.factory()
        try:
            run = run_experiment(backend, spec, search_id=self.search_id, trial_key=key)
        except (FrameworkError, ArithmeticError, ValueError, KeyError, TypeError) as exc:
            return self._terminal(config, scenario, seed, TrialStatus.FAILED,
                                  f"{type(exc).__name__}: {exc}", parent)
        finally:
            backend.close()
        values = {k: float(v) for k, v in run.record.results.items()
                  if isinstance(v, (int, float))}
        bad = [m for m in [o.metric for o in self.objectives] +
               [c.metric for c in self.constraints] if not math.isfinite(values.get(m, math.nan))]
        record = run.record
        status, reason = TrialStatus.COMPLETE, ""
        if bad:
            status, reason = TrialStatus.FAILED, f"missing or non-finite metrics: {bad}"
        elif self.pruner is not None:
            why = self.pruner([*prior, TrialSummary(
                trial_key=key, experiment_id=record.experiment_id,
                candidate=record.candidate, scenario=scenario, seed=seed,
                status=status, values=values)])
            if why:
                status, reason = TrialStatus.PRUNED, why
        if status != TrialStatus.COMPLETE:
            record = record.evolve(status=status.value, status_reason=reason)
        t = TrialSummary(trial_key=key, experiment_id=record.experiment_id,
                         candidate=record.candidate, scenario=scenario, seed=seed,
                         status=status, reason=reason, values=values,
                         activity_id=self._record(record, parent, run.samples_blob()))
        self._finish(t)
        return t

    # -- candidates --------------------------------------------------------------------
    def evaluate_candidate(self, config: Mapping[str, Any], seeds: Sequence[int],
                           parent: str | None, trials_left: list[int]) -> tuple[
                               CandidateSummary, list[TrialSummary]]:
        trials: list[TrialSummary] = []
        stop: tuple[TrialStatus, str] | None = None
        plan = [(s, seed) for s in self.scenarios for seed in seeds]
        for scenario, seed in plan:
            if stop is not None:
                trials.append(self._terminal(config, scenario, seed, TrialStatus.ABANDONED,
                                             stop[1], parent))
                continue
            if trials_left[0] <= 0:
                stop = (TrialStatus.ABANDONED, "search trial budget exhausted")
                trials.append(self._terminal(config, scenario, seed, TrialStatus.ABANDONED,
                                             stop[1], parent))
                continue
            trials_left[0] -= 1
            t = self.run_trial(config, scenario, seed, parent, prior=trials)
            if t.status == TrialStatus.PRUNED:
                stop = (TrialStatus.ABANDONED, f"candidate pruned: {t.reason}")
            trials.append(t)
        return self._summarize(config, trials, len(plan)), trials

    def _summarize(self, config: Mapping[str, Any], trials: Sequence[TrialSummary],
                   planned: int) -> CandidateSummary:
        cid = candidate_id(config)
        complete = [t for t in trials if t.status == TrialStatus.COMPLETE]
        if len(complete) < planned:
            worst = next((t for t in trials if t.status == TrialStatus.PRUNED), None) or \
                next((t for t in trials if t.status == TrialStatus.FAILED), None) or \
                next(t for t in trials if t.status != TrialStatus.COMPLETE)
            return CandidateSummary(candidate=cid, config=dict(config), status=worst.status,
                                    reason=f"{planned - len(complete)} of {planned} trials "
                                           f"not complete: {worst.reason}",
                                    trials=len(trials), complete=len(complete))
        values = {o.metric: sum(t.values[o.metric] for t in complete) / len(complete)
                  for o in self.objectives}
        cons = {f"{c.metric}{c.op}{c.bound}": max(c.value(t.values[c.metric])
                                                   for t in complete)
                for c in self.constraints}
        feasible = all(v <= 0 for v in cons.values())
        return CandidateSummary(candidate=cid, config=dict(config),
                                status=TrialStatus.COMPLETE,
                                reason="" if feasible else "violates constraints",
                                trials=len(trials), complete=len(complete), values=values,
                                constraints=cons, feasible=feasible)

    # -- the search --------------------------------------------------------------------
    def _activity(self, title: str, kind: ActivityKind, params: dict[str, Any]
                  ) -> ActivityRecorder | nullcontext[None]:
        if self.project is None:
            return nullcontext()
        return self.project.activity(kind, title, requirements=self.requirement_ids,
                                     params=params)

    def run(self, candidates: int) -> SearchRecord:
        """Propose and evaluate up to ``candidates`` configurations."""
        history: list[CandidateSummary] = []
        trials: list[TrialSummary] = []
        trials_left = [self.max_trials if self.max_trials is not None else 1 << 62]
        status: Literal["running", "finished", "abandoned"] = "finished"
        reason = ""
        params = {"search_id": self.search_id, "sampler": self.sampler.name,
                  "sampler_seed": self.sampler.seed,
                  "sampler_version": self.sampler.version, "seeds": list(self.seeds),
                  "scenarios": sorted(self.scenarios)}
        with self._activity(self.title, ActivityKind.EXPERIMENT, params) as act:
            parent = act.activity_id if act is not None else None
            for _ in range(candidates):
                config = self.sampler.propose(self.space, history)
                if config is None:
                    reason = "sampler exhausted"
                    break
                seen = next((h for h in history if h.candidate == candidate_id(config)), None)
                if seen is not None:
                    # a repeated proposal is the same trials; never run or count it twice
                    self.sampler.observe(config, seen, self.objectives)
                    continue
                summary, ts = self.evaluate_candidate(config, self.seeds, parent, trials_left)
                trials.extend(ts)
                history.append(summary)
                self.sampler.observe(config, summary, self.objectives)
                if trials_left[0] <= 0 and summary.status == TrialStatus.ABANDONED:
                    status, reason = "abandoned", "search trial budget exhausted"
                    break
            front = pareto_front(history, self.objectives)
            record = SearchRecord(
                search_id=self.search_id, title=self.title, objectives=self.objectives,
                space=self.space, sampler=self.sampler.name,
                sampler_seed=self.sampler.seed, sampler_version=self.sampler.version,
                constraints=self.constraints, scenarios=tuple(self.scenarios),
                seeds=self.seeds, budget=self.budget, max_trials=self.max_trials,
                status=status, status_reason=reason, trials=tuple(trials),
                candidates=tuple(history), pareto=tuple(c.candidate for c in front),
                activity_id=parent or "")
            if act is not None:
                act.generate(record.to_json().encode(), kind="search_record",
                             name=self.search_id)
                act.metric("trials", len(trials))
                act.metric("candidates", len(history))
                act.metric("pareto", len(front))
                for st in TrialStatus:
                    act.metric(f"trials_{st.value}",
                               sum(1 for t in trials if t.status == st))
                act.outcome = status if not reason else f"{status}: {reason}"
        return record

    def select_release(self, record: SearchRecord, held_out_seeds: Sequence[int],
                       *, max_selected: int | None = None) -> ReleaseSelection:
        """Re-run the Pareto set on held-out seeds; select the re-validated feasible
        Pareto candidates. Stored search scores are never reused for this decision."""
        held = tuple(held_out_seeds)
        overlap = sorted(set(held) & set(record.seeds))
        if not held or overlap:
            raise SchemaError("held-out seeds must be non-empty and disjoint from the "
                              "search seeds", overlap=overlap)
        by_id = {c.candidate: c for c in record.candidates}
        considered = tuple(record.pareto)
        rejected: dict[str, str] = {}
        revalidated: list[CandidateSummary] = []
        params = {"search_id": record.search_id, "held_out_seeds": list(held),
                  "considered": list(considered)}
        with self._activity(f"release selection for {record.search_id}",
                            ActivityKind.DECISION, params) as act:
            parent = act.activity_id if act is not None else None
            for cid in considered:
                summary, _ = self.evaluate_candidate(by_id[cid].config, held, parent,
                                                     [1 << 62])
                if summary.status != TrialStatus.COMPLETE:
                    rejected[cid] = f"held-out trials {summary.status.value}: {summary.reason}"
                elif not summary.feasible:
                    rejected[cid] = "infeasible on held-out seeds: " + ", ".join(
                        f"{k}={v:.4g}" for k, v in summary.constraints.items() if v > 0)
                else:
                    revalidated.append(summary)
            front = pareto_front(revalidated, self.objectives)
            for c in revalidated:
                if c not in front:
                    rejected[c.candidate] = "dominated on held-out seeds"
            first = self.objectives[0]
            front.sort(key=lambda c: c.values[first.metric] *
                       (1 if first.direction == Direction.MINIMIZE else -1))
            selected = front[:max_selected] if max_selected else front
            rationale = (f"{len(selected)} of {len(considered)} Pareto candidates stayed "
                         f"feasible and non-dominated on {len(held)} held-out seed(s)")
            selection = ReleaseSelection(
                search_id=record.search_id, held_out_seeds=held, considered=considered,
                revalidated=tuple(revalidated), rejected=rejected,
                selected=tuple(c.candidate for c in selected), rationale=rationale,
                activity_id=parent or "")
            if act is not None:
                act.generate(selection.to_json().encode(), kind="release_selection",
                             name=f"{record.search_id}.release")
                act.metric("selected", len(selected))
                act.outcome = rationale
        return selection
