# What AIR should adopt from open source: survey and plan

Survey date: 2026-10-22. Scope: open-source projects on GitHub, Gitee, AtomGit and
GitCode related to each part of the
*Requirements and architecture baseline v0.1* (see [architecture](../architecture.md)).
Six parallel researchers read the actual repositories
(source files, schemas, specs and licenses, not summaries). Their raw reports, with file
paths and unverified items marked, are in [sources/](sources/):

| # | Area | Report |
|---|---|---|
| 01 | Agentic robotics frameworks (ROSA, RAI, dimos, LeRobot, openpi, dora-rs, PhyAgentOS, OpenLoong, AimRT, Unitree SDK, LangGraph) | [01-agent-frameworks.md](sources/01-agent-frameworks.md) |
| 02 | Fleet coordination and execution (Open-RMF, VDA 5050 v3, openTCS, Nav2, BehaviorTree.CPP, ros2_control, ROS 2 actions, PlanSys2) | [02-fleet-coordination.md](sources/02-fleet-coordination.md) |
| 03 | Provenance and traceability (OpenTelemetry, OpenLineage, PROV, MLflow, DVC, in-toto/SLSA, RFC 9162/C2SP/Rekor, StrictDoc, Doorstop, OpenFastTrace, MCAP/rosbag2) | [03-provenance-traceability.md](sources/03-provenance-traceability.md) |
| 04 | Simulation, optimization, health, prognostics (Isaac Lab, ManiSkill, RoboVerse, MuJoCo/MJX, Genesis, Gazebo, Renode, FMPy, Optuna, ROS diagnostics, PSI, progpy, Chaos Toolkit) | [04-sim-eval-health.md](sources/04-sim-eval-health.md) |
| 05 | Developer ecosystem and hygiene (transformers, HF Hub, LangChain, pluggy, REP 2004, SROS2, OpenSSF, PyPI trusted publishing, MindSpore, PaddlePaddle, openEuler) | [05-dev-ecosystem.md](sources/05-dev-ecosystem.md) |
| 06 | Pollen Robotics Microduck (robot stack, RL pipeline, arena, OTA) | [06-microduck.md](sources/06-microduck.md) |

## How these claims were checked

The coordinator spot-checked, directly against the primary source, the claims this
plan depends on most:
- the VDA 5050 v3.0.0 rules: an unanswered zone request means do not enter; a
  disconnected robot finishes up to the last released node; the `orderUpdateId`
  acceptance rules
- licenses: PhyAgentOS is MIT; progpy is `NASA-1.3`; microduck-arena has **no license
  file**; microduck_rl meshes are **CC BY-NC-SA 4.0**
- the five defects found in AIR itself, below

Everything else is as reported, with **[U]** in the source reports where it was not
verified.

## The short version

The landscape strongly supports AIR's core bet. **Almost no agent framework keeps a
runtime between the model and the motors.**
- ROSA runs model-built `ros2` commands through a shell.
- ROS-LLM dispatches with `getattr` on model output.
- OpenLoong calls a grasp straight from an intent classifier, then sleeps 40 s as "done".
- RAI, dimos and PhyAgentOS let an LLM or VLM judge whether a physical step succeeded.

AIR's separation of facts from verdicts, admission, and UNRESOLVED-plus-reconcile is
the differentiator. The closest peer is **PhyAgentOS** (MIT, Sun Yat-sen University).
It has the same "execution record is never a verdict" and "reconcile, don't resend"
ideas, but still gives the final verdict to a VLM.

Where AIR is behind is mostly maturity:
- protocol details the AGV industry already standardized (VDA 5050)
- integrity engineering the transparency-log world already solved (Merkle checkpoints)
- observability standards (OpenTelemetry ids)
- project hygiene

## Defects the survey found in AIR (fix first)

