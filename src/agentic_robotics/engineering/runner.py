"""Run an experiment on a backend and produce an :class:`ExperimentRecord` that says
exactly what ran: seeds, the backend actually used, the platform, the runner version and
a physics fingerprint of the trajectories.

Each episode's RNG state is snapshotted at its start, so a single failing episode can be
replayed alone with :func:`replay_sample` (Drake ``MonteCarloSimulation``).
"""

from __future__ import annotations

import json
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.errors import SchemaError
from ..provenance.canonical import canonical_bytes
from ..provenance.model import ActivityKind, ExperimentRecord, SeedRecord
from ..provenance.project import Project, bytes_digest
from .backends import RUNNER_VERSION, BackendInfo, ExperimentBackend, TimePolicy
from .seeds import seed_record

__requirements__ = ("REQ02", "REQ04", "F05", "G03")

DEFAULT_STEPS = 100


class TraceLike(Protocol):
    """Anything with ``record(kind, subject, **data)``, e.g. ``runtime.Trace``."""

    def record(self, kind: str, subject: str, **data: Any) -> Any: ...


_OPS: dict[str, Callable[[float, float], bool]] = {
    "<=": operator.le, "<": operator.lt, ">=": operator.ge, ">": operator.gt,
}


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    hypothesis: str
    time_policy: TimePolicy
    scenario_name: str = "default"
    scenario: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    episodes: int = 1
    steps: int | None = None
    features: tuple[str, ...] = ()
    device: str = "cpu"
    requirement_ids: tuple[str, ...] = ("REQ04",)
    #: ``{"final_error": ["<=", 0.05]}``: all must hold for ``accepted=True``.
    acceptance: dict[str, tuple[str, float]] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    candidate: str = ""

    def __post_init__(self) -> None:
        if self.episodes < 1:
            raise SchemaError("an experiment needs at least one episode")
        for metric, (op, _) in self.acceptance.items():
            if op not in _OPS:
                raise SchemaError("unknown acceptance operator", metric=metric, op=op)

    @property
    def control_steps(self) -> int:
        if self.steps is not None:
            return self.steps
        return self.time_policy.episode_steps or DEFAULT_STEPS


@dataclass(frozen=True)
class Sample:
    """One episode: its seed, the RNG state at its start, its metrics and fingerprint."""

    index: int
    seed: int | None
    rng_state: Any
    metrics: dict[str, float]
    fingerprint: str


@dataclass(frozen=True)
class ExperimentRun:
    record: ExperimentRecord
    info: BackendInfo
    samples: tuple[Sample, ...]

    def samples_blob(self) -> bytes:
        return samples_blob(self.samples)


def samples_blob(samples: tuple[Sample, ...]) -> bytes:
    return canonical_bytes([{"index": s.index, "seed": s.seed, "rng_state": s.rng_state,
                             "metrics": s.metrics, "fingerprint": s.fingerprint}
                            for s in samples])


def load_samples(blob: bytes) -> tuple[Sample, ...]:
    data = json.loads(blob)
    return tuple(Sample(index=int(d["index"]), seed=d.get("seed"), rng_state=d["rng_state"],
                        metrics={k: float(v) for k, v in d["metrics"].items()},
                        fingerprint=str(d["fingerprint"])) for d in data)


def physics_fingerprint(trajectory: Any) -> str:
    """Digest of a trajectory in canonical JSON (shortest round-trip floats), so the same
    numbers give the same fingerprint in any implementation."""
    return bytes_digest(canonical_bytes(trajectory))


def _copy(obs: Mapping[str, Any]) -> Any:
    # Detach from backend buffers before keeping (a reused buffer would rewrite history).
    return json.loads(json.dumps(obs))


def run_episode(backend: ExperimentBackend, config: Mapping[str, Any], seed: int | None,
                steps: int, rng_state: Any = None) -> tuple[list[Any], dict[str, float], Any]:
    """Reset, optionally restore an RNG snapshot, and step. Returns the trajectory, the
    metrics and the RNG state at episode start."""
    traj = [_copy(backend.reset(seed))]
    if rng_state is not None:
        backend.set_rng_state(rng_state)
    start_state = backend.rng_state() if backend.capabilities.supports_rng_state else None
    for _ in range(steps):
        traj.append(_copy(backend.step(config)))
    return traj, dict(backend.metrics()), start_state


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def evaluate_acceptance(acceptance: Mapping[str, tuple[str, float]],
                        results: Mapping[str, Any]) -> bool | None:
    if not acceptance:
        return None
    for metric, (op, bound) in acceptance.items():
        value = results.get(metric)
        if not isinstance(value, (int, float)) or not _OPS[op](float(value), float(bound)):
            return False
    return True


