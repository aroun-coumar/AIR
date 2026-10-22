"""Engineering loop: declared backends, time policy, seeds and replay, measured
reproducibility, golden runs, keyed search with terminal statuses, held-out release
selection, and evaluation validity with invalidation (ADR 0014)."""

from __future__ import annotations

import json
import math
import random
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics.cli import main as cli
from agentic_robotics.core import SchemaError
from agentic_robotics.engineering import (
    BackendCapabilities,
    BackendInfo,
    Constraint,
    DeterminismClass,
    ExperimentSpec,
    GoldenStatus,
    GoldenStore,
    GridSampler,
    NotComparable,
    Objective,
    Param,
    PointMassBackend,
    ProcessBackend,
    RandomSampler,
    Reproducibility,
    Search,
    SeedSource,
    SubprocessTransport,
    TimePolicy,
    TrialStatus,
    UnsupportedFeature,
    assess_validity,
    check_golden,
    compare_results,
    derive_seed,
    experiment_records,
    invalidate,
    measure_golden,
    mmrv,
    pareto_front,
    pearson,
    probe_reproducibility,
    record_validity,
    remeasure,
    replay_sample,
    run_experiment,
    seed_record,
    trial_key,
    usable_as_evidence,
)
from agentic_robotics.engineering.backends import platform_key
from agentic_robotics.engineering.optuna_adapter import OptunaSampler
from agentic_robotics.engineering.process import BackendProcessError, handle
from agentic_robotics.engineering.search import CandidateSummary, Direction
from agentic_robotics.provenance import ActivityStatus, ExperimentRecord, Project

pytestmark = pytest.mark.req("REQ04", "G04")
traceability = pytest.mark.req("REQ02", "G03")
reproducible = pytest.mark.req("F05")

TP = TimePolicy(physics_dt=0.01, control_dt=0.05)
GAINS = {"kp": 4.0, "kd": 2.0}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path, name="eng")


def spec(**kw: Any) -> ExperimentSpec:
    fields: dict[str, Any] = {"experiment_id": "exp-1", "hypothesis": "PD settles",
                              "time_policy": TP, "config": dict(GAINS), "seed": 7,
                              "episodes": 3, "steps": 40}
    fields.update(kw)
    return ExperimentSpec(**fields)


# -- W3.1 backends and time policy ----------------------------------------------------
def test_time_policy_enforces_integer_decimation() -> None:
    assert TP.decimation == 5
    with pytest.raises(SchemaError, match="integer multiple"):
        TimePolicy(physics_dt=0.01, control_dt=0.025)
    with pytest.raises(SchemaError):
        TimePolicy(physics_dt=0.0, control_dt=0.05)
    doc = BackendInfo(backend="x", time_policy=TP).to_dict()
    doc["time_policy"]["control_dt"] = 0.033
    with pytest.raises(SchemaError):
        BackendInfo.from_dict(doc)
    assert TimePolicy(physics_dt=0.01, control_dt=0.05, episode_length_s=1.0).episode_steps == 20


def test_free_running_or_variable_step_is_marked_non_reproducible() -> None:
    free = TimePolicy(physics_dt=0.01, control_dt=0.05, lockstep=False)
    assert not free.reproducible and "free-running" in free.non_reproducible_reasons()[0]
    info = PointMassBackend().load({}, free)
    assert not info.reproducible
    run = run_experiment(PointMassBackend(), spec(time_policy=free))
    assert any("free-running" in lim for lim in run.record.limitations)
    with pytest.raises(UnsupportedFeature, match="variable-step"):
        PointMassBackend().load({}, TimePolicy(physics_dt=0.01, control_dt=0.05,
                                               variable_step=True))


def test_capability_flags_default_to_false_and_unknown() -> None:
    caps = BackendCapabilities()
    assert caps.determinism == DeterminismClass.UNKNOWN
    assert not (caps.supports_snapshot or caps.supports_lockstep or caps.supports_rng_state
                or caps.supports_variable_step or caps.vectorized)
    assert caps.fidelity_of("contact").value == "unknown"
    assert PointMassBackend.capabilities.fidelity_of("contact").value == "none"


