# 0015. Operations: health, readiness, incidents and prognostics

Status: Accepted
Date: 2026-10-22

## Context
Baseline stage 5 needs AIR to know when a robot is degraded, to keep it from being
asked for work it cannot do safely, to investigate unfamiliar failures without
improvising on the hardware, and to anticipate wear. The adoption plan items
W3.7 (health), W3.8 (incidents) and W3.9 (prognostics), with the catalog amendments
(readiness gate, diagnosis table), describe the mechanisms. Before this change AIR had
telemetry limits in the hardware catalog and a triage queue, but nothing that turned
readings into a health verdict, nothing that tied health to admission, no incident
handling, and no prognostics.

Constraints: the core stays stdlib-only and never requires hardware; the runtime never
trusts a report (ADR 0003); agents propose and the runtime disposes; everything
significant leaves evidence.

## Decisions
1. **Health aggregation** (`health/model.py`, `health/aggregate.py`;
   `ar.health_manifest/1.0`, `ar.health_report/1.0`).
   - The expected-item manifest is *derived* from a `ComponentManifest` and the
     catalog's `telemetry_limits` (component limit overrides win), so limits are not
     configured twice. Items carry the component-tree path; safety-relevant components
     are critical.
   - Levels follow ROS diagnostics numbering: OK 0, WARN 1, ERROR 2, STALE 3. The rollup
     is the plain worst-of, so STALE dominates: unknown is never better than known-bad
     for admission.
   - A never-reported expected item is ERROR "missing". An item silent past its
     `timeout_s`, or whose transport reported it lost (`lost()`: liveliness token,
     last will), is STALE. STALE is derived from facts, never configured, never OK.
   - Unexpected reporters go to an `other` bucket. They count while fresh; an unexpected
     reporter going silent is listed but does not make the robot STALE.
   - Critical items that turn ERROR or STALE go to `on_critical` listeners immediately
     (once per episode), including silence via `check_stale(now)`.
   - Monitors: `FrequencyMonitor` (min/max with tolerance over a window; no events is
     ERROR, out of band is WARN) and `LatencyMonitor` (future stamps and window max are
     ERROR, p95 is WARN), after ROS `FrequencyStatus`/`TimeStampStatus`.
   - `PsiProbe` reads `/proc/pressure/{cpu,memory,io}` with the stdlib. Absent files are
     reported *unsupported* at level STALE.
2. **Report vs gate** (`health/gating.py`). Health never commands anything. `HealthGate`
   is an ordinary admission check that `gated_checks` places immediately before
   `check_capability`; it replaces the admission context's capability report with a
   gated one, so the runtime admits against it. Gates (`CapabilityGate`) only downgrade
   (WARN/ERROR/STALE map to RESTRICTED or UNAVAILABLE) and add `health:<item>:<level>`
   limiting factors. Derived gates: a component whose type `provides` a nominal
   capability gates it; safety-relevant components gate every capability. The gated
   revision is the adapter's revision plus the number of gating-outcome changes, so it
   is monotonic and plans made against an older outcome are refused. A fall reports
   RESTRICTED (Microduck); `RobotOwnedRecoveryCheck` refuses commands for recovery the
   robot owns (`HealthManifest.robot_owned_recovery`), and incidents observe such
   recovery instead of commanding it.
3. **Readiness gate** (`health/readiness.py`, `ar.readiness_report/1.0`), after PX4
   `HealthAndArmingChecks`: pluggable checks run in a defined order (`order`, then
   `check_id`); each result has a level, the modes it blocks and failsafe flags; the
   report has `can_run` per mode. A crashing check blocks every mode (fail closed).
   `require(mode, ctx, project)` raises `NotReady` before activation and records a
   `readiness.evaluated` ledger entry. Built-in checks: health, capability, freshness,
   manifest (catalog data errors), diagnosis, prognostics.
4. **Diagnosis table** (`health/diagnosis.py`, `ar.diagnosis_table/1.0`), after
   foxBMS-2 `diag`: each fault id has a debounce threshold, a severity
   (info/warning/error/fatal), a delay before the error state, and a domain-pack failure
   class. Failures count up and passes count down; the fault is confirmed at the
   threshold, enters the error state after the delay, and clears only at zero, so a
   flapping signal cannot toggle it. `exercise(entry)` drives an entry through its life;
   the suite runs it for every built-in entry, and projects can run it on their tables
   (`air health diagnosis --exercise`).
5. **Incident pipeline** (`incidents/`, `ar.incident/1.0`, `ar.bounded_test/1.0`).
   - Detection → incident. Novelty is decided against the failure taxonomies of the
     domain packs (scoped to the component type's domains when the manifest and
     catalog are known; an explicit failure class must exist) and against signatures
     of incidents people closed as learned. Recurrences fold into the active incident.
   - Hypotheses from `agent:*`/`model:*` are marked non-authoritative. Only a bounded
     test's observations move a hypothesis to supported/refuted/inconclusive.
   - Bounded tests have the Chaos Toolkit shape: steady-state hypothesis (conditions on
     fresh observations) before and after, a method, rollbacks by strategy
     (always/never/deviated). The method runs only if the steady state held, inside an
     admission budget (command count, duration). Every robot action is a `Command`
     through `Runtime.submit`, never an adapter call.
   - An UNRESOLVED command aborts the test with no further commands (no retry, no blind
     rollback) and hands the incident to a person: reconcile settles it. A failed
     rollback also hands off.
   - Recovery is VERIFIED only when the command verifies and the given conditions hold
     on fresh observations. Robot-owned recovery is refused and can only be observed.
   - Agents cannot hand off or close incidents. Every step is an `incident.*` ledger
     entry carrying the incident snapshot (replayed on open) and, with a runtime, a
     trace event.
