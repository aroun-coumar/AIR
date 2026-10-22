# Architecture

This page maps the baseline (sections 5 and 6) onto the code, and shows how the
evidence spine ties everything together.

## Four paths

```
 devices / simulators / tools
          |  observations (time, frame, quality)
          v
   +--------------+   scoped context    +-------------------+
   |  adapters    | ------------------> | agents (decision) |  ModelProvider: any model
   +--------------+                     +-------------------+
          ^                                   | TaskPlan (typed proposal, no authority)
          | execute / interrupt               v
   +------------------------------------------------------------+
   |  runtime: admission -> dispatch checks -> execution ->      |
   |  verification from observations | UNRESOLVED -> reconcile   |
   +------------------------------------------------------------+
          ^ authority (delegations, revisions)   ^ fencing tokens
   +--------------------+                 +--------------------+
   | AuthorityService   |                 | ResourceManager    |
   +--------------------+                 +--------------------+
```

- **Data path:** adapters publish `Observation`s and an effective `CapabilityReport`.
- **Decision path:** `TaskAgent` builds scoped context (`ContextBuilder`) and asks a
  `ModelProvider` for a typed `TaskPlan`, which is validated before anything runs.
- **Command path:** `Runtime.submit` runs the admission checks, re-checks authority and
  fencing at dispatch, executes through the adapter, and verifies postconditions.
- **Authority path:** `AuthorityService` grants scoped `Delegation`s. Every change bumps
  a revision, stale revisions are rejected, and revocation interrupts running work.

## Evidence spine

```
 span(decision) --> agent_decision event
   span(plan)   --> plan_started ... plan_finished
     span(command) --> submitted, accepted, started, verification, verified|...
                       reservation_*, authority_*  (all carry trace_id/span_id)
          |
          | Trace sinks
          v
   Project ledger (.air/ledger.jsonl, hash-chained)
     + activity.* (research, development, simulation, experiment, evaluation,
                   release, deployment, maintenance, calibration, incident, ...)
     + entity.*   (content-addressed artifacts, physical components, replacements)
     + requirement.*, calibration.invalidated, project.created
```

- `provenance.tracing`: contextvars spans; async tasks inherit them, and
  `Command.trace_id` / `TaskPlan.trace_id` carry them across boundaries.
- `provenance.ledger`: append-only, hash-chained, verifiable, safe to reload when
  another writer appends.
- `provenance.project`: `Project.activity(...)` records used and generated entities, the
  environment (Python, platform, git commit and dirty flag), params, metrics, outcome and
  requirement links. Lineage and impact queries walk the PROV graph.
- `provenance.traceability`: requirement catalog, code claims (`__requirements__`),
  test evidence (pytest plugin) and activity counts make up the matrix, and `--check`
  rejects claims the evidence does not support.
- `provenance.explain`: timelines and "why" explanations from traces or ledgers.
- `provenance.context_pack`: generated briefing for people and agents.

## Agentic engineering work (research, development, simulation, maintenance)

```
 WorkTask (kind, goal, requirements, inputs, budget, sandbox policy, model constraints,
           data classification)
     |
     v
 WorkAgent ── ModelRouter ──> on-robot model | local host (Ollama/vLLM) | edge | cloud API
     |            (placement and egress policy; restricted data never leaves the device)
     | tool call: allowlist -> args -> authority -> approval -> Sandbox -> ledger
     v
 Sandbox (process | container; declares what it enforces)
     |
     v
 Proposals (report, patch, config, experiment result, maintenance plan, dataset)
     |
     v
 decide(): a different, authorized principal approves or rejects (decision activity)
```

- Robot actions requested by work agents go through `Runtime.submit` (admission
  applies) and need approval.
- Every model turn, tool call, approval, finding and proposal is in the project
  ledger on the activity's trace.
- See [ADR 0008](decisions/0008-agentic-engineering-work.md).

## Triage: one queue for everything AIR finds wrong