def test_unsupported_features_raise_never_fall_back() -> None:
    with pytest.raises(UnsupportedFeature) as exc:
        PointMassBackend().load({}, TP, features=["contact"])
    assert exc.value.details["features"] == ["contact"]
    with pytest.raises(SchemaError, match="unknown scenario keys"):
        PointMassBackend().load({"gravity": 9.8}, TP)


class GpuFallbackBackend(PointMassBackend):
    """Asked for a GPU, runs on the CPU, and says so."""

    def load(self, scenario: Mapping[str, Any], time_policy: TimePolicy, *,
             features: Iterable[str] = (), device: str = "cpu") -> BackendInfo:
        return super().load(scenario, time_policy, features=features).evolve(
            requested_device=device, actual_device="cpu")


def test_backend_info_records_actual_device_and_resolved_timing() -> None:
    run = run_experiment(GpuFallbackBackend(), spec(device="cuda:0"))
    info = BackendInfo.from_dict(run.record.backend_info)
    assert info.fell_back and info.requested_device == "cuda:0"
    assert info.resolved_physics_dt == 0.01 and info.resolved_output_interval == 0.05
    assert any("cuda:0" in lim for lim in run.record.limitations)


def test_out_of_process_backend_matches_in_process() -> None:
    argv = [sys.executable, "-m", "agentic_robotics.engineering.serve", "pointmass"]
    remote = ProcessBackend(SubprocessTransport(argv))
    try:
        assert remote.capabilities == PointMassBackend.capabilities
        out = run_experiment(remote, spec())
        assert out.info.out_of_process
        with pytest.raises(UnsupportedFeature):
            remote.load({}, TP, features=["gpu"])
    finally:
        remote.close()
    local = run_experiment(PointMassBackend(), spec())
    assert out.record.physics_fingerprint == local.record.physics_fingerprint


class LoopbackTransport:
    def __init__(self, protocol: str = "air.backend/1") -> None:
        self.backend = PointMassBackend()
        self.protocol = protocol

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        reply = handle(self.backend, json.loads(json.dumps(message)))
        if message["op"] == "hello":
            reply["result"]["protocol"] = self.protocol
        return reply

    def close(self) -> None:
        pass


def test_process_protocol_rejects_other_protocols_and_unknown_ops() -> None:
    with pytest.raises(BackendProcessError, match="different protocol"):
        ProcessBackend(LoopbackTransport("other/9"))
    assert handle(PointMassBackend(), {"id": 1, "op": "fly"})["ok"] is False
    pb = ProcessBackend(LoopbackTransport())
    pb.load({}, TP)
    pb.reset(1)
    snap = pb.snapshot()
    first = pb.step(GAINS)
    pb.restore(snap)
    assert pb.step(GAINS) == first


# -- W3.2 seeds and replay --------------------------------------------------------------
@reproducible
def test_seed_record_sources_and_stable_derivation() -> None:
    r = seed_record(7, episodes=3, seeded_rngs=["random"])
    assert r.source == SeedSource.USER and r.per_episode[1] == derive_seed(7, "episode", 1)
    assert seed_record(7, episodes=5, seeded_rngs=["random"]).per_episode[:3] == r.per_episode
    drawn = seed_record(None, episodes=1, seeded_rngs=["random"])
    assert drawn.source == SeedSource.DRAWN and drawn.root is not None
    unset = seed_record(7, episodes=2, seeded_rngs=[])
    assert unset.source == SeedSource.UNSET and not unset.per_episode
    vec = seed_record(1, episodes=1, envs=4, seeded_rngs=["random"])
    assert len(set(vec.per_env)) == 4


