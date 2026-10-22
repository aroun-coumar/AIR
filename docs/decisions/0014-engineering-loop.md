# 0014. Engineering and optimization loop (baseline stage 4)

Status: Accepted
Date: 2026-10-22

## Context
Stage 4 of the baseline needs experiments that can be interpreted, compared and
reproduced, and optimization that leaves evidence a release decision can rest on. AIR
had only `ExperimentRecord` 1.0: a free-text backend label, one optional seed, and
results that nobody re-measured. The survey (`docs/research/oss-adoption-plan.md` W3.1 to
W3.4, `catalog-backlog.md` amendments, `sources/04-sim-eval-health.md`,
`sources/06-microduck.md`) found the same failure modes across simulators and optimizers:
determinism claimed from a seed alone, one generic "sim" fidelity label, silent fallback
on unsupported features, optimizer stores treated as provenance (an Optuna study does not
persist its sampler, seed or version), and simulator benchmarks used as evidence with no
check against real results.

This change adds `agentic_robotics.engineering` (stdlib only) and `air experiment`.

## Decisions
1. **Declared backends (W3.1).** `ExperimentBackend` is a protocol (`load`, `reset`,
   `step`, `snapshot`, `restore`, `rng_state`, `set_rng_state`, `metrics`, `close`).
   - `BackendCapabilities` declares fidelity *per aspect*, a determinism class
     (`unknown` / `statistical` / `bitwise_same_host` / `bitwise`), a restore level
     (MuJoCo taxonomy: `physics` ⊂ `full_physics` ⊂ `integration`), a lossy-restore list,
     supported and unsupported features, and the RNGs it seeds. **Every flag defaults to
     false and determinism to `unknown`.**
   - Requesting an unsupported feature, variable step on a fixed-step backend, or
     lockstep on a backend that cannot do it raises `UnsupportedFeature`. Never a fallback.
   - `BackendInfo` (`ar.backend_info/1.0`) records what actually ran: requested vs actual
     device, resolved physics step and output interval, features, platform, and the
     reasons the run is non-reproducible.
   - `TimePolicy`: `physics_dt`, `control_dt`, episode length, lockstep, variable step,
     clock domain. `control_dt` must be an integer multiple of `physics_dt`; this is
     enforced when the policy is built *and* when a document is parsed. Free-running and
     variable-step runs are allowed but always recorded as non-reproducible.
   - Out-of-process backends speak the JSON-lines protocol `air.backend/1`
     (`engineering/process.py`, `python -m agentic_robotics.engineering.serve <name>`).
     Error codes survive the process boundary, so `UnsupportedFeature` still raises.
     The transport is injectable; tests run the real subprocess and a loopback.
   - A deterministic pure-Python reference backend (`pointmass`: a PD-driven point mass
     with seeded disturbances) makes every path testable offline.
2. **Seeds and measured determinism (W3.2).** `SeedRecord` (in `provenance/model.py`):
   root, per-episode and per-env seeds, source (`user` / `derived` / `drawn` / `unset`),
   the RNGs actually seeded, the derivation, and a digest of the per-sample RNG
   snapshots.
   - Episode seeds are derived by hashing (`sha256(root/kind/index)`), so episode *i* has
     the same seed however many episodes run.
   - A missing seed is drawn and recorded as `drawn`. A backend that seeds nothing gives
     `unset`.
   - The RNG state at the start of every sample is stored as a project artifact, so one
     failing sample replays alone (`replay_sample`; Drake `MonteCarloSimulation`).
   - `probe_reproducibility` measures determinism with three outcomes: `exact`,
     `within_tolerance`, `differs`. It also checks the RNG state after `reset(seed)`,
     repeated reset on one instance, observation buffer reuse, and (when declared)
     snapshot/restore against the lossy list. A declared bitwise class that measures
     anything but exact is reported as a finding. Report: `ar.reproducibility_report/1.0`.
3. **Physics fingerprints and golden runs (W3.3).** A fingerprint is the SHA-256 of the
   trajectory in canonical JSON (RFC 8785 numbers), so other implementations can compute
   it.
   - `GoldenRun` (`ar.golden_run/1.0`) pins platform, backend version and
     `RUNNER_VERSION`. `GoldenStore` keeps one golden per
     `<platform>/<backend>@<version>/<name>.json`.
   - A check **re-measures** the golden. Across platform, simulator version or runner
     version the result is `not_comparable`, never a number. A platform with no golden is
     `missing`.
   - `compare_results` raises `NotComparable` across those boundaries. `remeasure` re-runs
     a stored experiment from its record instead of trusting stored scores (Microduck
     `arena recover`).
4. **Search with AIR as the system of record (W3.4).** `Search` runs candidates from a
   sampler.
   - `SearchRecord` (`ar.search_record/1.0`) stores the sampler, its seed and version,
     space, objectives, constraints, scenarios, seeds, budget, every trial and every
     candidate. Each trial is an `ExperimentRecord` child activity of the search activity.
   - Trials are keyed by `(config, scenario, seed, budget)`. A key never runs twice; a
     repeated proposal is not re-run or counted.
   - Every candidate runs on the same scenarios and seeds. A candidate with any
     non-complete trial gets no aggregate and cannot be on the Pareto set.
   - Every trial ends `complete`, `failed`, `pruned` or `abandoned`, each with a reason
     (backend error, non-finite metric, pruner reason, candidate pruned, budget exhausted).
   - Feasible means every constraint value <= 0 (Optuna convention). The Pareto set is
     computed over feasible, complete candidates only.
   - `select_release` re-runs the Pareto set on held-out seeds (which must be disjoint
     from the search seeds), keeps the ones still feasible and non-dominated, and records
     a decision activity with `ar.release_selection/1.0`. Stored search scores are not
     reused for the decision.
   - Samplers: stdlib `RandomSampler` and `GridSampler`; `OptunaSampler` behind the
     `[optuna]` extra, imported lazily. Optuna only proposes (`ask`); AIR tells it
     results (`tell(values=)` for complete, `state=PRUNED/FAIL` otherwise, verified
     against `optuna/study/_tell.py`). Tests use a fake study; a real-Optuna test skips
     when it is not installed.
