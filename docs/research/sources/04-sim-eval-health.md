# Research source 04: simulation, optimization, health monitoring, prognostics

Raw findings from the 2026-10-22 survey (researcher report, condensed). **[U]** marks
claims that were not verified. The synthesis is in
[../oss-adoption-plan.md](../oss-adoption-plan.md).

## Simulation and emulation

| Project | License | Mechanism (files read) | Mode |
|---|---|---|---|
| Isaac Lab | BSD-3 (runtime needs Isaac Sim, separate terms [U]) | `envs/manager_based_env_cfg.py`: `seed=None` means "not set"; `decimation`; `events` with named randomization terms bound to modes; `episode_length_s`; finite-horizon truncation | adopt pattern; optional out-of-process adapter |
| ManiSkill3 | Apache-2.0 | `envs/sapien_env.py`: asserts `sim_freq % control_freq == 0`; **two-level RNG** (`_main_seed` to `_episode_seed`); `enhanced_determinism`; flat `set_state` drops controller state (lossy restore) | adopt pattern; optional adapter |
| RoboVerse / MetaSim | Apache-2.0 | `metasim/sim/base.py` `BaseSimHandler`: four abstract methods and **declared capability flags** (`set_states_restores_velocities`, `get_states_honours_env_ids`, ...); `env_step_s = physics_dt * decimation` | strongest source for AIR's backend interface |
| MuJoCo | Apache-2.0 | state taxonomy PHYSICS ⊂ FULLPHYSICS ⊂ INTEGRATION (including warmstart); identical integration state gives identical output; with sleeping enabled, only a full `mjData` copy restores state | adopt state-completeness levels; MuJoCo is the first real backend |
| MJX | Apache-2.0 | JAX version supports a subset of features and **raises on unsupported ones**; Warp version is NVIDIA-only | treat as distinct backends; fail loudly |
| Genesis | Apache-2.0 | multi-solver; README makes **no determinism claim** | later adapter; determinism unknown until measured |
| Gazebo gz-sim | Apache-2.0 | `ServerConfig`: seed, update rate, engine, log record/playback paths | optional adapter; log files as evidence artifacts |
| Renode | MIT | `renode-test` with Robot Framework; `Provides`/`Requires` named snapshots | optional firmware/MCU emulation adapter; snapshot pattern for bounded tests |
| FMPy | BSD-2 | FMI 1/2/3, co-simulation and model exchange | optional emulation backend; FMU GUID as model identity |
| DISCOVERSE (Tsinghua) | MIT | 3D Gaussian splatting rendering on MuJoCo physics | later |
| AgiBot Genie Sim 3.0 | license unclear [U] | Isaac Sim with LLM/VLM-scored benchmark | avoid until license read; LLM-judged scores are not acceptance evidence |

## Optimization

- **Optuna** (MIT [U]). `FrozenTrial` fields. `set_constraint`: feasible when all
  values ≤ 0. `best_trials` returns the feasible Pareto set only. `enqueue_trial` and
  `add_trial` exist. Heartbeats mark stale trials FAIL. **A study does not persist the
  sampler, its seed, or the Optuna version**, so AIR must record them.
- **Ax/BoTorch** (MIT [U]): a possible second optimizer adapter.

## Health, diagnostics, anomaly detection

- **ROS diagnostics** (BSD).
  - Updater: `FrequencyStatusParam(min, max, tolerance, window)` and `TimeStampStatusParam`.
  - Aggregator (`generic_analyzer.cpp`): a missing expected item is ERROR "Missing";
    timeout gives **STALE**; a group takes the worst child's level; unmatched items go to
    an "Other" analyzer; critical items are published immediately.
- **Linux PSI**: `/proc/pressure/{cpu,memory,io}` reports some/full with avg10/60/300,
  plus poll triggers. Readable with the stdlib.
- **jetson-stats** (AGPL-3.0): talks to a root service socket. Use it as an optional
  adapter and degrade gracefully when the service is missing.
- **PyOD** (BSD-2) batch detectors. **river** (BSD-3) ADWIN streaming drift detection:
  `update(x)`, `drift_detected`, grace period.
- **Merlion**: archived. Adopt ideas only (calibrated score, threshold, alarm, mean time
  to detect).

## Prognostics

- **NASA progpy** (NOSA 1.3, not GPL-compatible according to the FSF).
  - `predict(state, future_loading_eqn, t0, dt, horizon, events)` returns
    `PredictionResults` with `time_of_event` distributions.
  - `UncertainData`: `mean`/`cov`/`percentage_in_bounds`, and metrics such as relative
    accuracy and monotonicity.
- **Do not vendor or link it into the AGPL core.** Clean-room the ideas. Any adapter
  needs a legal review first [U].

## Fault injection