@reproducible
def test_run_records_seeds_platform_runner_and_fingerprint() -> None:
    run = run_experiment(PointMassBackend(), spec(acceptance={"final_error": ("<=", 0.5)}))
    r = run.record
    assert r.seeds is not None and r.seeds.seeded_rngs and len(r.seeds.per_episode) == 3
    assert r.seeds.sample_states.startswith("sha256:")
    assert r.platform == platform_key() and r.runner_version == "1"
    assert r.accepted is True and r.physics_fingerprint.startswith("sha256:")
    again = run_experiment(PointMassBackend(), spec())
    assert again.record.physics_fingerprint == r.physics_fingerprint
    assert ExperimentRecord.from_json(r.to_json()) == r


class SequentialRngBackend(PointMassBackend):
    """Seeds its generator only once; later episodes continue the stream, so a sample
    can be replayed alone only from its RNG snapshot (Drake)."""

    _seeded = False

    def reset(self, seed: int | None) -> dict[str, Any]:
        state = self._rng.getstate()
        obs = super().reset(seed)
        if self._seeded:
            self._rng.setstate(state)
        self._seeded = True
        return obs


@reproducible
def test_one_failing_sample_replays_alone_from_its_rng_snapshot() -> None:
    s = spec(episodes=4, scenario={"noise": 0.5})
    run = run_experiment(SequentialRngBackend(), s)
    ok, metrics = replay_sample(SequentialRngBackend(), s, run.samples[3])
    assert ok and metrics == run.samples[3].metrics
    # without the snapshot the stream starts over and the replay diverges
    s0 = run.samples[3]
    fresh = SequentialRngBackend()
    fresh.load(s.scenario, TP)
    fresh.set_rng_state = lambda state: None  # type: ignore[method-assign]
    ok_without, _ = replay_sample(fresh, s, s0)
    assert not ok_without


def test_experiment_record_1_0_documents_still_load_and_unknown_fields_fail() -> None:
    old = {"$schema": "ar.experiment_record/1.0", "experiment_id": "e", "hypothesis": "h"}
    assert ExperimentRecord.from_dict(old).seeds is None
    with pytest.raises(SchemaError, match="unknown fields"):
        ExperimentRecord.from_dict({**old, "seedz": 1})
    with pytest.raises(SchemaError):
        ExperimentRecord.from_dict({**old, "seeds": {"root": 1, "source": "guessed"}})


# -- reproducibility probe ------------------------------------------------------------
class JitterBackend(PointMassBackend):
    """Tiny run-to-run noise: reproducible only within tolerance."""

    _calls = 0

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        obs = super().step(action)
        JitterBackend._calls += 1
        obs["x"] += 1e-12 * (JitterBackend._calls % 3)
        return obs


class GlobalRandomBackend(PointMassBackend):
    """Draws from an unseeded generator: differs."""

    capabilities = BackendCapabilities(determinism=DeterminismClass.BITWISE,
                                       supports_lockstep=True)

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        obs = super().step(action)
        obs["x"] += random.SystemRandom().random()
        return obs


class BufferReuseBackend(PointMassBackend):
    """Returns the same dict every step."""

    def __init__(self) -> None:
        super().__init__()
        self._buf: dict[str, Any] = {}

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        self._buf.clear()
        self._buf.update(super().step(action))
        return self._buf


class LossyRestoreBackend(PointMassBackend):
    """Declares integration-level restore but forgets the RNG state."""

    def restore(self, snapshot: Any) -> None:
        state = self._rng.getstate()
        super().restore(snapshot)
        self._rng.setstate(state)


@reproducible
def test_probe_measures_exact_tolerance_and_differs() -> None:
    exact = probe_reproducibility(PointMassBackend, TP, config=GAINS, seed=3)
    assert exact.outcome == Reproducibility.EXACT and exact.ok
    assert exact.rng_state_match and exact.restore_check == "match"
    jitter = probe_reproducibility(JitterBackend, TP, config=GAINS, seed=3, tolerance=1e-9)
    assert jitter.outcome == Reproducibility.WITHIN_TOLERANCE
    assert any("declared bitwise_same_host" in f for f in jitter.findings)
    differs = probe_reproducibility(GlobalRandomBackend, TP, config=GAINS, seed=3)
    assert differs.outcome == Reproducibility.DIFFERS and not differs.ok
    assert any("no seeded RNG" in f for f in differs.findings)


