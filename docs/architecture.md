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