```
 runtime trace (unresolved, failed, unconfirmed stop, adapter/interrupt errors)  --live-->
 resource manager (lease expired while occupied)                                 --live-->
 work ledger (pending proposals, denied approvals, sandbox/model refusals)        --sweep->
 checks (ledger integrity, requirements matrix, source catalog), calibration      --sweep->
                                                                                       |
                         rule: severity (impact) + priority (urgency) + owner + deadlines
                         fingerprint: duplicates fold into one item (count, evidence)
                                                                                       v
   new -> triaged -> assigned -> in_progress -> resolved | dismissed
     \-- escalated (deadline passed, next owner)     resolved --(seen again)--> regressed
```

- Silences and inhibitions never hide safety or integrity items. Agents cannot resolve
  or dismiss them, and resolving a safety item needs an evidence reference.
- Every change is a `triage.*` ledger entry; the book is rebuilt by replay.
- `air triage sweep|list|show|ack|assign|start|resolve|dismiss|reopen|silence|rules`.
- See [ADR 0009](decisions/0009-triage.md).

## Hardware catalog

```
 hardware/data/*.json  +  .air/hardware/*.json  (project extensions, ids never collide)
          |
          v
 DeviceCatalog (ar.device_catalog/1.0): RobotClass (slots) · DeviceType · Protocol
          |
          | check_manifest(ComponentManifest 1.1: robot_class, components[type_id, properties])
          v
 findings (unknown type, missing/wrong-unit property, interface, slot counts)
 telemetry_limits / maintenance_plan  -->  health monitoring, maintenance, triage
```

- Device types declare typed properties (units checked for dimension), interfaces
  (protocol ids), telemetry with typical limits, maintenance items, failure modes and a
  safety flag.
- The catalog is data, so the core stays stdlib-only. `DeviceCatalog.problems()` keeps it
  internally consistent.
- The catalog describes and never prescribes: composition findings are advice unless
  `strict`.
- `extends` specializations, per-unit `limits`, pinned `documents`, typed `relations`
  and `sensitivity` carry custom-component context.
- `semantics` links units, classes and registry ids to QUDT, SOSA/SSN, SAREF, LwM2M and
  Matter, and exports WoT Thing Models and JSON-LD.
- `domains` holds the 19 domain packs, used for validation advice and agent briefings.
- `context` selects, ranks and budgets the facts for a task, with sources, and withholds
  restricted ones when data would leave the device.
- See [hardware-catalog.md](hardware-catalog.md),
  [ADR 0010](decisions/0010-hardware-catalog.md) and
  [ADR 0011](decisions/0011-hardware-semantics-and-context.md).

## Package dependency rule

`core` depends on nothing. `contracts` depend on `core`. `runtime`, `agents` and
`provenance` depend on `contracts`/`core`; `runtime` and `agents` use `provenance.tracing`.
`hardware` depends on `contracts` and `core`. `integrations` depend only on public
protocols (`plugins`, `contracts`). Nothing in the core imports an integration.

## Engineering and optimization loop

```
 ExperimentBackend (declared capabilities, all false by default)   in process | air.backend/1 JSON lines
          |  load(scenario, TimePolicy[integer decimation], features)  -> BackendInfo (what actually ran)
          v
 run_experiment -> ExperimentRecord 1.1 (SeedRecord, platform, runner_version, fingerprint)
          |                 \-- per-sample RNG snapshots -> replay_sample (one sample alone)
          |-- probe_reproducibility: exact | within_tolerance | differs (+ RNG, buffers, restore)
          |-- golden runs per platform + simulator pin + runner version; re-measure, never trust
          v
 Search: trials keyed (config, scenario, seed, budget) -> complete | failed | pruned | abandoned
          |  same seeds for every candidate; feasible Pareto set (constraints <= 0)
          v
 select_release: Pareto set re-run on held-out seeds -> ReleaseSelection
 benchmark scores count only with recorded sim-to-real validity (MMRV, Pearson); invalidate, never delete
```

- `engineering` depends on `core` and `provenance`; samplers beyond random and grid
  (Optuna) are optional extras imported lazily.
- Every experiment, trial, search, probe, golden check, re-measurement, validity
  assessment and release selection is a project activity.