@reproducible
def test_probe_detects_buffer_reuse_and_lossy_restore() -> None:
    reuse = probe_reproducibility(BufferReuseBackend, TP, config=GAINS, seed=3)
    assert reuse.buffer_reuse and not reuse.ok
    lossy = probe_reproducibility(LossyRestoreBackend, TP, config=GAINS, seed=3,
                                  scenario={"noise": 0.5})
    assert lossy.restore_check == "differs" and not lossy.ok


# -- W3.3 golden runs -------------------------------------------------------------------
@traceability
def test_golden_run_is_re_measured_per_platform(tmp_path: Path) -> None:
    store = GoldenStore(tmp_path / "golden")
    g = measure_golden(PointMassBackend, "settle", TP, config=GAINS, seed=1, steps=30)
    path = store.save(g)
    assert platform_key() in path.as_posix()
    assert [c.status for c in store.check(PointMassBackend)] == [GoldenStatus.MATCH]
    tampered = g.evolve(fingerprint="sha256:" + "0" * 64)
    assert check_golden(PointMassBackend, tampered).status == GoldenStatus.MISMATCH
    elsewhere = g.evolve(platform="linux-aarch64-cpython3.12.0")
    assert check_golden(PointMassBackend, elsewhere).status == GoldenStatus.NOT_COMPARABLE
    other = GoldenStore(tmp_path / "other")
    other.save(elsewhere)
    assert [c.status for c in other.check(PointMassBackend)] == [GoldenStatus.MISSING]
    pinned = g.evolve(backend_version="0.9")
    assert check_golden(PointMassBackend, pinned).status == GoldenStatus.NOT_COMPARABLE


@traceability
def test_results_compare_only_within_platform_and_versions() -> None:
    a = run_experiment(PointMassBackend(), spec()).record
    b = run_experiment(PointMassBackend(), spec(config={"kp": 6.0, "kd": 2.0})).record
    assert set(compare_results(a, b)) >= {"energy", "final_error"}
    with pytest.raises(NotComparable):
        compare_results(a, b.evolve(platform="linux-aarch64"))
    with pytest.raises(NotComparable):
        compare_results(a, b.evolve(runner_version="2"))


@traceability
def test_remeasure_reruns_from_the_record_instead_of_trusting_scores(
        project: Project) -> None:
    run_experiment(PointMassBackend(), spec(), project=project)
    stored = experiment_records(project)[0]
    assert remeasure(PointMassBackend, stored, project=project).fingerprint_match
    doctored = stored.evolve(results={**stored.results, "energy": 0.0},
                             physics_fingerprint="sha256:" + "1" * 64)
    result = remeasure(PointMassBackend, doctored)
    assert not result.fingerprint_match and result.measured["energy"] != 0.0
    assert any(a.title == "re-measure exp-1" for a in project.activities())


# -- W3.4 search ------------------------------------------------------------------------
def make_search(project: Project | None = None, **kw: Any) -> Search:
    fields: dict[str, Any] = {
        "factory": PointMassBackend, "time_policy": TP,
        "space": [Param("kp", low=0.5, high=8.0), Param("kd", low=0.1, high=4.0)],
        "objectives": [Objective("settle_time"), Objective("energy")],
        "constraints": [Constraint("overshoot", "<=", 0.2)],
        "scenarios": {"nominal": {"target": 1.0}}, "seeds": [1, 2, 3], "steps": 40,
        "project": project}
    fields.update(kw)
    return Search("s1", **fields)


def test_trial_key_is_config_scenario_seed_budget() -> None:
    k = trial_key({"kp": 1}, "nominal", 1, {"steps": 40})
    assert k == trial_key({"kp": 1}, "nominal", 1, {"steps": 40})
    assert len({k, trial_key({"kp": 1}, "nominal", 2, {"steps": 40}),
                trial_key({"kp": 1}, "other", 1, {"steps": 40}),
                trial_key({"kp": 1}, "nominal", 1, {"steps": 80})}) == 4