| # | Defect | Where | Source |
|---|---|---|---|
| D1 | A lease that expires while the robot still physically occupies the zone releases it. `preempt=True` takes a zone without checking occupancy. This breaks ADR 0006, and is the same bug Open-RMF ships (#466). | `runtime/resources.py` `_expire`, `reserve` | 02 |
| D2 | Command handles live only in memory. A restart loses deduplication and UNRESOLVED state, so a retried command could execute twice. | `runtime/runtime.py` `_handles` | 01 |
| D3 | The registry checks a digest stored in the same file (corruption, not tampering). `fetch` can't pin a digest. "Latest" can resolve to a prerelease (`1.0.0-rc1` > `1.0.0`). | `registry.py` | 05 |
| D4 | The ledger hash is Python-specific (float `ts`, `default=str`), so other implementations can't verify it. A linear chain cannot detect tail truncation or a full rewrite. Single writer is not enforced by a lock. | `provenance/ledger.py` | 03 |
| D5 | Trace ids are 64-bit (`trc_<16hex>`). W3C/OTel require 128-bit, so they can't be exported without remapping. | `core/ids.py`, `provenance/tracing.py` | 03 |

## Adoption plan

Priorities: **P0** = do next (it either fixes something wrong or locks a format before it
spreads); **P1** = with the next stage; **P2** = later or on demand. Effort: S/M/L.

### Wave 1: correctness, formats and hygiene (P0, mostly S): **done**

W1.1 to W1.8 were implemented on 2026-10-22; see
[ADR 0007](../decisions/0007-wave1-integrity-and-formats.md). The license decision is
still open with the owner. D4's truncation and rewrite detection remains as W2.7.

| Item | What | Learned from | AIR module | Serves |
|---|---|---|---|---|
| W1.1 | Fix D1: expired-while-occupied becomes `EXPIRED_OCCUPIED` and keeps blocking; preempt requires known-vacant; fail closed | VDA 5050 `releaseLossBehavior`, fail-closed zone grants; RMF #466 as the counter-example | `runtime/resources.py` | G06, REQ06 |
| W1.2 | Fix D3: `fetch(name, version, digest=)`, an `air.lock` of digests, PEP 440/SemVer precedence | HF Hub commit-hash revisions | `registry.py` | G15, F07 |
| W1.3 | Fix D4 (part): ledger entry v2 with canonical JSON (RFC 8785 style), integer `ts_ns`, no `default=str`; v1 entries still verify; OS file lock for single writer | RFC 8785; C2SP needs reproducible hashes | `provenance/ledger.py` | REQ10 |
| W1.4 | Fix D5: 128-bit trace and 64-bit span ids in bare hex (prefix for display only); `to_traceparent()`/`from_traceparent()`; legacy ids padded | W3C Trace Context, OTel SpanContext | `core/ids.py`, `tracing.py`, `Command.trace_id` | REQ15 |
| W1.5 | Suspect evidence: each requirement has a fingerprint; evidence records the fingerprint it was produced against; the matrix check reports "stale" when they differ | Doorstop link fingerprints; OpenFastTrace `~revision` ids | `traceability.py`, pytest plugin | F01 |
| W1.6 | CI hardening: `permissions: contents: read`, SHA-pinned actions, Dependabot; `SECURITY.md` (14-day response); CODEOWNERS; PR template with requirement ids and an evidence checklist | OpenSSF Scorecard and Best Practices; REP 2004 | `.github/` | G15, F07 |
| W1.7 | Versioning and deprecation policy: SemVer distribution, declared public API, `AIRDeprecationWarning(FutureWarning)`, `@deprecated`/`@experimental` | transformers `deprecate_kwarg`, LangChain `@deprecated` | `docs/policy/`, `core/deprecation.py` | REQ18 |
| W1.8 | JSON Schema export for every contract (stdlib generator), published under `schemas/`; CI drift check and minor-is-superset compatibility check | Pydantic/msgspec output format; dora breaking-change gate | `core/jsonschema.py`, CI | REQ01, F06 |

### Wave 2: commitment semantics and integrity (P0, M)

| Item | What | Learned from | AIR module | Serves |
|---|---|---|---|---|
| W2.1 | Fix D2: **durable command intent journal**, written before dispatch; rebuild on restart; an intent without a terminal state becomes UNRESOLVED and is reconciled by observation, never resent | PhyAgentOS `forge/task.py`; LangGraph `put_writes` | new `runtime/journal.py` | REQ14, REQ07 |
| W2.2 | **Facts vs verdict in the contract**: an adapter report can never claim VERIFIED; evidence carries `phase` (before/during/after) and `association_quality`; only authoritative evidence satisfies postconditions; model judgments are non-authoritative and can at most move a command to UNRESOLVED | PhyAgentOS `verification/contracts.py` | `contracts/execution.py`, runtime `_verify` | REQ14 |
| W2.3 | **Cancellation and instant actions**: a CANCELLING state; the cancel gets its own response; reservations freed only after a confirmed stop plus W1.1; CANCELLED distinct from KILLED; instant `pause`/`resume`/`cancel`/`state_request` at any revision; never report a cancel as success | ROS 2 actions; VDA `cancelOrder`, instantActions; RMF cancel vs kill; Nav2's CANCELED→SUCCESS default as the counter-example | `coordination/protocol.py`, `coordinator.py`, `runtime.py` | REQ17, G07 |
| W2.4 | **Released prefix vs horizon**, plus an `update_id` within a revision using the VDA acceptance rules (outdated rejected; identical ignored; conflicting rejected; nothing after a cancel; on rejection keep the current work); a disconnected robot finishes only the released prefix | VDA 5050 v3 §4.1, §6.1.4 | `coordination/*` | REQ09, G07, G14 |
| W2.5 | **Human approval as an interrupt**: an `AWAITING_APPROVAL` command state; `resume(command_id, decision, approver, authority_revision)` bound by id, never by position | LangGraph `interrupt`/`Command(resume=)` (made safer) | `runtime/admission.py`, `contracts/authority.py` | REQ17 |
| W2.6 | Intent deadman for continuous skills: a refresh period and keepalive below `max_input_age_s`; stop means hold position | Microduck `safety.rs` 500 ms deadman | `contracts/execution.py` | REQ12, G05 |
| W2.7 | Fix D4 (rest): **RFC 9162 Merkle tree plus C2SP signed checkpoints** over the ledger; `air ledger checkpoint/prove/verify --since`; anchor checkpoints in git (optionally Rekor) | RFC 9162; C2SP tlog-checkpoint; Rekor v2 | new `provenance/merkle.py` | REQ10, REQ16 |

### Wave 3: stage 4 and 5 foundations, and the second integration (P0/P1, M-L)

| Item | What | Learned from | AIR module | Serves |
|---|---|---|---|---|
| W3.1 | `ExperimentBackend` protocol with **declared** capability flags (fidelity per aspect, determinism class, restore level, lossy-restore list, unsupported features that raise); `TimePolicy` (physics_dt, control_dt with integer decimation, lockstep flag); out-of-process backends | RoboVerse `BaseSimHandler` flags; MuJoCo state taxonomy; ManiSkill freq invariant; MJX raises on unsupported features | new `engineering/backends.py` | REQ04, G03, G04 |
| W3.2 | `SeedRecord` (root, per-episode, per-env, source including `unset`, RNGs actually seeded); determinism **measured** by a reproducibility probe, not declared | ManiSkill main/episode seeds; Isaac Lab `seed=None` | `provenance/model.py` ExperimentRecord | F05, G04 |
| W3.3 | **Physics fingerprint**: golden run per platform, simulator version pin, `runner_version`; compare results only within platform plus version; re-measure, never trust stored scores | Microduck arena `golden.py`, `RUNNER_VERSION`, `arena recover` (8.4% arm64-vs-x86 divergence) | `engineering/`, ExperimentRecord | REQ02, G03 |
| W3.4 | Optuna adapter where **AIR is the system of record**: study = SearchRecord (sampler, seed, Optuna version); trial = ExperimentRecord, failed and pruned trials included; releases picked from re-validated feasible Pareto sets on held-out seeds | Optuna `FrozenTrial`, constraints, `best_trials` (the study doesn't persist sampler or seed) | `engineering/search.py`, extra `[optuna]` | REQ04, G04 |
| W3.5 | **Policies as content-addressed entities** with load gates (model_api, obs/action dims, robot model); a failed load keeps the old policy; origin classes official/community/local, with non-official needing explicit authority; action-chunk policies carry a staleness bound | Microduck `policy-manifest.md`, policy channel; LeRobot `ChunkPolicySpec`; openpi broker as the counter-example | `registry.py`, provenance, new `contracts/policy.py` | REQ11, REQ16, G03 |
| W3.6 | **Microduck RobotAdapter** (JSON-RPC/NDJSON to `robotd`, the HTTP lane, or `duck-sim`) as gate **G02's second, materially different integration**; conformance in CI against `duck-sim`, which runs the real daemons on a MuJoCo body | Microduck `duck-ipc-proto`, `docs/design/simulation.md` | new `integrations/microduck/` | G02, REQ03, REQ05 |
| W3.7 | Health aggregation: expected-component manifest, **STALE as a first-class level**, worst-of rollup, an "other" bucket, critical fast path; rate/latency monitors; Linux PSI probes; report-vs-gate (a fall makes capability RESTRICTED, and robot-owned recovery is not commanded) | ROS diagnostics `generic_analyzer.cpp`, `FrequencyStatusParam`; kernel PSI; Microduck `limp_fall` | new `health/` | REQ13, REQ14, G10, G11 |
| W3.8 | Incident pipeline: detector, incident (novelty flag), hypotheses (agent-proposed ones marked), **bounded tests behind a steady-state gate** with admission budgets and rollbacks, then recovery or human handoff | Chaos Toolkit spec; Renode snapshots; ros2_fault_injection | new `incidents/` | REQ14, REQ11, G11, G13 |
| W3.9 | `PrognosticEstimate` (event_state interval, time-of-event distribution, loading assumption, horizon, censoring); gate capability on p5, not the mean; clean-room only | progpy API shape (NOSA, so **do not link**) | new `maintenance/prognostics.py` | REQ16, G13 |

### P1 next (selected)

| Item | Learned from | AIR module |
|---|---|---|
| OTel exporter (`air[otel]`), which also reaches MLflow; GenAI semconv `invoke_agent`/`plan` names isolated and pinned | OTel GenAI semconv; MLflow OTLP ingest | `provenance/otel.py` |
| in-toto **test-result** attestation from the pytest plugin (optionally DSSE-signed) | in-toto attestation v1 | `testing/`, `provenance/attest.py` |
| PROV-JSON export | W3C PROV-JSON, `prov` | `provenance/export_prov.py` |
| MCAP recordings as ledger entities (Metadata record with activity and trace ids; sha256 is the integrity, not CRC) | MCAP spec; rosbag2 MCAP default | `provenance/recording.py` |
| Reproducibility lock and `air repro --check` | `dvc.lock` | provenance, CLI |
| Per-sender `seq`, liveness (ONLINE/OFFLINE/CONNECTION_BROKEN, heartbeats; when broken, holds are kept and tasks become UNRESOLVED), HLC timestamps | VDA `headerId`, connection; Nav2 bonds; dora uhlc | `coordination/` |
| Claim-then-allocate resource sequences with door/lift/area modules; graded robot trust (ignored/noticed/respected/utilized) | openTCS `Scheduler`, `IntegrationLevel` | `resources.py`, `team.py` |
| Skill `occupies` holds scoped by token; `blocking` semantics; RETRIABLE with explicit retry/skip; `on_halted` exactly once | dimos `CapabilityRegistry`; VDA blockingType; BT.CPP `onHalted` | `contracts/skill.py` |
| Report the applied command (clamped) vs requested; typed `max_relative_target` and workspace-box params | LeRobot `send_action`; RAI `WorkspaceBounds`; Microduck `limited_by` | `plugins.py`, `skill.py` |
| LeRobot and openpi adapters (with timeouts); VDA 5050 v3 bridge adapter | sources 01, 02 | `integrations/` |
| Asset cards (`ar.asset_card/1.0`); conformance-report contract and badge; copier plugin template; MkDocs (Diátaxis) docs; Quality Declaration (target REP 2004 QL2); trusted-publishing release with attestations and SBOM; towncrier | source 05 | repo |

### P2 later

MCP server exposing skills as *proposals* (calls go through `Runtime.submit`) · dora
transport · Open-RMF fleet bridge · RMF-style negotiation for multiple authority
domains · OpenLineage, Sphinx-Needs and StrictDoc/ReqIF exports · MLflow export ·
MuJoCo and FMPy reference backends · Microduck vetbook import into MaintenanceRecord ·
experience-to-lesson pipeline with guarded promotion · package split when the first
heavy integration lands (`agentic-robotics-<x>`, conformance as the shared standard
tests) · SROS2-style transport authentication.

## Anti-pattern charter (things AIR will not do)

Collected from what the surveyed projects actually do:

1. Let model tool calls actuate directly: no shell, no `getattr` dispatch, no SDK call
   from an intent classifier. **Everything goes through admission**, including over MCP.
2. Let an LLM or VLM set VERIFIED. Model judgments are evidence, not verdicts.
3. Allow by default (RAI `None` = everything), or put guardrails only in the prompt.
4. Treat elapsed time as completion, use wall-clock pacing, block forever, or retry
   silently forever.
5. Replay stale action chunks open-loop.
6. Release a zone on timeout, idleness or preemption while the robot may still be in it.
7. Report a cancel as success, rewrite released work, or coalesce conflicting duplicates.
8. Reset a whole team because one heartbeat was lost.
9. Treat STALE as OK, or drop unmatched diagnostics.
10. Claim determinism from a seed alone, compare sim results across platforms or
    versions, or accept LLM-judged benchmarks.
11. Trust an optimizer's own store as provenance, or hide failed trials.
12. Call CRC32 or MD5 integrity, run a log server per project, or claim SLSA levels for
    laptop builds.
13. Load code or pickle from assets, or discover plugins through import side effects.
14. Put a framework (LangChain, pydantic, otel, mlflow) into the stdlib core. They are
    extras only.
15. Claim safety-standard compliance, or imply protection a robot lacks (e.g. Microduck
    has no per-joint or thermal software limits).

## License register (for reuse decisions)

| Use freely (permissive, keep attribution) | Use with care | Do not copy |
|---|---|---|
| Open-RMF, VDA 5050 repo (MIT; spec text is VDA-owned), openTCS (MIT), Nav2, BT.CPP, ros2_control, LeRobot, openpi, dora-rs, RAI, ROSA, dimos, PhyAgentOS (MIT), Isaac Lab code, ManiSkill, RoboVerse, MuJoCo, Genesis, Gazebo, Renode, FMPy, Optuna, river, PyOD, Chaos Toolkit, OTel, OpenLineage, MLflow, DVC, StrictDoc, Sphinx-Needs, `prov`, MCAP, rosbag2, Microduck core repos, mjlab | jetson-stats (AGPL, compatible); Doorstop (LGPLv3); OpenFastTrace (GPL-3.0: ideas only); AimRT (Mulan PSL v2: legal review); Isaac Sim runtime terms; Genie Sim (license unclear) | progpy (NASA-1.3 / NOSA, not GPL-compatible); microduck-arena and microduck-challenges (no license); microduck_rl meshes (CC BY-NC-SA); OpenLoong repos (no license); Gitee AGV projects (no license) |

## A decision only the owner can make: the license

The libraries AIR is modelled on are all permissive:
- transformers, huggingface_hub, LeRobot, MindSpore, PaddlePaddle, Autoware, Microduck
  and dora-rs are Apache-2.0
- LangChain and PhyAgentOS are MIT
- Isaac Lab and MoveIt are BSD

The surveyed AGPL robotics or AI projects are Viam RDK and Ultralytics, and Ultralytics
sells a commercial license for production. AGPL §13 is likely to deter the product teams
and hosted workbenches the baseline targets (ADR 0004 already notes this). Relicensing
gets harder with every outside contribution.

Options, in order of ecosystem fit:
1. Apache-2.0 for everything.
2. Apache-2.0 core, contracts, schemas and SDK, with AGPL only for a hosted platform.
3. AGPL plus a CLA plus a commercial license.

Whichever is chosen, the JSON Schemas and the conformance suite should be permissively
licensed, so that other implementations can conform. This is a business and legal
decision; how AGPL applies to plugins needs counsel.

## Suggested sequencing against the baseline stages

- **Now:** Wave 1, plus the license decision.
- **Before calling stage 3 done:** Wave 2 (W2.1 to W2.6 close G07 and most of G12/G14).
- **Stage 4:** W3.1 to W3.5, plus the P1 provenance items (OTel, attestations, MCAP,
  repro lock).
- **Stage 5:** W3.7 to W3.9.
- **Stage 6 / G02:** W3.6 (Microduck) can start in parallel with stage 4. It needs only
  the existing adapter protocol and `duck-sim`.
