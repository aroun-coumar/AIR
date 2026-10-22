# Context pack: agentic-robotics

Generated 2026-10-22 10:05 UTC at commit `8a0629e` by `air context`. Do not edit by hand; regenerate it.

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

Evidence status for REQ and gate items: evidenced 30, implemented 1, untraced 5

Open (not yet claimed verified):

- REQ02 End-to-end research, development, simulation, evaluation and implementation support: claimed in_progress, evidence evidenced
- REQ03 Coverage across the 19 hardware, software, infrastructure and environment domains: claimed in_progress, evidence evidenced
- REQ04 Agentic optimization and configuration down to hardware: claimed planned, evidence evidenced
- REQ06 Multiple software agents within a robot and coordination among physical robots: claimed in_progress, evidence evidenced
- REQ07 Intelligent conversation leading to executable commitments and verified outcomes: claimed in_progress, evidence evidenced
- REQ08 Shared memory and context reuse to reduce repeated model input and reasoning: claimed in_progress, evidence evidenced
- REQ09 Fast task decomposition, allocation, prioritization, dispatch and selective acknowledgement: claimed in_progress, evidence evidenced
- REQ11 Guided and anticipatory transfer of learning with context enrichment: claimed planned, evidence implemented
- REQ12 Real-time inference, execution and synchronization of robots and features: claimed in_progress, evidence evidenced
- REQ13 Hardware bottleneck awareness and appropriate adaptation: claimed in_progress, evidence evidenced
- REQ14 Detection, investigation, learning and handling of unfamiliar failures: claimed in_progress, evidence evidenced
- REQ15 Robot and team monitoring linked to mission consequences: claimed in_progress, evidence evidenced
- REQ16 Maintenance and component lifecycle management: claimed in_progress, evidence evidenced
- REQ17 Human intervention, override, bounded delegation, takeover and handback: claimed in_progress, evidence evidenced
- G02 Heterogeneous integration: claimed planned, evidence untraced
- G03 Engineering traceability: claimed planned, evidence untraced
- G04 Constrained optimization: claimed planned, evidence untraced
- G05 Timing and freshness: claimed in_progress, evidence evidenced
- G07 Typed commitments: claimed in_progress, evidence evidenced
- G08 Context efficiency: claimed in_progress, evidence evidenced
- G09 Qualified handoff: claimed planned, evidence untraced
- G10 Hardware adaptation: claimed in_progress, evidence evidenced
- G11 Unfamiliar failure: claimed planned, evidence untraced
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
- `src/agentic_robotics/conformance.py`: Adapter conformance suite (baseline sections 4 and 7, "Plugin conformance"). [REQ03, REQ18]
- `src/agentic_robotics/contracts/__init__.py`: Public data contracts. Every contract is versioned and round-trips through JSON.
- `src/agentic_robotics/contracts/authority.py`: Delegation and human intervention contracts (baseline sections 6 and 17). [REQ01, REQ17]
- `src/agentic_robotics/contracts/capability.py`: Capability reports (baseline sections 6 and 15). [REQ01, REQ13]
- `src/agentic_robotics/contracts/execution.py`: Execution contracts, commands and results (baseline sections 6 and 14). [REQ01, REQ07, REQ12]
- `src/agentic_robotics/contracts/manifest.py`: Robot and component manifests (baseline section 6, ComponentManifest). [REQ01, REQ03, REQ16]
- `src/agentic_robotics/contracts/observation.py`: Observation contract (baseline section 6). [REQ01, REQ12]
- `src/agentic_robotics/contracts/resources.py`: Resource reservations with fencing (baseline sections 6 and 11). [REQ01, REQ06, REQ09]
- `src/agentic_robotics/contracts/skill.py`: Skill and task specifications (baseline section 6). [REQ01, REQ05]
- `src/agentic_robotics/coordination/__init__.py`: Multi-robot coordination: typed commitment protocol, allocation, fenced reservations,
- `src/agentic_robotics/coordination/allocation.py`: Task allocation. Products can supply their own :class:`Allocator`; the coordinator [REQ09]
- `src/agentic_robotics/coordination/coordinator.py`: Team coordinator: a transactional coordinator within one authority domain [REQ06, REQ07, REQ09, REQ10]
- `src/agentic_robotics/coordination/participant.py`: Participant: the robot-side half of team coordination. [REQ06, REQ07, REQ09]
- `src/agentic_robotics/coordination/protocol.py`: Team coordination protocol (baseline section 11). [REQ06, REQ07, REQ09]
- `src/agentic_robotics/coordination/team.py`: Team: the convenience entry point for multi-robot work (baseline section 7 API shape). [REQ06, REQ09, REQ18]
- `src/agentic_robotics/coordination/transport.py`: Message transport between coordinator and participants. [REQ06, REQ09]
- `src/agentic_robotics/core/__init__.py`: Shared core: identities, schemas, units, frames, clocks and errors.
- `src/agentic_robotics/core/clock.py`: Clock abstractions. [REQ01, REQ12]
- `src/agentic_robotics/core/deprecation.py`: Deprecation and stability markers (see ``docs/policy/versioning.md``). [REQ18]
- `src/agentic_robotics/core/errors.py`: Framework error hierarchy. [REQ01]
- `src/agentic_robotics/core/ids.py`: Identity helpers. [REQ01]
- `src/agentic_robotics/core/jsonschema.py`: JSON Schema (Draft 2020-12) for every contract, generated from the dataclass hints. [REQ01, REQ18, F02, F06]
- `src/agentic_robotics/core/schema.py`: Versioned, typed contract base. [REQ01, REQ18]
- `src/agentic_robotics/core/units.py`: Minimal units and coordinate-frame support. [REQ01]
- `src/agentic_robotics/core/versions.py`: Version precedence shared by the registry, deprecations and compatibility checks. [REQ18]
- `src/agentic_robotics/integrations/__init__.py`: Reference integrations. Each lives behind the public adapter protocol, so products
- `src/agentic_robotics/integrations/fake.py`: Fake mobile robot: a dependency-free adapter for quickstarts, tests and conformance. [REQ03, REQ13]
- `src/agentic_robotics/integrations/fake_arm.py`: Fake stationary manipulator with an inspection station, for the two-robot reference [REQ03, REQ06]
- `src/agentic_robotics/integrations/world.py`: Shared fake world for multi-robot reference scenarios: stations, parts and who [REQ03, REQ06]
- `src/agentic_robotics/plugins.py`: Adapter extension protocol and plugin discovery (baseline sections 5 and 19). [REQ01, REQ03, REQ18]
- `src/agentic_robotics/provenance/__init__.py`: Traceability, provenance and maintenance records for every project built on the
- `src/agentic_robotics/provenance/canonical.py`: Canonical JSON (RFC 8785, JSON Canonicalization Scheme). [REQ10]
- `src/agentic_robotics/provenance/cli.py`: ``air`` provenance, traceability and debugging commands. [REQ02, REQ14, REQ15, REQ16, REQ18]
- `src/agentic_robotics/provenance/context_pack.py`: Context feeder: a generated, always-current briefing on a project for the people and [REQ08, REQ18]
- `src/agentic_robotics/provenance/explain.py`: Debugging aids: timelines and plain-language explanations of runtime outcomes. [REQ14, REQ15]
- `src/agentic_robotics/provenance/ledger.py`: Tamper-evident, append-only evidence ledger (baseline REQ10 and section 4). [REQ10, REQ02, REQ15, REQ16]
- `src/agentic_robotics/provenance/model.py`: Provenance data model, following W3C PROV: activities *use* and *generate* entities [REQ02, REQ10, REQ16]
- `src/agentic_robotics/provenance/project.py`: Project provenance: one place where every research, development, simulation, [REQ02, REQ10, REQ15, REQ16, REQ18]
- `src/agentic_robotics/provenance/sources.py`: Source catalog: open-source projects a project studies, and what it takes from each. [REQ16, REQ18, AIR01]
- `src/agentic_robotics/provenance/traceability.py`: Requirements traceability: requirement -> implementing code -> test evidence -> [REQ02, REQ18, F01]
- `src/agentic_robotics/provenance/tracing.py`: Causal trace context, compatible with W3C Trace Context and OpenTelemetry. [REQ02, REQ15]
- `src/agentic_robotics/quickstart.py`: Fake-device quickstart: one robot, an agent, a plan, and verified execution. [REQ18]
- `src/agentic_robotics/registry.py`: Local asset registry: the platform layer's storage contract (baseline section 18). [REQ10, REQ18, G15]
- `src/agentic_robotics/robot.py`: A Robot: a physical or simulated system identified by its manifest and served by an [REQ05]
- `src/agentic_robotics/runtime/__init__.py`: Local runtime: admission, authority, fenced resources, execution and verification.
- `src/agentic_robotics/runtime/admission.py`: Admission checks: the gate between a proposed command and execution. [REQ12, REQ13, REQ17]
- `src/agentic_robotics/runtime/authority.py`: Authority service: grants, checks and revokes delegations (baseline section 17). [REQ17]
- `src/agentic_robotics/runtime/plans.py`: Plan execution: turns a typed :class:`TaskPlan` into fenced, authorized commands. [REQ09, REQ06, REQ07]
- `src/agentic_robotics/runtime/resources.py`: Exclusive resource ownership with fencing tokens (baseline section 11). [REQ06, REQ09]
- `src/agentic_robotics/runtime/runtime.py`: The local runtime: admission, execution ownership, interruption and verification. [REQ05, REQ07, REQ12, REQ14, REQ17]
- `src/agentic_robotics/runtime/state.py`: Command state machine with explicit legal transitions. [REQ07]
- `src/agentic_robotics/runtime/trace.py`: Structured execution trace: the evidence log for decisions, commands and outcomes. [REQ02, REQ15]
- `src/agentic_robotics/scenarios.py`: Reference scenarios built only on public APIs (baseline section 20). [REQ06, REQ09, REQ18]
- `src/agentic_robotics/skills.py`: Skill registry: discovery and lookup of declared skills. [REQ05, REQ18]
- `src/agentic_robotics/testing/__init__.py`: Testing support for projects built on the framework (pytest evidence plugin).
- `src/agentic_robotics/testing/pytest_plugin.py`: pytest plugin: turns test results into requirement evidence. [REQ02, REQ18]
- `src/agentic_robotics/work/__init__.py`: Agent-performed engineering work (research, development, simulation, evaluation,
- `src/agentic_robotics/work/agent.py`: Work agents: research, development, simulation, evaluation, maintenance and diagnosis [AIR01, AIR03, REQ02, REQ04, REQ05, REQ11, REQ16, REQ17]
- `src/agentic_robotics/work/cli.py`: ``air work`` and ``air models``: run agent work in sandboxes, review proposals. [AIR01, REQ02, REQ18]
- `src/agentic_robotics/work/contracts.py`: Contracts for agent-performed engineering work (baseline sections 8, 9, 13, 16). [AIR01, AIR03, REQ02, REQ04, REQ11, REQ16, REQ17]
- `src/agentic_robotics/work/sandbox.py`: Sandboxes where agent work executes: research scripts, development builds and tests, [AIR01, REQ02, REQ04, G15]
- `src/agentic_robotics/work/tools.py`: Tools available to work agents. Each declares its side effect and an input schema; [AIR01, AIR03, REQ02, REQ04, REQ16, REQ17]

## Contracts (schema/version: fields)

- `activity/1.1` (Activity): activity_id, kind, title, status, started_at, ended_at, actors, requirements, used, generated, parent_id, trace_id, params, environment, metrics, notes, outcome, error
- `capability_report/1.0` (CapabilityReport): robot_id, capability_revision, observed_at, clock_domain, profiles
- `command/1.1` (Command): command_id, robot_id, skill_id, issued_by, delegation_id, authority_revision, args, capability_revision, fencing, task_id, plan_id, trace_id
- `component_manifest/1.0` (ComponentManifest): robot_id, name, hardware_revision, adapter, components, nominal_capabilities, frames, description
- `coordination_message/1.0` (CoordinationMessage): message_id, conversation_id, task_id, task_revision, sender, recipients, kind, sent_at, causal_ref, expires_at, payload, evidence, trace_id
- `delegation/1.0` (Delegation): delegation_id, grantor, grantee, robots, skills, operations, parent_id, valid_from, valid_until, may_redelegate, limits, reason
- `entity_ref/1.0` (EntityRef): entity_id, kind, name, version, digest, uri, size_bytes, physical, attributes
- `execution_contract/1.0` (ExecutionContract): trigger, deadline_s, deadline_kind, period_s, max_input_age_s, result_validity_s, jitter_s, queue_policy, compute_budget, memory_budget_mb, allowed_clock_states, fallback
- `execution_result/1.0` (ExecutionResult): command_id, robot_id, skill_id, state, reason, message, observed_effects, verification, authority_revision, capability_revision, started_at, finished_at
- `experiment_record/1.0` (ExperimentRecord): experiment_id, hypothesis, requirement_ids, candidate, scenario, backend, backend_version, model_fidelity, seed, numerical_settings, resource_limits, acceptance_criteria, results, accepted, limitations
- `human_intervention/1.0` (HumanIntervention): intervention_id, operator, kind, robots, state, revoked_delegations, command_owner, physically_confirmed, handback_conditions
- `maintenance_record/1.0` (MaintenanceRecord): record_id, robot_id, components, reason, maintenance_type, operating_mode, procedure, performed_by, findings, replacements, calibration_invalidated, return_to_service_checks, exposure
- `mission/1.0` (Mission): mission_id, goal, tasks
- `model_catalog/1.0` (ModelCatalog): models
- `observation/1.0` (Observation): source, signal, clock_domain, acquired_at, received_at, value, unit, frame, spatial, quality, uncertainty, artifact_ref
- `requirement_set/1.0` (RequirementSet): name, requirements
- `resource_reservation/1.1` (ResourceReservation): reservation_id, resource, holder, fencing_token, state, exclusive, lease_expires_at, physical_occupancy, last_confirmed_at, reconciliation_policy
- `sandbox_policy/1.0` (SandboxPolicy): network, allowed_commands, env_allowlist, max_wall_s, max_memory_mb, max_cpus, max_output_bytes, require_enforced
- `skill_set/1.0` (SkillSet): skills
- `skill_spec/1.0` (SkillSpec): skill_id, version, description, inputs, required_capabilities, preconditions, postconditions, exclusive_resources, side_effects, interruption, execution, expected_evidence, on_uncertain_outcome, experimental
- `source_catalog/1.0` (SourceCatalog): name, updated, sources
- `task_plan/1.1` (TaskPlan): plan_id, goal, steps, produced_by, rationale, trace_id
- `team_plan/1.0` (TeamPlan): plan_id, mission, assignments, unassigned, trace_id
- `work_proposal/1.0` (Proposal): proposal_id, task_id, kind, title, summary, artifacts, evidence, requirements, produced_by, status
- `work_task/1.0` (WorkTask): task_id, kind, goal, requirements, inputs, deliverables, acceptance, tools, approval_required, data_classification, budget, sandbox, models, robot_id

## Decisions

- [0001. Isolated Python 3.12 environment managed by uv](docs/decisions/0001-isolated-python-env.md): Accepted
- [0002. Standard-library core and strict, versioned contracts](docs/decisions/0002-stdlib-core-strict-contracts.md): Accepted
- [0003. Separate reported, verified and unresolved outcomes](docs/decisions/0003-verified-vs-unresolved-outcomes.md): Accepted
- [0004. AGPL-3.0 license](docs/decisions/0004-agpl-license.md): Accepted (open for review)
- [0005. Provenance spine: PROV model, hash-chained ledger, evidence-backed claims](docs/decisions/0005-provenance-spine.md): Accepted
- [0006. Team coordination: transactional coordinator, revisions, occupancy-aware release](docs/decisions/0006-team-coordination.md): Accepted
- [0007. Wave 1: fail-closed leases, verifiable registry, ledger v2, W3C ids, schemas](docs/decisions/0007-wave1-integrity-and-formats.md): Accepted
- [0008. Agentic engineering work in sandboxes, with provider-agnostic models](docs/decisions/0008-agentic-engineering-work.md): Accepted

## Recent recorded activities

- 2026-10-22 development: ar.source_catalog/1.0 contract and air sources CLI (succeeded) [REQ16, REQ18]
- 2026-10-22 research: open-source source catalog: 343 projects in 8 areas (succeeded) [REQ16, REQ18, AIR01]
- 2026-10-22 evaluation: automated test run (succeeded) [AIR01, AIR02, AIR03, F01, F02, F05, F06, F07, G01, G05, G06, G07, G08, G10, G12, G13, G14, G15, REQ01, REQ02, REQ03, REQ04, REQ05, REQ06, REQ07, REQ08, REQ09, REQ10, REQ12, REQ13, REQ14, REQ15, REQ16, REQ17, REQ18]
- 2026-10-22 development: Model layer (OpenAI-compatible, Claude SDK, robot-hosted, router), sandboxes, work agents, proposals, air work/models CLI (succeeded) [AIR01, AIR02, AIR03, REQ02, REQ04, REQ05, REQ17]
- 2026-10-22 decision: Agentic engineering work in sandboxes with provider-agnostic models (ADR 0008) (succeeded) [AIR01, AIR02, AIR03]
- 2026-10-22 evaluation: automated test run (succeeded) [F01, F02, F05, F06, G01, G05, G06, G07, G08, G10, G12, G13, G14, G15, REQ01, REQ02, REQ03, REQ05, REQ06, REQ07, REQ08, REQ09, REQ10, REQ12, REQ13, REQ14, REQ15, REQ16, REQ17, REQ18]
- 2026-10-22 development: Wave 1: fail-closed leases, verifiable registry, ledger v2 + lock, W3C trace ids, requirement fingerprints, JSON Schema export, CI hardening (succeeded) [REQ06, REQ10, REQ15, REQ18, G06, G15, F01, F02, F06]
- 2026-10-22 decision: Wave 1 integrity and format decisions (ADR 0007) (succeeded) [REQ10, REQ15, REQ18, G06, G15, F01]
- 2026-10-22 research: Open-source landscape survey: what AIR should adopt (6 areas incl. Microduck) (succeeded) [REQ01, REQ02, REQ06, REQ10, REQ14, REQ17, REQ18]
- 2026-10-22 evaluation: automated test run (succeeded) [F01, F02, F05, F06, G01, G05, G06, G07, G08, G10, G12, G13, G14, G15, REQ01, REQ02, REQ03, REQ05, REQ06, REQ07, REQ08, REQ09, REQ10, REQ12, REQ13, REQ14, REQ15, REQ16, REQ17, REQ18]
- 2026-10-22 development: Stage 3: two-robot coordination, fake arm, shared world, handoff scenario (succeeded) [REQ06, REQ07, REQ09, REQ10, REQ14]
- 2026-10-22 decision: Team coordination design (ADR 0006) (succeeded) [REQ06, REQ07, REQ09, REQ10]
- 2026-10-22 evaluation: automated test run (succeeded) [F01, F02, F05, F06, G01, G05, G06, G07, G08, G10, G12, G13, G15, REQ01, REQ02, REQ03, REQ05, REQ06, REQ07, REQ08, REQ09, REQ10, REQ12, REQ13, REQ14, REQ15, REQ16, REQ17, REQ18]
- 2026-10-22 development: Evidence spine: tracing, ledger, provenance, traceability, debug CLI, context feeder (succeeded) [REQ02, REQ10, REQ14, REQ15, REQ16, REQ18]
- 2026-10-22 decision: Provenance spine (ADR 0005) (succeeded) [REQ02, REQ10, REQ15, REQ16]

## Ledger

- entries: 280, integrity: OK
- head: `90a2df0c3b500391`

## Recent commits

- `8a0629e` Add source catalog: 343 studied open-source projects and backlog mapping (2026-10-22)
- `ec964d2` Add agentic engineering work: sandboxes, provider-agnostic models, proposals (2026-10-22)
- `a5bbbfb` Wave 1: fail-closed leases, verifiable registry, ledger v2, W3C ids, schemas (2026-10-22)
- `1479e5b` Add open-source landscape survey and adoption plan (2026-10-22)
- `8a6be8f` Add stage 3: two-robot coordination and handoff scenario (2026-10-22)
- `0678a14` Add evidence spine: provenance, traceability, debugging, context feeder (2026-10-22)
- `f9ef7f5` Merge GitHub initial commit (README, AGPL-3.0 license) (2026-10-22)
- `837c91b` Initial commit (2026-10-22)
- `d3d515c` Add agentic robotics framework: contracts and one-robot runtime (2026-10-22)
