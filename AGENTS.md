# Agent guide: start here

This is the entry point for coding agents working in this repository (the layout follows
the AgibotTech/genie_sim pattern). Read in this order and stop when your question is
answered:

1. **This file**: which package owns the task.
2. **[`CLAUDE.md`](CLAUDE.md)**: the rules that keep the architecture honest, and the five
   checks that must pass before a commit.
3. **`src/agentic_robotics/<package>/AGENTS.md`**: files, rules and tests of that package.
4. **[`docs/CONTEXT.md`](docs/CONTEXT.md)**: generated snapshot of modules, contracts,
   requirement status, decisions and recent activity.
5. The decision records in [`docs/decisions/`](docs/decisions/) the package guide links to.
6. Only then the source files.

Do not start with a repository-wide search: the table below tells you where to look.

## Dispatch table

| If the task is about | Package | Guide |
|---|---|---|
| ids, contract base, units, clocks, errors, JSON Schema export | `core` | [AGENTS.md](src/agentic_robotics/core/AGENTS.md) |
| a public data contract (manifest, skill, command, result, authority) | `contracts` | [AGENTS.md](src/agentic_robotics/contracts/AGENTS.md) |
| admission, execution, verification, UNRESOLVED, reconcile, leases | `runtime` | [AGENTS.md](src/agentic_robotics/runtime/AGENTS.md) |
| model providers, context selection, the task agent | `agents` | [AGENTS.md](src/agentic_robotics/agents/AGENTS.md) |
| ledger, tracing, traceability, lineage, `air` provenance commands | `provenance` | [AGENTS.md](src/agentic_robotics/provenance/AGENTS.md) |
| multi-robot coordination | `coordination` | [AGENTS.md](src/agentic_robotics/coordination/AGENTS.md) |
| agent work in sandboxes, proposals | `work` | [AGENTS.md](src/agentic_robotics/work/AGENTS.md) |
| the triage queue | `triage` | [AGENTS.md](src/agentic_robotics/triage/AGENTS.md) |
| device types, robot classes, protocols, manifest checks | `hardware` | [AGENTS.md](src/agentic_robotics/hardware/AGENTS.md) |
| a robot, simulator, fleet protocol or policy server bridge | `integrations` | [AGENTS.md](src/agentic_robotics/integrations/AGENTS.md) |
| exposing AIR skills to MCP clients | `mcp` | [AGENTS.md](src/agentic_robotics/mcp/AGENTS.md) |
| adapter conformance, plugin discovery, the asset registry | `conformance.py`, `plugins.py`, `registry.py` | [CLAUDE.md](CLAUDE.md) |

## Canonical commands

| Command | What it does |
|---|---|
| `air doctor` | Environment and tool availability; never raises |
| `air schema check` | Schemas current and backwards compatible |
| `air requirements matrix --check` | Requirement evidence (after `pytest --air-evidence`) |
| `air conformance <manifest> --skills <skills>` | Adapter conformance |
| `air hardware check <manifest>` | Manifest against the hardware catalog |
| `air mcp serve --skills ... --manifest ...` | MCP over stdio; actuation denied unless pre-approved |
| `air context --out docs/CONTEXT.md` | Regenerate the briefing |

## Changelog

Every change that users can notice adds a news fragment under
[`changelog.d/`](changelog.d/) (`<id>.<type>.md`, types in [`towncrier.toml`](towncrier.toml)).
