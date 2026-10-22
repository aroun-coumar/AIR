# Context pack: agentic-robotics

Generated 2026-10-22 13:03 UTC at commit `a538297` by `air context`. Do not edit by hand; regenerate it.

## Project

- **Version:** 0.1.0.dev0
- **Description:** A reusable framework for building agentic robotic systems: typed execution contracts, runtime admission, authority, and evidence.
- **Python:** >=3.11
- **Runtime dependencies:** none

## Read first

- [CLAUDE.md](CLAUDE.md)
- [README.md](README.md)
- [docs/architecture.md](docs/architecture.md)

## Requirements status

Evidence status for REQ and gate items: evidenced 35, untraced 1

Open (not yet claimed verified):

- REQ02 End-to-end research, development, simulation, evaluation and implementation support: claimed in_progress, evidence evidenced
- REQ03 Coverage across the 19 hardware, software, infrastructure and environment domains: claimed in_progress, evidence evidenced
- REQ04 Agentic optimization and configuration down to hardware: claimed planned, evidence evidenced
- REQ06 Multiple software agents within a robot and coordination among physical robots: claimed in_progress, evidence evidenced
- REQ07 Intelligent conversation leading to executable commitments and verified outcomes: claimed in_progress, evidence evidenced
- REQ08 Shared memory and context reuse to reduce repeated model input and reasoning: claimed in_progress, evidence evidenced
- REQ09 Fast task decomposition, allocation, prioritization, dispatch and selective acknowledgement: claimed in_progress, evidence evidenced
- REQ11 Guided and anticipatory transfer of learning with context enrichment: claimed planned, evidence evidenced
- REQ12 Real-time inference, execution and synchronization of robots and features: claimed in_progress, evidence evidenced
- REQ13 Hardware bottleneck awareness and appropriate adaptation: claimed in_progress, evidence evidenced
- REQ14 Detection, investigation, learning and handling of unfamiliar failures: claimed in_progress, evidence evidenced
- REQ15 Robot and team monitoring linked to mission consequences: claimed in_progress, evidence evidenced
- REQ16 Maintenance and component lifecycle management: claimed in_progress, evidence evidenced
- REQ17 Human intervention, override, bounded delegation, takeover and handback: claimed in_progress, evidence evidenced
- G02 Heterogeneous integration: claimed planned, evidence evidenced
- G03 Engineering traceability: claimed planned, evidence evidenced
- G04 Constrained optimization: claimed planned, evidence evidenced
- G05 Timing and freshness: claimed in_progress, evidence evidenced
- G07 Typed commitments: claimed in_progress, evidence evidenced
- G08 Context efficiency: claimed in_progress, evidence evidenced
- G09 Qualified handoff: claimed planned, evidence untraced
- G10 Hardware adaptation: claimed in_progress, evidence evidenced
- G11 Unfamiliar failure: claimed planned, evidence evidenced
- G12 Human control: claimed in_progress, evidence evidenced
- G13 Maintenance readiness: claimed in_progress, evidence evidenced
- G14 Deployment resilience: claimed planned, evidence evidenced
- G15 Authorization and integrity: claimed in_progress, evidence evidenced
- AIR01 Agents perform research, development, simulation, evaluation, maintenance and diagnosis work inside sandboxes, under budgets, authority and approval, with every step recorded: claimed in_progress, evidence evidenced
- AIR02 Provider-agnostic intelligence: local open models, closed APIs and robot-hosted models, selected by placement and data-egress policy: claimed in_progress, evidence evidenced
- AIR03 Agent deliverables are proposals decided by a different authorized principal; agents never approve their own work or actuate robots outside runtime admission: claimed in_progress, evidence evidenced

## Package map

