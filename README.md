# Agentic Robotics Framework

A reusable library, framework, tool and platform for building agentic robotic products,
built from the *Requirements and architecture baseline v0.1* (21 October 2026).

Product teams import the parts they need, bring their own robots, models and
infrastructure, and compose them through stable, typed public contracts. Agents
propose; the runtime admits, executes, and verifies against observations.

```python
from agentic_robotics import Robot, Runtime, SkillRegistry, TaskAgent, PlanExecutor, ScriptedModel

skills  = SkillRegistry.load("examples/skills/manifest.json")
runtime = Runtime(skills, root_principals=["operator:alice"])
robot   = runtime.register(Robot.from_manifest("examples/robots/carrier.manifest.json"))

async with runtime:
    grant  = runtime.authority.grant(delegation)               # bounded, revocable authority
    plan   = await TaskAgent(model, skills, runtime.clock).plan("inspect the part", [robot])
    result = await PlanExecutor(runtime).submit(
        plan, principal="agent:task", delegation_id=grant.delegation_id
    ).wait_for_verified_result()
```

## Four layers, one set of contracts

| Layer | What it is | Where |
|---|---|---|
| **Library** | Typed, versioned contracts and a local runtime. The core has no third-party dependencies: no GPU framework, database, simulator, ROS or hosted account. | `agentic_robotics.core`, `.contracts`, `.runtime` |
| **Framework** | Extension points downstream teams implement: robot adapters (entry-point discovery), model providers, admission checks, declarative skills. | `.plugins`, `.agents`, `runtime.admission` |
| **Tool** | `agentic-robotics` CLI: validate documents, list schemas and adapters, run adapter conformance, use the registry, run the quickstart. | `.cli`, `.conformance` |
| **Platform** | Content-addressed, immutable asset registry for manifests, skills, plans and policies. Works on a local directory today; a hosted registry or workbench implements the same `AssetRegistry` interface. | `.registry` |

## Getting started

The project uses an isolated Python 3.12 environment managed by [uv](https://docs.astral.sh/uv/).
Nothing is installed system-wide.

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv -e ".[dev]"
```

```bash
.venv/Scripts/agentic-robotics quickstart
```

There is also a container definition (`Dockerfile`, `.devcontainer/`) that runs the same
toolchain: `docker build -t agentic-robotics . && docker run --rm agentic-robotics`.

Checks:

```bash
.venv/Scripts/python -m pytest -q
```

```bash
.venv/Scripts/ruff check src tests examples
```

```bash
.venv/Scripts/mypy
```

## What exists now: baseline stages 1 and 2

**Stage 1: contracts and developer foundation**
- Versioned contracts (`ar.<name>/<major.minor>`) with strict parsing: unknown fields, bad types and major-version mismatches are rejected.
- Identities, units with dimension checks, coordinate frames, clock domains with synchronization state, and a stable error-code hierarchy.
- `ComponentManifest`, `CapabilityReport`, `Observation`, `SkillSpec` / `TaskSpec` / `TaskPlan`, `ExecutionContract`, `Command` / `ExecutionResult`, `Delegation`, `HumanIntervention`, `ResourceReservation`.
- Plugin protocol (`RobotAdapter`), entry-point discovery, a conformance suite, a fake-device quickstart and a custom-adapter guide (`examples/custom_adapter.py`).

**Stage 2: one-robot execution path**
- Admission pipeline: support, arguments, authority (scope, expiry, principal, stale revision), clock quality, capability availability and revision, input freshness, preconditions, and resource fencing. Authority and fencing are re-checked at dispatch.
- Acceptance, start, reported completion, and verified postconditions are separate events. A missed deadline after start, or lost contact, gives an **UNRESOLVED** outcome, which is settled by `reconcile()` (observation), never by a blind retry.
- Interruption follows the skill's declared semantics. A cancel is never shown as a confirmed stop unless the adapter confirms it. Revoking authority interrupts running work.
- Command-id deduplication, a busy policy, an explicit state machine, and a structured trace (JSON lines).
- Task agent behind a `ModelProvider` interface. It gets scoped context, only the tools for skills the robot supports, and model-call and context budgets. Plans are reused until a capability revision changes, and invalid plans are rejected before admission.
- Plan executor that reserves exclusive resources with fencing tokens and keeps a reservation held when an outcome is unresolved.

### Gate traceability (baseline section 21)

| Gate | Covered by |
|---|---|
| G01 Developer reuse | `test_minimal_import_has_no_heavy_dependencies`, quickstart |
| G05 Timing and freshness (partial) | stale-input, clock-quality and deadline tests (no model or cloud round trip) |
| G06 Ownership under failure (single robot) | fencing tests, `test_unresolved_step_keeps_its_reservation` |
| G07 Typed commitments (single robot) | state-machine, duplicate and unresolved tests |
| G12 Human control (partial) | revocation-before and revocation-during execution tests |
| G15 Authorization and integrity (partial) | scope and principal rejection, registry tamper detection |

## Next stages

3. Two-robot coordination: allocation, task protocol, reservations across participants, selective acknowledgements, reconciliation.
4. Engineering and optimization loop: experiment interface, simulator and emulator adapters, release manifests.
5. Operational lifecycle: hardware profiling, incidents, guided learning, maintenance, takeover and handback.
6. Reusable ecosystem: a second materially different integration, hosted registry and workbench.

Package names and APIs follow the baseline's proposals and stay experimental (`0.x`) until
they pass their acceptance gates.