5. **Evaluation validity.** A benchmark score counts as evidence only after a recorded
   sim-to-real validity assessment (`ar.benchmark_validity/1.0`).
   - The assessment computes MMRV (SimplerEnv's mean maximum rank violation) and the
     Pearson correlation in stdlib. It needs at least three policies and a reference to
     the real-robot evidence.
   - It is recorded as an evaluation activity. `usable_as_evidence` also requires the same
     backend and version.
   - `invalidate` appends `evidence.invalidated` (who, why, evidence) to the ledger.
     Experiments, assessments and benchmarks can be invalidated; nothing is deleted
     (inspect_ai, lm-eval-harness).
6. **`ExperimentRecord` 1.1** adds, all defaulted: `seeds`, `backend_info`, `config`,
   `scenario_params`, `platform`, `runner_version`, `physics_fingerprint`,
   `reproducibility`, `search_id`, `trial_key`, `status`, `status_reason`, `budget`.
   `backend_info` is a dict holding an `ar.backend_info` document, so `provenance` does
   not import `engineering`. 1.0 documents still load and unknown fields still fail.

## Consequences
- Experiments, trials, searches, probes, golden checks, re-measurements, validity
  assessments and release selections all appear in the project ledger as activities
  with generated entities. Runner, probe and search also emit trace events
  (`experiment_started`, `episode_finished`, `experiment_finished`,
  `reproducibility_probe`, `trial_finished`) to any `Trace`-like sink.
- `engineering` depends on `core` and `provenance` only. It does not import `runtime`;
  its trace sink is duck-typed.
- Real simulators (MuJoCo, Isaac, Gazebo, FMUs) plug in as `ExperimentBackend`s, in
  process or through `air.backend/1`, without new core dependencies.
- The point-mass backend claims only `bitwise_same_host`, although Python floats would
  probably give bitwise results across hosts. Golden runs measure that per platform.

## Sources (patterns adopted; no code copied)
| Source | License | What was adopted |
|---|---|---|
| RoboVerse / MetaSim `BaseSimHandler` | Apache-2.0 | declared capability flags |
| MuJoCo state taxonomy; MJX | Apache-2.0 | restore levels; raise on unsupported features |
| ManiSkill3 `sapien_env.py` | Apache-2.0 | decimation invariant; main/episode seeds; lossy restore |
| Isaac Lab `ManagerBasedEnvCfg` | BSD-3-Clause | `seed=None` recorded as unset; decimation |
| Genesis, FMPy, CARLA | Apache-2.0, BSD-2-Clause, MIT | record actual device and resolved step |
| Drake `MonteCarloSimulation` | BSD-3-Clause | per-sample RNG state for single-sample replay |
| Gymnasium `check_env` | MIT | reproducibility checks incl. RNG state |
| Microduck arena (`golden.py`, `RUNNER_VERSION`, `arena recover`) | **no license file** (ideas only) | per-platform golden runs, version-scoped comparison, re-measurement |
| Optuna (`FrozenTrial`, constraints, `best_trials`, `study/_tell.py`) | MIT | ask/tell adapter, feasibility <= 0, feasible Pareto set |
| SMAC3, Ax | BSD-3-Clause, MIT | trial keys `(config, instance, seed, budget)`; terminal statuses |
| SimplerEnv | MIT | MMRV and correlation as sim-to-real validity |
| inspect_ai, lm-eval-harness | MIT | invalidation with provenance instead of deletion |

## Not done / needs the owner
- **Real backends.** No MuJoCo, Isaac Lab, ManiSkill, Gazebo or FMPy adapter yet (A5).
  The out-of-process protocol is ready for them.
- **Vectorized backends.** Per-env seeds are recorded, but no shipped backend is
  vectorized, and the probe checks one env.
- **Optuna against a real install.** Not tested here: Optuna is not in the shared venv,
  and the brief says not to install it. Constraints reach Optuna only as the
  `air_constraints` user attribute; wiring them into `constraints_func` is left to the
  caller who builds the study.
- **Cross-platform goldens in CI.** The store supports one golden per platform, but no CI
  job records and checks them on arm64 and x86.
- **Thresholds.** The default validity thresholds (MMRV <= 0.1, Pearson >= 0.7, three
  policies) are placeholders. They need a domain decision per benchmark.
- **Requirement claims.** REQ04, G03 and G04 stay `planned`, and F05 and REQ02 stay
  `in_progress`. The owner decides claims after review (`air requirements claim`).
- **Release manifests.** No release manifest over accepted experiments yet (A7); a
  `ReleaseSelection` is the input it will need.
- **Randomization terms and fault-injection scenarios.** Not done (A14, A15).