def test_search_compares_candidates_on_the_same_seeds_and_records_trials(
        project: Project) -> None:
    record = make_search(project).run(6)
    assert record.sampler == "random" and record.sampler_seed == 0
    assert len(record.trials) == 18 and len({t.trial_key for t in record.trials}) == 18
    for c in record.candidates:
        seeds = sorted(t.seed for t in record.trials if t.candidate == c.candidate)
        assert seeds == [1, 2, 3]
    feasible = {c.candidate for c in record.candidates if c.feasible}
    assert set(record.pareto) <= feasible and record.pareto
    trials = [a for a in project.activities() if a.params.get("search_id") == "s1"
              and a.parent_id]
    assert len(trials) == 18 and all(a.parent_id == record.activity_id for a in trials)
    stored = [r for r in experiment_records(project) if r.search_id == "s1"]
    assert {r.status for r in stored} == {"complete"}
    assert all(r.trial_key and r.config for r in stored)


class ExplodingBackend(PointMassBackend):
    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        if float(action["kp"]) > 5:
            raise ArithmeticError("integrator diverged")
        return super().step(action)


def test_every_trial_ends_terminal_with_a_reason(project: Project) -> None:
    def prune(trials: Any) -> str | None:
        return "overshoot already above 0.5" if trials[-1].values["overshoot"] > 0.5 else None

    search = make_search(project, factory=ExplodingBackend, pruner=prune, max_trials=14,
                         sampler=GridSampler(),
                         space=[Param("kp", low=1.0, high=7.0, grid_points=3),
                                Param("kd", low=0.0, high=0.0, grid_points=1)])
    record = search.run(10)
    statuses = {t.status for t in record.trials}
    assert TrialStatus.FAILED in statuses
    assert all(t.reason for t in record.trials if t.status != TrialStatus.COMPLETE)
    failed = [t for t in record.trials if t.status == TrialStatus.FAILED]
    assert "integrator diverged" in failed[0].reason
    by_status = {c.status for c in record.candidates}
    assert TrialStatus.FAILED in by_status
    assert all(not c.feasible for c in record.candidates if c.status != TrialStatus.COMPLETE)
    stored = {r.trial_key: r for r in experiment_records(project)}
    for t in record.trials:
        assert stored[t.trial_key].status == t.status.value
    failed_acts = [a for a in project.activities() if a.status == ActivityStatus.FAILED]
    assert failed_acts


def test_pruned_and_abandoned_trials_are_recorded(project: Project) -> None:
    record = make_search(project, pruner=lambda ts: "first seed too slow",
                         max_trials=4).run(5)
    statuses = [t.status for t in record.trials]
    assert TrialStatus.PRUNED in statuses and TrialStatus.ABANDONED in statuses
    reasons = {t.reason for t in record.trials if t.status == TrialStatus.ABANDONED}
    assert any("candidate pruned" in r for r in reasons)
    assert record.status == "abandoned" and "budget" in record.status_reason
    assert not record.pareto


def test_pareto_front_needs_feasible_complete_candidates() -> None:
    objs = [Objective("a"), Objective("b", Direction.MAXIMIZE)]
    c = [CandidateSummary("x", {}, TrialStatus.COMPLETE, values={"a": 1, "b": 1}, feasible=True),
         CandidateSummary("y", {}, TrialStatus.COMPLETE, values={"a": 2, "b": 2}, feasible=True),
         CandidateSummary("z", {}, TrialStatus.COMPLETE, values={"a": 2, "b": 0.5},
                          feasible=True),
         CandidateSummary("w", {}, TrialStatus.COMPLETE, values={"a": 0, "b": 9},
                          feasible=False),
         CandidateSummary("v", {}, TrialStatus.PRUNED, values={"a": 0, "b": 9})]
    assert [p.candidate for p in pareto_front(c, objs)] == ["x", "y"]