- `air experiment backends|run|probe|golden|search|list|remeasure|invalidate|validity`.
- See [ADR 0014](decisions/0014-engineering-loop.md).
## Policies, releases and engineering-domain contracts
 model bytes (opaque) + ar.policy_manifest sidecar ──► LocalRegistry.publish_policy (blob + doc)
                                                         │  TUF targets/snapshot/timestamp/root
                                                         ▼  (threshold-signed; director per robot)
 PolicySlot.load ── digest ✓ · PolicyRequirements ✓ · origin authority ✓ ──► LOADED (old kept on failure)
        arm(op) ──► start(token) ──► infer: check_observation → runner → check_action
                                        └─► ActionChunk(provenance, max_age_s) ──► accept (stale/foreign refused)
        stop() ──► declared exit mode (hold | damp)
 ActivationManager: stage (security counter ≥ stored) ─► boot ─► TRIAL ─► confirm(observed checks)
                                                                   └─► reboot / deadline ─► revert
- `ar.policy_manifest/1.0` advertises everything needed before admission (model API,
  features and dims, robot model, joint order, gains, observation scale/clip/history,
  action scale and bounds, prediction vs execution steps, action interval, inference
  backend, origin, exit mode, mandatory staleness bound); `identity()` hashes all of it
  with the model digest.
- `ar.release/1.0` and `ar.release_activation/1.0` implement trial-then-confirm with a
  monotonic security counter; `releases.tuf` implements the TUF client order with
  rollback and freeze checks; HMAC signing is stdlib, Ed25519 is the `[signing]` extra.
- `ar.parameter_schema/1.0` and `ar.parameter_set/1.0`: typed parameter metadata with
  units from the core unit table, validation and diff (reboot and calibration flags).
- `ar.episode/1.0` keeps truncation distinct from termination and converts losslessly to
  RLDS-like and LeRobot-like dicts; `ar.training_run/1.0` records git state and NaN checks.
- `ar.action_space/1.0` and `ar.unified_action/1.0`: one named-slot action vector with
  units and masks.
- `ar.asset_card/1.0` (claims, never authority) and `ar.conformance_report/1.0`
  (`air conformance --out`; `passed` must agree with the outcomes).
- `releases` depends on `contracts`, `core` and `provenance`; it calls the runtime's authority
  service only through its `check()` method; nothing in `runtime` depends on it. See
  [ADR 0016](decisions/0016-policies-releases-contracts.md).
## Operations: health, readiness, incidents, prognostics (stage 5)
 ComponentManifest + catalog telemetry_limits --derive--> HealthManifest (expected items, gates)
 adapter readings · monitors (rate, latency) · PSI probe · diagnosis table · transport loss
          |
 HealthAggregator: missing = ERROR · silent past timeout or lost = STALE (never OK)
                   worst-of rollup by component tree · "other" bucket · critical fast path
          |                                  |
          | HealthReport                     | ReadinessGate (ordered pluggable checks)
          v                                  v      -> can_run per mode, failsafe flags
 HealthGate (admission check before check_capability): only downgrades profiles
   + prognostic_gater (p5 time-of-event vs mission)  -> RESTRICTED / UNAVAILABLE
   + RobotOwnedRecoveryCheck: recovery the robot owns is never commanded
          |
 Detection -> Incident (novel vs domain-pack failure classes and learned signatures)
   -> Hypothesis (agent ones non-authoritative) -> BoundedTest: steady state, method via
      Runtime.submit within a budget, steady state again, rollbacks by strategy
   -> recovery VERIFIED from observations | handoff (UNRESOLVED: reconcile, never retry)
          |
 triage.operations: health ERROR/STALE, readiness blocks/failsafes, novel or handed-off
                    incidents, prognostic crossings -> triage Signals
- Health reports, it does not command. Its only effect on the runtime is a gated
  `CapabilityReport` (and a monotonic gated revision) that ordinary admission checks.
- Every incident step and readiness evaluation is a ledger entry; incident steps are also
  trace events. Prognostics are a stdlib, clean-room estimator (progpy is NOSA: API shape
  only).
- `air health derive|check|ready|psi|diagnosis`, `air incident open|list|show|propose|
  handoff|close`, `air prognose estimate|list`.
- See [ADR 0015](decisions/0015-health-incidents-prognostics.md).