- `src/agentic_robotics/__init__.py`: Agentic Robotics Framework.
- `src/agentic_robotics/agents/__init__.py`: Agents: model-provider interface, context selection and the robot task agent.
- `src/agentic_robotics/agents/context.py`: Context selection for agent decisions (baseline section 12). [REQ08]
- `src/agentic_robotics/agents/model.py`: Model-provider interface (baseline sections 5 and 8). [REQ05]
- `src/agentic_robotics/agents/providers/__init__.py`: Provider-agnostic model layer: local open models, closed APIs, and robot-hosted models.
- `src/agentic_robotics/agents/providers/anthropic_provider.py`: Claude via the official Anthropic SDK (optional extra: ``agentic-robotics[anthropic]``). [AIR02, REQ05]
- `src/agentic_robotics/agents/providers/base.py`: Provider-neutral model layer: where a model runs, what it can do, and a multi-turn [AIR02, REQ05, REQ08, REQ12]
- `src/agentic_robotics/agents/providers/catalog.py`: Model catalog: a project's declared models and where they run (``.air/models.json``). [AIR02, REQ18, F07]
- `src/agentic_robotics/agents/providers/openai_compat.py`: OpenAI-compatible chat-completions client (stdlib only). [AIR02, REQ05, REQ12]
- `src/agentic_robotics/agents/providers/router.py`: Model routing by policy: which models may see this work, in which order to try them. [AIR02, REQ05, REQ08, REQ12, F07]
- `src/agentic_robotics/agents/providers/scripted.py`: Deterministic chat provider for tests, examples and offline development. [AIR02, REQ18]
- `src/agentic_robotics/agents/task_agent.py`: Robot task agent: turns a goal into a typed, validated :class:`TaskPlan`. [REQ05, REQ07, REQ08]
- `src/agentic_robotics/cli.py`: ``air`` / ``agentic-robotics`` command-line tool. [REQ18]
- `src/agentic_robotics/conformance.py`: Adapter conformance suite (baseline sections 4 and 7, "Plugin conformance"). [REQ03, REQ18, F06]
- `src/agentic_robotics/contracts/__init__.py`: Public data contracts. Every contract is versioned and round-trips through JSON.
- `src/agentic_robotics/contracts/authority.py`: Delegation and human intervention contracts (baseline sections 6 and 17). [REQ01, REQ07, REQ17]
- `src/agentic_robotics/contracts/capability.py`: Capability reports (baseline sections 6 and 15). [REQ01, REQ13]
- `src/agentic_robotics/contracts/dataset.py`: Dataset episodes and training-run records (ADR 0016, W5.5). [REQ02, REQ11, F05, F07, G03]
- `src/agentic_robotics/contracts/execution.py`: Execution contracts, commands and results (baseline sections 6 and 14). [REQ01, REQ07, REQ12, REQ14]
- `src/agentic_robotics/contracts/manifest.py`: Robot and component manifests (baseline section 6, ComponentManifest). [REQ01, REQ03, REQ16]
- `src/agentic_robotics/contracts/observation.py`: Observation contract (baseline section 6). [REQ01, REQ12, REQ14]
- `src/agentic_robotics/contracts/parameters.py`: Typed parameter metadata and parameter sets (ADR 0016, W5.1). [REQ03, REQ04, REQ16, F02, F04, G10]
- `src/agentic_robotics/contracts/policy.py`: Learned-policy contracts: the policy manifest, action chunks and the cross-embodiment [REQ05, REQ11, REQ12, REQ16, F03, F04, G03, G05, G15]
- `src/agentic_robotics/contracts/release.py`: Release, activation, asset-card and conformance-report contracts (ADR 0016). [REQ10, REQ16, REQ18, F04, F06, F10, G14, G15]
- `src/agentic_robotics/contracts/resources.py`: Resource reservations with fencing (baseline sections 6 and 11). [REQ01, REQ06, REQ09]
- `src/agentic_robotics/contracts/skill.py`: Skill and task specifications (baseline section 6). [REQ01, REQ05, REQ12, REQ14]
- `src/agentic_robotics/coordination/__init__.py`: Multi-robot coordination: typed commitment protocol, allocation, fenced reservations,
- `src/agentic_robotics/coordination/allocation.py`: Task allocation. Products can supply their own :class:`Allocator`; the coordinator [REQ09]
- `src/agentic_robotics/coordination/coordinator.py`: Team coordinator: a transactional coordinator within one authority domain [REQ06, REQ07, REQ09, REQ10, REQ17, G06, G07, G14]
- `src/agentic_robotics/coordination/hlc.py`: Hybrid logical clocks for coordination messages. [REQ10, REQ12]
- `src/agentic_robotics/coordination/liveness.py`: Liveness and message ordering for team coordination. [REQ06, REQ10, REQ12, G14]
- `src/agentic_robotics/coordination/orders.py`: Orders: released prefix, horizon, and update acceptance (VDA 5050 v3 sections 4.1 and [REQ07, REQ09, G07, G14]
- `src/agentic_robotics/coordination/participant.py`: Participant: the robot-side half of team coordination. [REQ06, REQ07, REQ09, REQ17, G07, G14]
- `src/agentic_robotics/coordination/protocol.py`: Team coordination protocol (baseline section 11). [REQ06, REQ07, REQ09, REQ17]
- `src/agentic_robotics/coordination/team.py`: Team: the convenience entry point for multi-robot work (baseline section 7 API shape). [REQ06, REQ09, REQ17, REQ18]
- `src/agentic_robotics/coordination/transport.py`: Message transport between coordinator and participants. [REQ06, REQ09, G14]
- `src/agentic_robotics/core/__init__.py`: Shared core: identities, schemas, units, frames, clocks and errors.
- `src/agentic_robotics/core/clock.py`: Clock abstractions. [REQ01, REQ12]
- `src/agentic_robotics/core/deprecation.py`: Deprecation and stability markers (see ``docs/policy/versioning.md``). [REQ18]
- `src/agentic_robotics/core/errors.py`: Framework error hierarchy. [REQ01]
- `src/agentic_robotics/core/ids.py`: Identity helpers. [REQ01]
- `src/agentic_robotics/core/jsonschema.py`: JSON Schema (Draft 2020-12) for every contract, generated from the dataclass hints. [REQ01, REQ18, F02, F06]
- `src/agentic_robotics/core/schema.py`: Versioned, typed contract base. [REQ01, REQ18]
- `src/agentic_robotics/core/units.py`: Minimal units and coordinate-frame support. [REQ01]
- `src/agentic_robotics/core/versions.py`: Version precedence shared by the registry, deprecations and compatibility checks. [REQ18]
- `src/agentic_robotics/doctor.py`: ``air doctor``: report environment and tool availability. Never raises. [REQ18]
- `src/agentic_robotics/engineering/__init__.py`: Engineering and optimization loop (baseline stage 4; ADR 0014). [REQ04, G04]
- `src/agentic_robotics/engineering/backends.py`: Experiment backends: simulators, emulators and reference models behind one protocol. [REQ04, G03, G04]
- `src/agentic_robotics/engineering/cli.py`: ``air experiment``: run, probe, golden runs, search, re-measure, validity, invalidate. [REQ02, REQ04, F05, G03, G04]
- `src/agentic_robotics/engineering/evaluation.py`: Evaluation validity: a simulator benchmark counts as evidence only after its [REQ02, G03, F05]
- `src/agentic_robotics/engineering/golden.py`: Physics fingerprints and golden runs (W3.3; Microduck arena ``golden.py``). [REQ02, G03, F05]
- `src/agentic_robotics/engineering/optuna_adapter.py`: Optuna as a *sampler* for :class:`~agentic_robotics.engineering.search.Search` [REQ04, G04]
- `src/agentic_robotics/engineering/pointmass.py`: Deterministic pure-Python reference backend: a 1-D point mass driven to a target by a [REQ04, G03]
- `src/agentic_robotics/engineering/probe.py`: Reproducibility probe: determinism is *measured*, never taken from a declaration or a [F05, G04, REQ04]
- `src/agentic_robotics/engineering/process.py`: Out-of-process backends over a JSON-lines protocol (``air.backend/1``). [REQ04, G03]
- `src/agentic_robotics/engineering/runner.py`: Run an experiment on a backend and produce an :class:`ExperimentRecord` that says [REQ02, REQ04, F05, G03]
- `src/agentic_robotics/engineering/search.py`: Constrained search where AIR is the system of record (W3.4). [REQ04, G04, F05]
- `src/agentic_robotics/engineering/seeds.py`: Seeds: one root, derived per-episode and per-env seeds, and an honest source. [F05, G04]
- `src/agentic_robotics/engineering/serve.py`: ``python -m agentic_robotics.engineering.serve <backend>``: serve a registered backend [REQ04]
- `src/agentic_robotics/hardware/__init__.py`: Hardware catalog: robot classes, device types and protocols, and manifest checks. [REQ03]
- `src/agentic_robotics/hardware/catalog.py`: Hardware catalog: robot classes, device types and protocols AIR can describe. [REQ03, REQ01, REQ13, REQ16]
- `src/agentic_robotics/hardware/cli.py`: ``air hardware``: browse the catalog, check manifests, and build task context. [REQ03, REQ16]
- `src/agentic_robotics/hardware/context.py`: Task context: the hardware facts a specific task needs, ranked and within a budget. [REQ08, REQ14, REQ16, AIR01]
- `src/agentic_robotics/hardware/domains.py`: Domain packs: the engineering knowledge of each of AIR's 19 domains (D01 to D19). [REQ03, D14, D15]
- `src/agentic_robotics/hardware/semantics.py`: Shared meaning for catalog data: vocabulary links, signal binding, WoT and JSON-LD export. [REQ03, REQ01, D16, D17]
- `src/agentic_robotics/hardware/validate.py`: Check robot manifests against the hardware catalog, and derive what to watch and maintain. [REQ03, REQ13, REQ16, D14]
- `src/agentic_robotics/health/__init__.py`: Health: expected-component aggregation, monitors, probes, readiness, diagnosis (ADR 0015). [REQ13, REQ14]
- `src/agentic_robotics/health/aggregate.py`: Health aggregation: expected items, STALE, worst-of rollup, "other", fast path. [REQ13, REQ14, G11]
- `src/agentic_robotics/health/cli.py`: ``air health``: derive expected items, aggregate readings, readiness, PSI, diagnosis. [REQ13, REQ14, REQ18]
- `src/agentic_robotics/health/diagnosis.py`: Diagnosis table: fault id -> debounce threshold, severity, delay before the error state [REQ14, G11]
- `src/agentic_robotics/health/gating.py`: Report vs gate: health restricts the capability report the runtime admits against. [REQ13, REQ14, G10, G11]
- `src/agentic_robotics/health/model.py`: Health contracts: levels, expected items, capability gates and reports (ADR 0015). [REQ13, REQ14, G10, G11]
- `src/agentic_robotics/health/monitors.py`: Rate and latency monitors (ROS ``diagnostic_updater`` FrequencyStatus/TimeStampStatus [REQ12, REQ13]
- `src/agentic_robotics/health/probes.py`: Host probes: Linux pressure stall information (PSI), read with the standard library. [REQ13, G10]
- `src/agentic_robotics/health/readiness.py`: Readiness gate: pluggable checks before activation (PX4 ``HealthAndArmingChecks`` [REQ13, REQ14, G10, G11, G13]
- `src/agentic_robotics/incidents/__init__.py`: Incidents: detection, novelty, hypotheses, bounded tests, recovery or handoff (ADR 0015). [REQ14]
- `src/agentic_robotics/incidents/cli.py`: ``air incident``: open, list, propose hypotheses, hand off and close incidents. [REQ14, REQ18]
- `src/agentic_robotics/incidents/model.py`: Incident contracts: detections, incidents, hypotheses and bounded tests (ADR 0015). [REQ14, REQ11, G11, G13]
- `src/agentic_robotics/incidents/pipeline.py`: Incident pipeline: detector -> incident -> hypotheses -> bounded tests -> recovery or [REQ14, REQ11, REQ17, G11, G13]
- `src/agentic_robotics/integrations/__init__.py`: Reference integrations. Each lives behind the public adapter protocol, so products
- `src/agentic_robotics/integrations/common.py`: Small helpers shared by the ecosystem bridges (ADR 0019). Standard library only. [REQ03, REQ12, REQ13]
- `src/agentic_robotics/integrations/dora/__init__.py`: dora-rs bridge (W6.1): AIR skills as a dora operator, and static dataflow checks. [REQ05, REQ18]
- `src/agentic_robotics/integrations/dora/dataflow.py`: Static checks for a dora dataflow before it runs (in the spirit of ``dora validate``). [REQ18]
- `src/agentic_robotics/integrations/dora/operator.py`: Expose AIR skills as a dora operator (W6.1 mapping layer, ADR 0019). [REQ05, REQ07, REQ12]
- `src/agentic_robotics/integrations/fake.py`: Fake mobile robot: a dependency-free adapter for quickstarts, tests and conformance. [REQ03, REQ13, REQ14]
- `src/agentic_robotics/integrations/fake_arm.py`: Fake stationary manipulator with an inspection station, for the two-robot reference [REQ03, REQ06]
- `src/agentic_robotics/integrations/lerobot/__init__.py`: LeRobot inference-contract mapping and policy client (no lerobot/torch import). [REQ11]
- `src/agentic_robotics/integrations/lerobot/client.py`: A LeRobot policy client over an injected transport: admit before motion, bounded waits, [REQ11, REQ12]
- `src/agentic_robotics/integrations/lerobot/contracts.py`: LeRobot inference contracts, mirrored in the standard library (mapping layer only). [REQ01, REQ11, REQ12]
- `src/agentic_robotics/integrations/microduck/__init__.py`: Pollen Robotics Microduck bridge: robotd JSON-RPC/NDJSON (W3.6, G02). Optional; nothing [REQ03, G02]
- `src/agentic_robotics/integrations/microduck/adapter.py`: Microduck RobotAdapter over robotd's JSON-RPC/NDJSON IPC (W3.6, gate G02). [REQ03, REQ05, REQ12, REQ13, REQ14]
- `src/agentic_robotics/integrations/microduck/client.py`: A JSON-RPC/NDJSON client for robotd with bounded waits (never blocks forever). [REQ03, REQ12]
- `src/agentic_robotics/integrations/microduck/fake_robotd.py`: A fake robotd that speaks the verified duck-ipc-proto subset (ADR 0019). [REQ03, REQ18]
- `src/agentic_robotics/integrations/microduck/protocol.py`: The robotd wire protocol, as verified from ``pollen-robotics/microduck`` [REQ03, REQ05]
- `src/agentic_robotics/integrations/microduck/transport.py`: Line transports for robotd: an injectable seam between the adapter and the socket. [REQ03]
- `src/agentic_robotics/integrations/openpi/__init__.py`: openpi policy client (websocket + msgpack) with timeouts and a staleness bound. [REQ11]
- `src/agentic_robotics/integrations/openpi/client.py`: An openpi policy client with timeouts, bounded reconnects and a staleness bound. [REQ11, REQ12]
- `src/agentic_robotics/integrations/openpi/fake_server.py`: A fake openpi websocket policy server (in-memory), following [REQ11, REQ18]
- `src/agentic_robotics/integrations/openpi/msgpack_codec.py`: A small standard-library MessagePack codec with openpi's ndarray convention. [REQ01]
- `src/agentic_robotics/integrations/piper/__init__.py`: AgileX Piper bridge over CAN (piper_sdk). Optional; ``piper_sdk`` is imported only when [REQ03]
- `src/agentic_robotics/integrations/piper/adapter.py`: AgileX Piper RobotAdapter over CAN through an injected layer (ADR 0019). [REQ03, REQ05, REQ12, REQ13]
- `src/agentic_robotics/integrations/piper/can.py`: The injectable CAN layer for the AgileX Piper arm (ADR 0019). [REQ03]
- `src/agentic_robotics/integrations/piper/fake_can.py`: A fake Piper behind :class:`~.can.CanLayer`, in SDK units (ADR 0019). [REQ03, REQ18]
- `src/agentic_robotics/integrations/ros2/__init__.py`: ROS 2 action bridge (W6.2). Optional; rclpy is imported lazily by [REQ03]
- `src/agentic_robotics/integrations/ros2/actions.py`: The injectable ROS 2 action layer (ADR 0019). [REQ03]
- `src/agentic_robotics/integrations/ros2/adapter.py`: ROS 2 action RobotAdapter (W6.2, ADR 0019). [REQ03, REQ05, REQ14, REQ17]
- `src/agentic_robotics/integrations/ros2/fake_actions.py`: A fake ROS 2 action graph behind :class:`~.actions.ActionLayer` (ADR 0019). [REQ03, REQ18]
- `src/agentic_robotics/integrations/unitree/__init__.py`: Unitree bridge over the unitree_sdk2 DDS channel model. Optional; no cyclonedds import [REQ03]
- `src/agentic_robotics/integrations/unitree/adapter.py`: Unitree RobotAdapter over the unitree_sdk2 DDS channel model (ADR 0019). [REQ03, REQ05, REQ12, REQ14]
- `src/agentic_robotics/integrations/unitree/api.py`: Unitree SDK2 channel-model constants, verified from ``unitreerobotics/unitree_sdk2_python`` [REQ03]
- `src/agentic_robotics/integrations/unitree/channels.py`: The injectable channel layer between the Unitree adapter and DDS. [REQ03]
- `src/agentic_robotics/integrations/unitree/fake_channels.py`: A fake Unitree DDS graph behind :class:`~.channels.ChannelLayer` (ADR 0019). [REQ03, REQ18]
- `src/agentic_robotics/integrations/vda5050/__init__.py`: VDA 5050 v2/v3 bridge over an injected MQTT publisher. Optional; paho-mqtt is imported [REQ03, REQ06]
- `src/agentic_robotics/integrations/vda5050/adapter.py`: VDA 5050 bridge: AIR commitments as orders and instant actions (ADR 0019). [REQ03, REQ06, REQ09, REQ14, REQ17]
- `src/agentic_robotics/integrations/vda5050/fake_vehicle.py`: A fake VDA 5050 vehicle on the other side of an injected MQTT publisher (ADR 0019). [REQ03, REQ18]
- `src/agentic_robotics/integrations/vda5050/messages.py`: VDA 5050 message builders and readers for v2 (2.1.0) and v3 (3.0.0). [REQ06, REQ09]
- `src/agentic_robotics/integrations/world.py`: Shared fake world for multi-robot reference scenarios: stations, parts and who [REQ03, REQ06]
- `src/agentic_robotics/maintenance/__init__.py`: Maintenance: prognostics feeding capability, readiness and triage (ADR 0015). [REQ16]
- `src/agentic_robotics/maintenance/cli.py`: ``air prognose``: estimate time-to-event from a degradation series; list estimates. [REQ16, REQ18]
- `src/agentic_robotics/maintenance/prognostics.py`: Prognostics: time-of-event estimates with uncertainty, gating capability on p5. [REQ16, G13]
- `src/agentic_robotics/mcp/__init__.py`: MCP server for AIR skills (W6.3). Tools are proposals that go through ``Runtime.submit``; [REQ05, REQ17]
- `src/agentic_robotics/mcp/cli.py`: ``air mcp``: list the MCP tools for a skill set, or serve AIR skills over stdio. [REQ18]
- `src/agentic_robotics/mcp/server.py`: An MCP server for AIR skills over stdio JSON-RPC (W6.3, ADR 0019). Standard library only. [REQ05, REQ17, REQ18]
- `src/agentic_robotics/plugins.py`: Adapter extension protocol and plugin discovery (baseline sections 5 and 19). [REQ01, REQ03, REQ14, REQ18]
- `src/agentic_robotics/provenance/__init__.py`: Traceability, provenance and maintenance records for every project built on the
- `src/agentic_robotics/provenance/attest.py`: in-toto attestations of AIR evidence, optionally wrapped in signed DSSE envelopes. [REQ10, F01]
- `src/agentic_robotics/provenance/canonical.py`: Canonical JSON (RFC 8785, JSON Canonicalization Scheme). [REQ10]
- `src/agentic_robotics/provenance/cli.py`: ``air`` provenance, traceability and debugging commands. [REQ02, REQ14, REQ15, REQ16, REQ18]
- `src/agentic_robotics/provenance/context_pack.py`: Context feeder: a generated, always-current briefing on a project for the people and [REQ08, REQ18]
- `src/agentic_robotics/provenance/explain.py`: Debugging aids: timelines and plain-language explanations of runtime outcomes. [REQ14, REQ15]
- `src/agentic_robotics/provenance/export_prov.py`: W3C PROV-JSON export of a project ledger. [REQ10, F07]
- `src/agentic_robotics/provenance/export_sdoc.py`: StrictDoc (``.sdoc``) export of the requirement matrix. [F01, REQ18]
- `src/agentic_robotics/provenance/interop_cli.py`: ``air`` commands for ledger integrity (checkpoints, proofs), attestations, exports, [REQ10, REQ16, REQ18, F05]
- `src/agentic_robotics/provenance/ledger.py`: Tamper-evident, append-only evidence ledger (baseline REQ10 and section 4). [REQ10, REQ02, REQ15, REQ16]
- `src/agentic_robotics/provenance/merkle.py`: RFC 9162 Merkle tree over the ledger, with C2SP signed checkpoints. [REQ10, REQ16]
- `src/agentic_robotics/provenance/model.py`: Provenance data model, following W3C PROV: activities *use* and *generate* entities [REQ02, REQ10, REQ16]
- `src/agentic_robotics/provenance/otel.py`: OpenTelemetry trace export without the OpenTelemetry SDK. [REQ15, REQ18, REQ02]
- `src/agentic_robotics/provenance/project.py`: Project provenance: one place where every research, development, simulation, [REQ02, REQ10, REQ15, REQ16, REQ18]
- `src/agentic_robotics/provenance/recording.py`: Recordings (MCAP, rosbag2, run logs, video, any file) as ledger entities. [REQ02, F05, F07]
- `src/agentic_robotics/provenance/repro.py`: Reproducibility lock (the ``dvc.lock`` pattern): what an activity's result depended on, [F05, REQ02]
- `src/agentic_robotics/provenance/runlog.py`: Self-describing run logs: a small, stdlib, JSON-lines format for robot and simulation [REQ02, F05, F07]
- `src/agentic_robotics/provenance/signing.py`: Signers and verifiers for checkpoints (C2SP signed notes) and DSSE envelopes. [REQ10]
- `src/agentic_robotics/provenance/sources.py`: Source catalog: open-source projects a project studies, and what it takes from each. [REQ16, REQ18, AIR01]
- `src/agentic_robotics/provenance/traceability.py`: Requirements traceability: requirement -> implementing code -> test evidence -> [REQ02, REQ18, F01]
- `src/agentic_robotics/provenance/tracing.py`: Causal trace context, compatible with W3C Trace Context and OpenTelemetry. [REQ02, REQ15]
- `src/agentic_robotics/quickstart.py`: Fake-device quickstart: one robot, an agent, a plan, and verified execution. [REQ18]
- `src/agentic_robotics/registry.py`: Local asset registry: the platform layer's storage contract (baseline section 18). [REQ10, REQ18, G15]
- `src/agentic_robotics/releases/__init__.py`: Policies, releases and signed update metadata (ADR 0016). [REQ10, REQ16, G14, G15]
- `src/agentic_robotics/releases/activation.py`: Release activation by trial-then-confirm (pattern from mcuboot's test/confirm swap). [REQ16, F04, G14, G15, D08, D17]
- `src/agentic_robotics/releases/cli.py`: ``air policy``, ``air release``, ``air params`` and ``air dataset`` commands. [REQ16, REQ18, F04, F10]
- `src/agentic_robotics/releases/policy_slot.py`: A robot's policy slot: load gate, origin authority, two-step start, checked inference, [REQ05, REQ11, REQ12, REQ16, REQ17, G05, G12, G15]
- `src/agentic_robotics/releases/records.py`: Ledger records for engineering assets: parameter snapshots, episodes, training runs, [REQ02, REQ10, REQ16, F04, F05, G03]
- `src/agentic_robotics/releases/signing.py`: Signing keys for release metadata. [REQ10, G15, D15]
- `src/agentic_robotics/releases/tuf.py`: TUF-style signed metadata over registry assets, plus an Uptane-like director. [REQ10, REQ16, F04, G14, G15, D15, D17]
- `src/agentic_robotics/robot.py`: A Robot: a physical or simulated system identified by its manifest and served by an [REQ05]
- `src/agentic_robotics/runtime/__init__.py`: Local runtime: admission, authority, fenced resources, execution and verification.
- `src/agentic_robotics/runtime/admission.py`: Admission checks: the gate between a proposed command and execution. [REQ12, REQ13, REQ14, REQ17]
- `src/agentic_robotics/runtime/authority.py`: Authority service: grants, checks and revokes delegations (baseline section 17). [REQ17]
- `src/agentic_robotics/runtime/holds.py`: Token-scoped holds on a robot's own subsystems (a skill's ``occupies``). [REQ05, REQ06, REQ14]
- `src/agentic_robotics/runtime/journal.py`: Durable command intent journal (W2.1, fixes survey defect D2). [REQ07, REQ14]
- `src/agentic_robotics/runtime/plans.py`: Plan execution: turns a typed :class:`TaskPlan` into fenced, authorized commands. [REQ09, REQ06, REQ07, REQ14]
- `src/agentic_robotics/runtime/resources.py`: Exclusive resource ownership with fencing tokens (baseline section 11). [REQ06, REQ09, G06]
- `src/agentic_robotics/runtime/runtime.py`: The local runtime: admission, execution ownership, interruption and verification. [REQ05, REQ07, REQ12, REQ14, REQ17]
- `src/agentic_robotics/runtime/state.py`: Command state machine with explicit legal transitions. [REQ07]
- `src/agentic_robotics/runtime/trace.py`: Structured execution trace: the evidence log for decisions, commands and outcomes. [REQ02, REQ15]
- `src/agentic_robotics/scenarios.py`: Reference scenarios built only on public APIs (baseline section 20). [REQ06, REQ09, REQ17, REQ18]
- `src/agentic_robotics/skills.py`: Skill registry: discovery and lookup of declared skills. [REQ05, REQ18]
- `src/agentic_robotics/testing/__init__.py`: Testing support for projects built on the framework (pytest evidence plugin).
- `src/agentic_robotics/testing/pytest_plugin.py`: pytest plugin: turns test results into requirement evidence. [REQ02, REQ18, REQ10]
- `src/agentic_robotics/triage/__init__.py`: Triage: one queue for everything AIR finds wrong (ADR 0009).
- `src/agentic_robotics/triage/book.py`: The triage book: an event-sourced queue over the project ledger (ADR 0009). [REQ14, REQ15, REQ17, G11, G12]
- `src/agentic_robotics/triage/cli.py`: ``air triage``: sweep, list, decide and silence triage items. [REQ14, REQ15, REQ17]
- `src/agentic_robotics/triage/collectors.py`: Collectors turn AIR's existing signals into triage signals (ADR 0009). [REQ14, REQ15, REQ17, G11]
- `src/agentic_robotics/triage/contracts.py`: Triage contracts: signals in, tracked items out (ADR 0009). [REQ14, REQ15, REQ17]
- `src/agentic_robotics/triage/operations.py`: Operations collector: health, readiness, incidents and prognostics into triage (ADR 0015). [REQ14, REQ15, REQ16, G11, G13]
- `src/agentic_robotics/triage/policy.py`: Triage policy as data: routing rules, inhibitions and silences (ADR 0009). [REQ14, REQ15, REQ17]
- `src/agentic_robotics/work/__init__.py`: Agent-performed engineering work (research, development, simulation, evaluation,
- `src/agentic_robotics/work/agent.py`: Work agents: research, development, simulation, evaluation, maintenance and diagnosis [AIR01, AIR03, REQ02, REQ04, REQ05, REQ11, REQ16, REQ17]
- `src/agentic_robotics/work/approval.py`: Approvals for work-agent tool calls, hardened (catalog W2.5). [AIR01, AIR03, REQ02, REQ04, G15]
- `src/agentic_robotics/work/cli.py`: ``air work`` and ``air models``: run agent work in sandboxes, review proposals. [AIR01, REQ02, REQ18]
- `src/agentic_robotics/work/contracts.py`: Contracts for agent-performed engineering work (baseline sections 8, 9, 13, 16). [AIR01, AIR03, REQ02, REQ04, REQ11, REQ16, REQ17]
- `src/agentic_robotics/work/egress.py`: Egress policy as data, and the stdlib proxy that enforces it. [AIR01, REQ02, REQ04, G15]
- `src/agentic_robotics/work/execpolicy.py`: Command policy rules for the ``run`` tool (pattern from openai/codex execpolicy, [AIR01, REQ02, REQ04, G15]
- `src/agentic_robotics/work/netbridge.py`: In-sandbox bridge: TCP 127.0.0.1:PORT -> a bind-mounted Unix socket (the egress proxy). [AIR01, REQ02, G15]
- `src/agentic_robotics/work/remote.py`: Remote and microVM sandboxes: one adapter, many providers. [AIR01, REQ02, REQ04, G15, F07]
- `src/agentic_robotics/work/risk.py`: Risk analysis, kept separate from the confirmation decision (pattern from OpenHands [AIR01, REQ02, REQ04, G15]
- `src/agentic_robotics/work/sandbox.py`: Sandboxes where agent work executes: research scripts, development builds and tests, [AIR01, REQ02, REQ04, G15]
- `src/agentic_robotics/work/tools.py`: Tools available to work agents. Each declares its side effect and an input schema; [AIR01, AIR03, REQ02, REQ04, REQ16, REQ17]
- `src/agentic_robotics/work/winjob.py`: Windows Job Objects through ``ctypes`` (standard library only). [AIR01, REQ02, G15]

## Contracts (schema/version: fields)

- `action_chunk/1.0` (ActionChunk): actions, provenance, created_at, max_age_s, execution_steps
- `action_space/1.0` (ActionSpace): space_id, slots, description
- `activity/1.1` (Activity): activity_id, kind, title, status, started_at, ended_at, actors, requirements, used, generated, parent_id, trace_id, params, environment, metrics, notes, outcome, error
- `approval_decision/1.0` (ApprovalDecision): command_id, decision, approver, authority_revision, args_digest, decided_at, delegation_id, note
- `approval_request/1.0` (ApprovalRequest): command_id, robot_id, skill_id, issued_by, args, args_digest, authority_revision, requested_at, expires_at, reason
- `asset_card/1.0` (AssetCard): asset, kind, title, license, summary, intended_use, out_of_scope_use, limitations, hazards, operating_envelope, evaluations, conformance_report, requirements, maintainers, derived_from, superseded_by
- `backend_info/1.0` (BackendInfo): backend, version, build_ids, platform, requested_device, actual_device, capabilities, time_policy, resolved_physics_dt, resolved_output_interval, features, out_of_process, non_reproducible
- `benchmark_validity/1.0` (BenchmarkValidity): validity_id, benchmark, sim_scores, real_scores, mmrv, pearson, max_mmrv, min_pearson, min_policies, valid, reasons, backend, backend_version, platform, real_evidence, assessed_at, notes
- `bounded_test/1.0` (BoundedTest): test_id, hypothesis_id, title, steady_state, method, rollbacks, rollback_strategy, budget, predicts_deviation, max_input_age_s
- `cancel_response/1.0` (CancelResponse): command_id, code, state, started, stop_confirmed, message
- `capability_report/1.0` (CapabilityReport): robot_id, capability_revision, observed_at, clock_domain, profiles
- `command/1.2` (Command): command_id, robot_id, skill_id, issued_by, delegation_id, authority_revision, args, capability_revision, fencing, task_id, plan_id, trace_id, retry_of
- `command_policy/1.0` (CommandPolicy): rules, default
- `component_manifest/1.1` (ComponentManifest): robot_id, name, hardware_revision, adapter, components, nominal_capabilities, frames, description, robot_class
- `conformance_report/1.0` (ConformanceReportRecord): robot_id, adapter_kind, suite_version, air_version, passed, outcomes, contract_versions, manifest_digest, skills_digest, generated_at
- `coordination_message/1.1` (CoordinationMessage): message_id, conversation_id, task_id, task_revision, sender, recipients, kind, sent_at, causal_ref, expires_at, payload, evidence, trace_id, seq, update_id, hlc
- `delegation/1.0` (Delegation): delegation_id, grantor, grantee, robots, skills, operations, parent_id, valid_from, valid_until, may_redelegate, limits, reason
- `device_catalog/1.0` (DeviceCatalog): name, device_types, robot_classes, protocols
- `diagnosis_table/1.0` (DiagnosisTable): entries
- `domain_library/1.0` (DomainLibrary): domains
- `entity_ref/1.0` (EntityRef): entity_id, kind, name, version, digest, uri, size_bytes, physical, attributes
- `episode/1.0` (Episode): metadata, steps
- `execution_contract/1.1` (ExecutionContract): trigger, deadline_s, deadline_kind, period_s, max_input_age_s, result_validity_s, jitter_s, queue_policy, compute_budget, memory_budget_mb, allowed_clock_states, fallback, keepalive_period_s, exit_mode
- `execution_result/1.1` (ExecutionResult): command_id, robot_id, skill_id, state, reason, message, observed_effects, verification, authority_revision, capability_revision, started_at, finished_at, failure, retriable, applied_args, limited_by, evidence, retry_of
- `experiment_record/1.1` (ExperimentRecord): experiment_id, hypothesis, requirement_ids, candidate, scenario, backend, backend_version, model_fidelity, seed, numerical_settings, resource_limits, acceptance_criteria, results, accepted, limitations, seeds, backend_info, config, scenario_params, platform, runner_version, physics_fingerprint, reproducibility, search_id, trial_key, status, status_reason, budget
- `golden_run/1.0` (GoldenRun): name, platform, backend, backend_version, runner_version, time_policy, seed, steps, fingerprint, scenario, config, metrics, recorded_at
- `health_manifest/1.0` (HealthManifest): robot_id, expected, gates, robot_owned_recovery, default_timeout_s
- `health_report/1.0` (HealthReport): robot_id, observed_at, level, items, groups, other, critical
- `human_intervention/1.0` (HumanIntervention): intervention_id, operator, kind, robots, state, revoked_delegations, command_owner, physically_confirmed, handback_conditions
- `incident/1.0` (Incident): incident_id, robot_id, detected_at, detector, signal, summary, signature, novel, known_classes, component_id, state, occurrences, hypotheses, tests, recovery, handed_off_to, resolution, labels, evidence
- `maintenance_record/1.0` (MaintenanceRecord): record_id, robot_id, components, reason, maintenance_type, operating_mode, procedure, performed_by, findings, replacements, calibration_invalidated, return_to_service_checks, exposure
- `mission/1.0` (Mission): mission_id, goal, tasks
- `model_catalog/1.0` (ModelCatalog): models
- `observation/1.1` (Observation): source, signal, clock_domain, acquired_at, received_at, value, unit, frame, spatial, quality, uncertainty, artifact_ref, origin, association_quality, covariance, score
- `parameter_schema/1.0` (ParameterSchema): name, parameters, applies_to, description
- `parameter_set/1.0` (ParameterSet): robot_id, values, schema_name, captured_at, source
- `policy_manifest/1.0` (PolicyManifest): policy_id, version, model_digest, model_api, model_format, inference_backend, robot_model, observation_features, action_feature, prediction_steps, execution_steps, action_interval_s, max_action_age_s, exit_mode, origin, joint_order, kp, kd, observation, action_scale, action_low, action_high, default_joint_positions, source, training_run, derived_from, description
- `prognostic_estimate/1.0` (PrognosticEstimate): estimate_id, robot_id, component_id, event, signal, threshold, direction, estimated_at, event_state, time_of_event, horizon_s, loading, method, seed, inputs_n, validation
- `readiness_report/1.0` (ReadinessReport): robot_id, evaluated_at, modes, can_run, failsafe, level, results, checks_run
- `release/1.0` (Release): release_id, name, version, security_counter, targets, confirm_checks, confirm_within_s, notes, created_at
- `release_activation/1.0` (ReleaseActivation): robot_id, slot, state, active, previous, pending, security_counter, trial_started_at, trial_boots, history
- `release_selection/1.0` (ReleaseSelection): search_id, held_out_seeds, considered, revalidated, rejected, selected, rationale, activity_id
- `reproducibility_report/1.0` (ReproducibilityReport): backend, backend_version, platform, seed, steps, tolerance, outcome, max_abs_diff, declared_determinism, rng_state_match, reset_repeatable, buffer_reuse, restore_check, findings, config
- `requirement_set/1.0` (RequirementSet): name, requirements
- `resource_reservation/1.2` (ResourceReservation): reservation_id, resource, holder, fencing_token, state, exclusive, lease_expires_at, physical_occupancy, last_confirmed_at, reconciliation_policy, loan, claim_id, mode
- `sandbox_policy/1.1` (SandboxPolicy): network, allowed_commands, env_allowlist, max_wall_s, max_memory_mb, max_cpus, max_output_bytes, require_enforced, mode, egress, protected_paths, deny_read
- `search_record/1.0` (SearchRecord): search_id, title, objectives, space, sampler, sampler_seed, sampler_version, constraints, scenarios, seeds, budget, max_trials, status, status_reason, trials, candidates, pareto, activity_id
- `skill_set/1.1` (SkillSet): skills
- `skill_spec/1.1` (SkillSpec): skill_id, version, description, inputs, required_capabilities, preconditions, postconditions, exclusive_resources, side_effects, interruption, execution, expected_evidence, on_uncertain_outcome, experimental, occupies, blocking, skip_if, requires_approval, approval_timeout_s, retriable_failures, workspace, max_relative_target
- `source_catalog/1.0` (SourceCatalog): name, updated, sources
- `task_plan/1.1` (TaskPlan): plan_id, goal, steps, produced_by, rationale, trace_id
- `team_plan/1.1` (TeamPlan): plan_id, mission, assignments, unassigned, trace_id, release_window
- `training_run/1.0` (TrainingRun): run_id, algorithm, git, nan_check, seed, config, datasets, outputs, started_at, ended_at, steps, metrics, environment
- `triage_item/1.0` (TriageItem): item_id, fingerprint, kind, source, subject, title, severity, priority, state, rule, owner, assignee, escalation, escalation_level, labels, robot_id, requirements, evidence, first_seen, last_seen, count, ack_due, resolve_due, resolve_escalated, suppressed_by, resolution, grouping_version, missing_group_labels, content_hash, priority_locked, snooze_until, wake_after_count, stale
- `triage_policy/1.0` (TriagePolicy): rules, inhibitions, default, max_silence_s, max_snooze_s
- `unified_action/1.0` (UnifiedAction): space_id, values, mask
- `work_proposal/1.0` (Proposal): proposal_id, task_id, kind, title, summary, artifacts, evidence, requirements, produced_by, status
- `work_task/1.1` (WorkTask): task_id, kind, goal, requirements, inputs, deliverables, acceptance, tools, approval_required, data_classification, budget, sandbox, models, robot_id, command_policy, confirm_risk, approval_mode

## Decisions

- [0001. Isolated Python 3.12 environment managed by uv](docs/decisions/0001-isolated-python-env.md): Accepted
- [0002. Standard-library core and strict, versioned contracts](docs/decisions/0002-stdlib-core-strict-contracts.md): Accepted
- [0003. Separate reported, verified and unresolved outcomes](docs/decisions/0003-verified-vs-unresolved-outcomes.md): Accepted
- [0004. AGPL-3.0 license](docs/decisions/0004-agpl-license.md): Accepted (open for review)
- [0005. Provenance spine: PROV model, hash-chained ledger, evidence-backed claims](docs/decisions/0005-provenance-spine.md): Accepted
- [0006. Team coordination: transactional coordinator, revisions, occupancy-aware release](docs/decisions/0006-team-coordination.md): Accepted
- [0007. Wave 1: fail-closed leases, verifiable registry, ledger v2, W3C ids, schemas](docs/decisions/0007-wave1-integrity-and-formats.md): Accepted
- [0008. Agentic engineering work in sandboxes, with provider-agnostic models](docs/decisions/0008-agentic-engineering-work.md): Accepted
- [0009. Triage: one queue for everything AIR finds wrong](docs/decisions/0009-triage.md): Accepted
- [0010. A typed hardware catalog for robots, devices and protocols](docs/decisions/0010-hardware-catalog.md): Accepted
- [0011. Hardware semantics, domain packs, task context and custom components](docs/decisions/0011-hardware-semantics-and-context.md): Accepted
- [0012. Runtime commitment semantics](docs/decisions/0012-runtime-commitments.md): Accepted
- [0013. Provenance integrity (Merkle checkpoints) and interoperable exports](docs/decisions/0013-provenance-integrity-and-exports.md): Accepted
- [0014. Engineering and optimization loop (baseline stage 4)](docs/decisions/0014-engineering-loop.md): Accepted
- [0015. Operations: health, readiness, incidents and prognostics](docs/decisions/0015-health-incidents-prognostics.md): Accepted
- [0016. Policies, releases and engineering-domain contracts](docs/decisions/0016-policies-releases-contracts.md): Accepted
- [0017. Enforcing sandboxes and agent tool policy](docs/decisions/0017-enforcing-sandboxes.md): Accepted
- [0018. Team cancellation, released prefix, liveness and scoped resource loans](docs/decisions/0018-coordination-liveness-resources.md): Accepted
- [0019. Ecosystem bridges, the MCP server and repository hygiene](docs/decisions/0019-ecosystem-bridges.md): Accepted

## Recent recorded activities

- 2026-10-22 development: research backlog: cross-group alignment (succeeded) [REQ07, REQ09, REQ17, G07]
- 2026-10-22 development: research backlog: ecosystem bridges, MCP server and repo hygiene (succeeded) [G02, REQ03, REQ05, REQ18]
- 2026-10-22 development: research backlog: coordination liveness and resources (succeeded) [REQ06, REQ09, G06, G07, G14]
- 2026-10-22 development: research backlog: enforcing sandboxes and tool policy (succeeded) [AIR01, AIR03, REQ17, D15]
- 2026-10-22 development: research backlog: policies, releases and engineering contracts (succeeded) [REQ11, REQ16, G03, G15]
- 2026-10-22 development: research backlog: health, incidents and prognostics (succeeded) [REQ13, REQ14, REQ16, G10, G11, G13]
- 2026-10-22 development: research backlog: engineering and optimization loop (succeeded) [REQ02, REQ04, G03, G04]
- 2026-10-22 development: research backlog: provenance integrity and exports (succeeded) [REQ10, REQ15, REQ16, F01]
- 2026-10-22 development: research backlog: runtime commitment semantics (succeeded) [REQ07, REQ12, REQ14, REQ17, G05, G07, G12]
- 2026-10-22 development: hardware context: custom components, semantics, domain packs, task context (succeeded) [REQ03, REQ08, REQ14, REQ16, D14, D15, D16]
- 2026-10-22 development: hardware catalog: robot classes, device types, protocols (succeeded) [REQ03, REQ13, REQ16, D05, D07, D14, D16]
- 2026-10-22 development: triage review fixes: full fingerprints, content hash, severity rise, priority lock, snooze, stale, silence limits (succeeded) [REQ14, REQ15, REQ17, G11, G12]
- 2026-10-22 development: triage: one queue for everything AIR finds wrong (ADR 0009) (succeeded) [REQ14, REQ15, REQ17, G11, G12]
- 2026-10-22 development: ar.source_catalog/1.0 contract and air sources CLI (succeeded) [REQ16, REQ18]
- 2026-10-22 research: open-source source catalog: 343 projects in 8 areas (succeeded) [REQ16, REQ18, AIR01]

## Open triage

3 open item(s); run `air triage sweep` to refresh and `air triage list` for details.

- `tri_994f14ffe2364496` hygiene/P4 new, role:maintainer: qemu/qemu: optional integration with an AGPL-incompatible license (GPL-2.0); keep it a separate process or tool (x2)
- `tri_b95ad4742ff34361` hygiene/P4 new, role:maintainer: eclipse-ditto/ditto: optional integration with an AGPL-incompatible license (EPL-2.0); keep it a separate process or tool (x2)
- `tri_ee280d984b604722` hygiene/P4 new, role:maintainer: IDEA-Research/GroundingDINO: no push since 2024-08-12 (x2)

## Ledger

- entries: 775, integrity: OK
- head: `e0447b6c38f293b8`

## Recent commits

- `a538297` Merge group 8: ecosystem bridges, MCP server, repo hygiene (2026-10-22)
- `a6bb0c5` Add ecosystem bridges, MCP server and repo hygiene (ADR 0019) (2026-10-22)
- `591f02f` Align coordination with runtime commitment semantics (2026-10-22)
- `8f2841c` Merge group 7: coordination liveness and resources (2026-10-22)
- `fa8bf2f` Merge group 6: enforcing sandboxes and tool policy (2026-10-22)
- `4b7631c` Tidy optional-dependency comments after merge (2026-10-22)
- `bc9b2f8` Merge group 1: runtime commitment semantics (2026-10-22)
- `d4e9c79` Coordination: cancellation, released prefix, liveness, scoped resource loans (2026-10-22)
- `10667a2` Merge group 2: provenance integrity and exports (2026-10-22)
- `cabb191` Merge group 4: health, incidents, prognostics (2026-10-22)