def run_experiment(backend: ExperimentBackend, spec: ExperimentSpec, *,
                   project: Project | None = None, trace: TraceLike | None = None,
                   search_id: str = "", trial_key: str = "",
                   parent: str | None = None) -> ExperimentRun:
    """Load, run every episode and build the record. Load errors (unsupported features,
    bad timing) raise. With ``project`` the run is recorded in the ledger."""
    info = backend.load(spec.scenario, spec.time_policy, features=spec.features,
                        device=spec.device)
    seeds = seed_record(spec.seed, episodes=spec.episodes,
                        seeded_rngs=backend.capabilities.seeded_rngs)
    if trace is not None:
        trace.record("experiment_started", spec.experiment_id, backend=info.backend,
                     version=info.version, platform=info.platform,
                     seed=seeds.root, seed_source=seeds.source.value)
    samples = []
    trajectories = []
    for i in range(spec.episodes):
        ep_seed = seeds.per_episode[i] if seeds.per_episode else None
        traj, metrics, state = run_episode(backend, spec.config, ep_seed, spec.control_steps)
        fp = physics_fingerprint(traj)
        trajectories.append(fp)
        samples.append(Sample(index=i, seed=ep_seed, rng_state=state, metrics=metrics,
                              fingerprint=fp))
        if trace is not None:
            trace.record("episode_finished", spec.experiment_id, index=i, seed=ep_seed,
                         fingerprint=fp, **metrics)
    names = sorted({k for s in samples for k in s.metrics})
    results: dict[str, Any] = {k: _mean([s.metrics[k] for s in samples if k in s.metrics])
                               for k in names}
    blob = samples_blob(tuple(samples))
    seeds = SeedRecord(root=seeds.root, source=seeds.source, per_episode=seeds.per_episode,
                       per_env=seeds.per_env, seeded_rngs=seeds.seeded_rngs,
                       derivation=seeds.derivation, sample_states=bytes_digest(blob))
    limitations = list(info.non_reproducible)
    if info.fell_back:
        limitations.append(f"requested device {info.requested_device!r} ran on "
                           f"{info.actual_device!r}")
    record = ExperimentRecord(
        experiment_id=spec.experiment_id, hypothesis=spec.hypothesis,
        requirement_ids=spec.requirement_ids, candidate=spec.candidate,
        scenario=spec.scenario_name, backend=info.backend, backend_version=info.version,
        model_fidelity=",".join(f"{k}={v.value}" for k, v in
                                sorted(info.capabilities.fidelity.items())),
        seed=seeds.root,
        numerical_settings={"physics_dt": spec.time_policy.physics_dt,
                            "control_dt": spec.time_policy.control_dt,
                            "decimation": spec.time_policy.decimation,
                            "steps": spec.control_steps, "episodes": spec.episodes},
        acceptance_criteria={k: list(v) for k, v in spec.acceptance.items()},
        results=results, accepted=evaluate_acceptance(spec.acceptance, results),
        limitations=tuple(limitations), seeds=seeds, backend_info=info.to_dict(),
        config=dict(spec.config), scenario_params=dict(spec.scenario),
        platform=info.platform, runner_version=RUNNER_VERSION,
        physics_fingerprint=physics_fingerprint(trajectories), search_id=search_id,
        trial_key=trial_key, status="complete" if search_id else "",
        budget=dict(spec.budget))
    if trace is not None:
        trace.record("experiment_finished", spec.experiment_id,
                     fingerprint=record.physics_fingerprint, accepted=record.accepted)
    run = ExperimentRun(record=record, info=info, samples=tuple(samples))
    if project is not None:
        record_run(project, run, parent=parent)
    return run


def record_run(project: Project, run: ExperimentRun, *, parent: str | None = None) -> str:
    """Record ``run`` as an experiment activity; returns the activity id."""
    return record_experiment_record(project, run.record, samples=run.samples_blob(),
                                    parent=parent)


def record_experiment_record(project: Project, record: ExperimentRecord, *,
                             samples: bytes | None = None, parent: str | None = None) -> str:
    title = record.hypothesis or record.experiment_id
    with project.activity(ActivityKind.EXPERIMENT, title,
                          requirements=record.requirement_ids, parent=parent,
                          params={"experiment_id": record.experiment_id,
                                  "scenario": record.scenario, "backend": record.backend,
                                  "backend_version": record.backend_version,
                                  "seed": record.seed, "platform": record.platform,
                                  "runner_version": record.runner_version,
                                  "search_id": record.search_id,
                                  "trial_key": record.trial_key,
                                  "config": record.config}) as act:
        if samples is not None:
            act.generate(samples, kind="experiment_samples",
                         name=f"{record.experiment_id}.samples")
        act.generate(record.to_json().encode(), kind="experiment_record",
                     name=record.experiment_id)
        for k, v in record.results.items():
            act.metric(k, v)
        if record.status in ("failed", "abandoned"):
            act.fail(record.status_reason or record.status)
        act.outcome = record.status or {True: "accepted", False: "rejected",
                                        None: "inconclusive"}[record.accepted]
    return act.activity_id


def replay_sample(backend: ExperimentBackend, spec: ExperimentSpec, sample: Sample
                  ) -> tuple[bool, dict[str, float]]:
    """Replay one episode alone from its seed and RNG snapshot. Returns whether the
    fingerprint matched and the re-measured metrics."""
    backend.load(spec.scenario, spec.time_policy, features=spec.features, device=spec.device)
    traj, metrics, _ = run_episode(backend, spec.config, sample.seed, spec.control_steps,
                                   rng_state=sample.rng_state)
    return physics_fingerprint(traj) == sample.fingerprint, metrics


def experiment_records(project: Project) -> list[ExperimentRecord]:
    """Every ExperimentRecord stored in the project (via its generated entity)."""
    out = []
    for ref in project.entities().values():
        if ref.kind == "experiment_record" and ref.digest and project.artifacts.has(ref.digest):
            out.append(ExperimentRecord.load(project.artifacts.path(ref.digest)))
    return out