- **Chaos Toolkit** (Apache-2.0): a steady-state hypothesis with tolerances. If the steady
  state is not met, the method must not run. Rollbacks run with a strategy
  (always/never/deviated).
- **ros2_fault_injection** (repo and license not found [U]): YAML with injectors, faults,
  a schedule and assertions (`topic_hz`, `twist_stopped`).

## Chinese ecosystem

No robotics-specific open-source fault-diagnosis or predictive-maintenance framework was
found on Gitee or AtomGit. openEuler SIG-ROS packages ROS diagnostics tooling.
Chinese-origin simulators of note are on GitHub: DISCOVERSE (MIT), Genesis (Apache-2.0)
and Genie Sim (license unclear). Read this as "no adoptable framework found", not "none
exist".

## Adoption items

| # | Pattern | AIR module | Req / gate | P | Effort |
|---|---|---|---|---|---|
| A1 | `ExperimentBackend` protocol with declared fidelity and restore flags; raise on unsupported features | new `engineering/backends.py` | REQ04, REQ03; G03, G04 | P0 | M |
| A2 | `SeedRecord`: root, per-episode, per-env, source (user/derived/drawn/unset), RNGs actually seeded; determinism **measured** by a reproducibility probe | `provenance/model.py` ExperimentRecord | REQ04; G04 | P0 | S |
| A3 | `TimePolicy`: `physics_dt`, `control_dt` (integer decimation), episode length, clock domain, lockstep flag | `engineering/backends.py` | REQ04; G03 | P0 | S |
| A4 | Snapshot/restore completeness levels plus a lossy list | backend API | REQ04, REQ14 | P1 | M |
| A5 | Fake and MuJoCo reference backends; FMPy emulation backend | `integrations/` | REQ03, REQ04 | P1 | M |
| A6 | Optuna adapter with AIR as the system of record: one study = one SearchRecord (sampler, seed, version); one trial = one ExperimentRecord, failed and pruned trials included | `engineering/search.py` | REQ04; G04, G10 | P0 | M |
| A7 | Release manifest = accepted experiments plus Pareto rationale, re-validated on held-out seeds | `engineering/release.py` | REQ04; G10 | P1 | M |
| A8 | Diagnostic aggregation: expected-component manifest, STALE level, worst-of rollup, "other" bucket, critical fast path | new `health/aggregate.py` | REQ13, REQ14; G11 | P0 | M |
| A9 | Rate and latency monitors with window and tolerance | `health/monitors.py` | REQ13 | P0 | S |
| A10 | PSI and cgroup v2 probes; jtop as an optional adapter | `health/probes/` | REQ13; G11 | P1 | S/M |
| A11 | Streaming detector protocol `update(x) -> Detection | None` | `health/detectors.py` | REQ14 | P1 | S |
| A12 | Incident pipeline: steady-state gate, bounded tests within admission budgets, rollbacks, verified recovery or human handoff | new `incidents/` | REQ14, REQ11; G13 | P0 | L |
| A13 | `PrognosticEstimate` (event_state interval, time-of-event distribution, loading assumption, horizon, censoring, validation) feeding capability and maintenance; gate on p5, not the mean | `maintenance/prognostics.py` | REQ16; G13 | P1 | M |
| A14 | Fault-injection scenarios as experiments | `engineering/`, `scenarios.py` | REQ14; G13 | P2 | M |
| A15 | Randomization recorded as named terms with modes | ExperimentRecord | REQ04 | P2 | S |

## Proposed shapes (summary)

- `BackendInfo`: backend and probed version; build ids; fidelity per aspect; determinism
  (bitwise / bitwise_same_host / statistical / unknown); restore level; restore lossy
  list; unsupported features.
- `ExperimentBackend`: `info`, `load` (raises on unsupported features), `reset(seed)`,
  `step`, `snapshot`, `restore`, `metrics`, `close`. Heavy simulators run out of process.
- Prognostics map into `CapabilityProfile` (degraded when p5 time-of-event is below the
  mission duration) and `MaintenanceRecord` (predictive maintenance cites the estimate;
  the prediction error is recorded after service).
- Incident pipeline steps: probes, aggregator, detectors, incident (with a novelty
  flag), hypotheses (agent-proposed ones marked as such), bounded tests, recovery or
  takeover, then a new known signature.

## Anti-patterns

- trusting the optimizer's store as provenance
- omitting `seed=None`
- claiming determinism from a seed alone
- a single generic "sim" fidelity label
- silently falling back on unsupported features
- treating STALE as OK
- point RUL estimates without uncertainty
- LLM-judged benchmarks used as acceptance evidence
- depending on archived libraries or on NOSA code in the core
- comparing optimizations on free-running simulators without declaring timing as
  non-reproducible