def test_release_is_picked_from_revalidated_pareto_on_held_out_seeds(
        project: Project) -> None:
    search = make_search(project)
    record = search.run(6)
    with pytest.raises(SchemaError, match="disjoint"):
        search.select_release(record, [1, 99])
    sel = search.select_release(record, [101, 102])
    assert set(sel.selected) <= set(sel.considered) == set(record.pareto)
    assert set(sel.selected) | set(sel.rejected) == set(sel.considered)
    held = [t for t in project.activities() if t.parent_id == sel.activity_id]
    assert len(held) == 2 * len(sel.considered)
    # a constraint that only bites on the held-out seeds rejects the candidate
    strict = make_search(None)
    rec = strict.run(6)
    strict.constraints = (Constraint("overshoot", "<=", -1.0),)
    tightened = strict.select_release(rec, [101])
    assert not tightened.selected and all("infeasible" in r for r in tightened.rejected.values())


def test_repeated_proposals_are_not_run_twice() -> None:
    class Repeat(RandomSampler):
        def propose(self, space: Any, history: Any) -> dict[str, Any]:
            return {"kp": 3.0, "kd": 1.0}

    record = make_search(sampler=Repeat()).run(4)
    assert len(record.candidates) == 1 and len(record.trials) == 3


class FakeTrial:
    def __init__(self, number: int) -> None:
        self.number = number
        self.user_attrs: dict[str, Any] = {}

    def suggest_float(self, name: str, low: float, high: float, log: bool = False) -> float:
        return low + (high - low) * ((self.number * 7 + len(name)) % 10) / 10

    def suggest_int(self, name: str, low: int, high: int, log: bool = False) -> int:
        return low

    def suggest_categorical(self, name: str, choices: list[Any]) -> Any:
        return choices[self.number % len(choices)]

    def set_user_attr(self, key: str, value: Any) -> None:
        self.user_attrs[key] = value


class FakeState:
    COMPLETE, PRUNED, FAIL = "COMPLETE", "PRUNED", "FAIL"


class FakeStudy:
    def __init__(self) -> None:
        self.asked: list[FakeTrial] = []
        self.told: list[tuple[int, Any, Any]] = []

    def ask(self) -> FakeTrial:
        self.asked.append(FakeTrial(len(self.asked)))
        return self.asked[-1]

    def tell(self, trial: FakeTrial, values: Any = None, state: Any = None) -> None:
        assert (values is None) != (state is None)
        self.told.append((trial.number, values, state))


def test_optuna_adapter_keeps_air_as_system_of_record(project: Project) -> None:
    study = FakeStudy()
    sampler = OptunaSampler(study=study, trial_state=FakeState, seed=5, version="4.0.0")
    record = make_search(project, sampler=sampler,
                         pruner=lambda ts: "slow" if ts[-1].values["energy"] > 20 else None
                         ).run(4)
    assert record.sampler == "optuna" and record.sampler_seed == 5
    assert "4.0.0" in record.sampler_version
    assert len(study.told) == len(study.asked) == 4
    for number, values, state in study.told:
        cand = record.candidates[number]
        if cand.status == TrialStatus.COMPLETE:
            assert values == [cand.values["settle_time"], cand.values["energy"]]
        else:
            assert state == FakeState.PRUNED
    assert len([r for r in experiment_records(project) if r.search_id == "s1"]) == \
        len(record.trials)


def test_real_optuna_when_installed() -> None:
    pytest.importorskip("optuna")
    sampler = OptunaSampler(directions=["minimize", "minimize"], seed=0)
    record = make_search(sampler=sampler).run(3)
    assert record.sampler_version.startswith("optuna")


# -- evaluation validity ------------------------------------------------------------------
@traceability
def test_mmrv_and_pearson() -> None:
    assert mmrv([0.8, 0.5, 0.3], [0.7, 0.4, 0.2]) == 0.0
    assert math.isclose(mmrv([0.5, 0.8], [0.7, 0.4]), 0.3)
    assert math.isclose(pearson([1, 2, 3], [2, 4, 6]) or 0, 1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) is None
    with pytest.raises(SchemaError):
        mmrv([1.0], [1.0])


