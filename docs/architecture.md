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

## Package dependency rule

`core` depends on nothing. `contracts` depend on `core`. `runtime`, `agents` and
`provenance` depend on `contracts`/`core`; `runtime` and `agents` use `provenance.tracing`.
`integrations` depend only on public protocols (`plugins`, `contracts`). Nothing in the
core imports an integration.
