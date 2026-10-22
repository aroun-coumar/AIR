"""Engineering and optimization loop (baseline stage 4; ADR 0014).

    from agentic_robotics.engineering import (PointMassBackend, TimePolicy, ExperimentSpec,
                                              run_experiment, probe_reproducibility, Search)

    tp = TimePolicy(physics_dt=0.01, control_dt=0.05)
    run = run_experiment(PointMassBackend(), ExperimentSpec("exp-1", "PD settles", tp,
                         config={"kp": 4.0, "kd": 2.0}, seed=7, episodes=3), project=p)
    report = probe_reproducibility(PointMassBackend, tp, config={"kp": 4.0}, seed=1)

- ``backends``: the declared-capability backend protocol, :class:`TimePolicy`,
  :class:`BackendInfo` (what actually ran) and the backend registry.
- ``process``: out-of-process backends over JSON lines (``air.backend/1``).
- ``seeds``/``runner``: seed records, experiment runs, per-sample replay.
- ``probe``: measured reproducibility (exact / within tolerance / differs).
- ``golden``: physics fingerprints, per-platform golden runs, re-measurement.
- ``search``: keyed trials, terminal statuses, Pareto sets, held-out release selection.
- ``evaluation``: sim-to-real validity (MMRV, Pearson) and invalidation with provenance.
"""

from ..provenance.model import SeedRecord, SeedSource
from .backends import (
    RUNNER_VERSION,
    BackendCapabilities,
    BackendInfo,
    DeterminismClass,
    ExperimentBackend,
    Fidelity,
    RestoreLevel,
    Snapshot,
    TimePolicy,
    UnsupportedFeature,
    available_backends,
    make_backend,
    platform_key,
    register_backend,
)
from .evaluation import (
    BenchmarkValidity,
    assess_validity,
    invalidate,
    invalidations,
    mmrv,
    pearson,
    record_validity,
    usable_as_evidence,
)
from .golden import (
    GoldenCheck,
    GoldenRun,
    GoldenStatus,
    GoldenStore,
    NotComparable,
    check_golden,
    compare_results,
    measure_golden,
    remeasure,
)
from .pointmass import PointMassBackend
from .probe import Reproducibility, ReproducibilityReport, probe_reproducibility
from .process import ProcessBackend, SubprocessTransport, process_factory, serve
from .runner import (
    ExperimentRun,
    ExperimentSpec,
    Sample,
    experiment_records,
    physics_fingerprint,
    record_run,
    replay_sample,
    run_experiment,
)
from .search import (
    Constraint,
    Direction,
    GridSampler,
    Objective,
    Param,
    RandomSampler,
    ReleaseSelection,
    Search,
    SearchRecord,
    TrialStatus,
    pareto_front,
    trial_key,
)
from .seeds import derive_seed, seed_record

__requirements__ = ("REQ04", "G04")

__all__ = [
    "RUNNER_VERSION", "BackendCapabilities", "BackendInfo", "BenchmarkValidity",
    "Constraint", "DeterminismClass", "Direction", "ExperimentBackend", "ExperimentRun",
    "ExperimentSpec", "Fidelity", "GoldenCheck", "GoldenRun", "GoldenStatus", "GoldenStore",
    "GridSampler", "NotComparable", "Objective", "Param", "PointMassBackend",
    "ProcessBackend", "RandomSampler", "ReleaseSelection", "Reproducibility",
    "ReproducibilityReport", "RestoreLevel", "Sample", "Search", "SearchRecord",
    "SeedRecord", "SeedSource", "Snapshot", "SubprocessTransport", "TimePolicy",
    "TrialStatus", "UnsupportedFeature", "assess_validity", "available_backends",
    "check_golden", "compare_results", "derive_seed", "experiment_records", "invalidate",
    "invalidations", "make_backend", "measure_golden", "mmrv", "pareto_front", "pearson",
    "physics_fingerprint", "platform_key", "probe_reproducibility", "process_factory",
    "record_run", "record_validity", "register_backend", "remeasure", "replay_sample",
    "run_experiment", "seed_record", "serve", "trial_key", "usable_as_evidence",
]