@traceability
def test_benchmark_scores_count_only_after_validity_and_can_be_invalidated(
        project: Project) -> None:
    run = run_experiment(PointMassBackend(), spec(), project=project)
    ok, why = usable_as_evidence(project, run.record, "settle-bench")
    assert not ok and "no valid sim-to-real" in why[0]
    weak = assess_validity("settle-bench", {"a": 0.9, "b": 0.2}, {"a": 0.1, "b": 0.8},
                           real_evidence=["ledger:3"])
    assert not weak.valid and len(weak.reasons) >= 2
    good = assess_validity("settle-bench", {"a": 0.8, "b": 0.5, "c": 0.3},
                           {"a": 0.7, "b": 0.4, "c": 0.2, "d": 0.9},
                           real_evidence=["field-trial-2026-10"], backend="pointmass",
                           backend_version="1.0")
    assert good.valid and good.mmrv == 0.0 and set(good.real_scores) == {"a", "b", "c"}
    record_validity(project, weak)
    record_validity(project, good)
    assert usable_as_evidence(project, run.record, "settle-bench") == (True, ())
    invalidate(project, run.record.experiment_id, by="person:ana", reason="wrong target")
    ok, why = usable_as_evidence(project, run.record, "settle-bench")
    assert not ok and "wrong target" in why[0]
    assert experiment_records(project)  # nothing was deleted
    invalidate(project, good.validity_id, by="person:ana", reason="real data mislabeled")
    other = run_experiment(PointMassBackend(), spec(experiment_id="exp-2")).record
    assert not usable_as_evidence(project, other, "settle-bench")[0]
    with pytest.raises(SchemaError):
        invalidate(project, "exp-2", by="", reason="x")
    assert project.ledger.verify().ok


# -- CLI ---------------------------------------------------------------------------------
@traceability
def test_cli_experiment_commands(project: Project, tmp_path: Path,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    root = str(project.root)
    assert cli(["experiment", "backends"]) == 0
    assert cli(["experiment", "run", "--project", root, "--record", "--id", "exp-cli",
                "--config", "kp=4", "--config", "kd=2", "--seed", "3", "--steps", "40",
                "--episodes", "2", "--accept", "final_error<=0.5"]) == 0
    assert "exp-cli" in capsys.readouterr().out
    assert cli(["experiment", "probe", "--project", root, "--record", "--config", "kp=4"]) == 0
    assert "exact" in capsys.readouterr().out
    gdir = str(tmp_path / "golden")
    assert cli(["experiment", "golden", "record", "--dir", gdir, "--name", "g1",
                "--steps", "20"]) == 0
    assert cli(["experiment", "golden", "check", "--dir", gdir, "--project", root,
                "--record"]) == 0
    assert cli(["experiment", "search", "--project", root, "--record", "--id", "s-cli",
                "--param", "kp=0.5:8", "--param", "kd=0.1:4", "--objective", "settle_time",
                "--objective", "energy:min", "--constraint", "overshoot<=0.05",
                "--seeds", "1,2", "--held-out", "9", "--candidates", "4",
                "--steps", "40"]) == 0
    out = capsys.readouterr().out
    assert "s-cli" in out and "release:" in out
    assert cli(["experiment", "remeasure", "exp-cli", "--project", root]) == 0
    assert cli(["experiment", "invalidate", "exp-cli", "--project", root, "--by",
                "person:ana", "--reason", "bad scenario"]) == 0
    capsys.readouterr()
    assert cli(["experiment", "list", "--project", root]) == 0
    assert "INVALID exp-cli" in capsys.readouterr().out
    assert cli(["experiment", "validity", "--project", root, "--record", "--benchmark", "b",
                "--sim", "a=0.8", "--sim", "b=0.5", "--sim", "c=0.3", "--real", "a=0.7",
                "--real", "b=0.4", "--real", "c=0.2", "--evidence", "trial-1"]) == 0
    assert cli(["experiment", "validity", "--benchmark", "b", "--sim", "a=0.8",
                "--real", "a=0.7"]) == 1
    assert cli(["experiment", "run", "--feature", "contact"]) == 2
