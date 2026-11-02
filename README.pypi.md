# AIR: the runtime between the model and the motors

**AIR** is a dependency-free Python platform for robots that agents can safely direct,
and for the engineering work around them that agents can safely do. Agents propose; the
runtime admits, executes and **verifies against observations**. Everything leaves evidence.

```bash
pip install --pre airobotics
```

```bash
air quickstart
```

`--pre` is needed while only alpha releases exist. The quickstart runs a full plan on a fake robot. It needs no hardware, simulator, model
key or network. The import name is `agentic_robotics`, and the CLI is `air` (also
available as `airobotics`).

## What it gives you

- **A runtime that never trusts a report.**
  - Admission checks authority, scope, clock quality, input freshness, health,
    preconditions and fencing.
  - `verified` comes only from authoritative observations. A physically unknown outcome
    is `unresolved` and is settled by `reconcile()`, never by a blind retry.
  - An intent journal is written before dispatch.
  - Human approval works as an interrupt bound to the exact arguments.
  - Cancel and kill are different things, and continuous skills have a deadman.
- **Multi-robot coordination.** Fenced, all-or-nothing leases; work released in steps
  under VDA 5050 order-update rules; liveness tracking; hybrid logical clocks.
- **Any model, placed deliberately.** Local open models (Ollama, vLLM, llama.cpp),
  Claude through an optional extra, or models hosted on the robot itself. Restricted data
  never reaches a cloud model.
- **Agents that do engineering work** (research, development, simulation, maintenance,
  diagnosis) in sandboxes that declare what they enforce. Their results are proposals
  that a different person approves.
- **A package library.**
  - Interface contracts with semver.
  - Packages that state what they provide and require, as version ranges, with
    hardware and host needs.
  - Compatibility checks against a real robot.
  - A service registry.
  - A skill library where promotion depends on evidence: simulation never authorizes
    hardware.
- **A hardware catalog and context.** 100 robot classes, 648 device types and 122
  protocols, with units, limits, maintenance and failure modes. Plus 19 engineering
  domain packs, and task context for agents. AIR describes hardware; it never requires it.
- **Operations.** Health with STALE as a real level, a readiness gate, incidents with
  bounded tests, prognostics, and one triage queue for everything that's wrong.
- **Evidence you can prove.** A hash-chained ledger with RFC 9162 Merkle checkpoints,
  in-toto attestations, PROV-JSON and OTLP export, and requirements traced through code
  to tests.

## Optional extras

The core uses only the Python standard library. Integrations are opt-in:

```bash
pip install "airobotics[anthropic]"
```

Other extras: `optuna`, `signing` (Ed25519), and one per bridge: `piper`, `vda5050`,
`openpi`, `lerobot`, `dora`.

## Status

Alpha (`0.x`). The APIs are experimental until they pass the acceptance gates of the
baseline. Bridges to real robots are tested against fake peers that speak the upstream
wire protocols. The [quality declaration](https://github.com/aroun-coumar/AIR/blob/main/QUALITY_DECLARATION.md)
states maturity honestly.

## Links

- Source, full README and architecture diagrams: https://github.com/aroun-coumar/AIR
- Architecture: https://github.com/aroun-coumar/AIR/blob/main/docs/architecture.md
- Decisions (ADRs): https://github.com/aroun-coumar/AIR/tree/main/docs/decisions
- Hardware catalog: https://github.com/aroun-coumar/AIR/blob/main/docs/hardware-catalog.md
- Security policy: https://github.com/aroun-coumar/AIR/blob/main/SECURITY.md

License: GNU Affero General Public License v3.0 or later.