6. **Prognostics** (`maintenance/prognostics.py`, `ar.prognostic_estimate/1.0`). The
   estimate takes progpy's *API shape* (event state, time-of-event distribution,
   loading assumption, horizon); no progpy code is imported, linked or copied (NOSA
   1.3). It holds an event-state interval, time-of-event quantiles (p5/p50/p95),
   censoring (the share of bootstrap samples not reaching the event within the
   horizon; censored quantiles are lower bounds), the stated loading assumption and the
   horizon. Capability is gated on **p5**: RESTRICTED when p5 is shorter than the
   mission (or the horizon is), UNAVAILABLE when the event is reached. The estimator is
   a clean-room least-squares trend with a seeded residual bootstrap.
   `prediction_error` compares an estimate with the actual event after service.
7. **Triage integration** (`triage/operations.py`). Health ERROR/STALE items, readiness
   blocks and failsafes, novel and handed-off incidents, and prognostic threshold
   crossings become triage signals with evidence references, so feeding the same report
   twice is idempotent and cleared conditions auto-resolve (except protected
   severities). `OPERATIONS_RULES` route them (critical health and failsafes: safety/P1;
   aborted incidents: safety/P1); `operations_policy(base)` puts them in front of a
   policy without overriding rules the project already names.
8. **CLI**: `air health derive|check|ready|psi|diagnosis`, `air incident
   open|list|show|propose|handoff|close`, `air prognose estimate|list`. Bounded tests and
   recovery command a robot, so they run in code against a live `Runtime`, not from the
   CLI.

## Consequences
- A degraded robot is refused work through the same admission path as everything else;
  there is no second control path. RESTRICTED profiles are still admitted (the existing
  `check_capability` refuses only UNAVAILABLE); skills that must not run when restricted
  need their own check or gate mapping.
- STALE dominating the rollup makes a robot with one silent critical sensor "stale"
  overall. That is deliberate: no information is not health.
- Health and incidents depend on `runtime.admission`; nothing in `runtime` depends on
  them. `triage.operations` uses them by duck typing, except a lazy import of
  `threshold_crossings`.
- Seven new contracts at 1.0 with exported schemas.

## Sources (patterns adopted, no code copied)
- ROS `diagnostics` (BSD-3-Clause): `generic_analyzer.cpp` (expected items, STALE,
  worst-of, Other analyzer, critical items), `diagnostic_updater` FrequencyStatus and
  TimeStampStatus.
- Linux kernel PSI interface (`/proc/pressure/*`, documented ABI; read only).
- PX4-Autopilot (BSD-3-Clause): `HealthAndArmingChecks` (ordered checks, per-mode
  arming, failsafe flags).
- foxBMS-2 (BSD-3-Clause software): `diag` module (fault id, threshold counter,
  severity, delay).
- Chaos Toolkit (Apache-2.0): experiment spec (steady-state hypothesis, method,
  rollbacks with strategy).
- NASA progpy (NOSA-1.3, not GPL-compatible): API shape only, from
  `docs/research/sources/04-sim-eval-health.md`; not vendored, linked or imported (a
  test asserts it).
- Pollen Robotics Microduck (`docs/research/sources/06-microduck.md`): "the fall verdict
  reports but does not gate; `limp_fall` recovery runs on its own". Idea only; no code
  (microduck-arena has no license file; microduck_rl meshes are CC BY-NC-SA 4.0).

## Not done / needs the owner
- **Merge `OPERATIONS_RULES` into `default_policy()`** (triage owner): today callers opt
  in with `operations_policy(...)`; `air triage sweep` with the default policy routes
  these kinds to the default quality/P3 rule.
- **A health sweep inside `air triage sweep`**: it needs a place where live health
  reports are kept (a runtime-side recorder); the collector is ready.
- **Transport liveness wiring** (Zenoh liveliness tokens, MQTT last will) belongs to the
  coordination transports; `HealthAggregator.lost()` is the entry point.
- **Wire `HealthGate` into the reference runtime builders and `quickstart`**, and decide
  whether RESTRICTED should block specific skills (a per-skill `allowed_when_restricted`
  needs a skill-contract minor bump).
- **Streaming detectors** (`update(x) -> Detection | None`, ADWIN-style) and
  automatic incident opening from the critical fast path: not built.
- **STPA unsafe control actions as typed admission rules** (catalog backlog W3.8): not
  built.
- **Work orders from prognostic triggers and a CMMS REST integration** (catalog backlog
  W3.9): not built; `threshold_crossings` and the triage signal are the trigger.
- **jetson-stats/cgroup v2 probes** and **fault-injection scenarios as experiments**:
  not built.
- **Requirement claims**: REQ13/REQ14/REQ16/G10/G11/G13 now have more evidence; claims
  were not changed (the lead decides).
