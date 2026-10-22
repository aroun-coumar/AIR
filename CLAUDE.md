# Working on AIR (Agentic Robotics Framework)

Guidance for people and AI agents changing this repository. For a generated, current
snapshot of modules, contracts, requirement status, decisions and recent activity, read
`docs/CONTEXT.md` (regenerate it with `air context --out docs/CONTEXT.md`).

## What this is

An installable library/framework/tool/platform (`agentic-robotics`, import
`agentic_robotics`) built from the *Requirements and architecture baseline v0.1*.
It is not an application. Product teams embed it; nothing here should assume a
particular robot, simulator, model provider or hosted service.

## Environment

- Isolated Python 3.12 via uv in `.venv` (nothing system-wide). Windows paths below;
  use `.venv/bin/` on Linux/macOS.
- Install: `uv pip install --python .venv -e ".[dev]"`
- Tests: `.venv/Scripts/python -m pytest -q`
- Lint/types: `.venv/Scripts/ruff check src tests examples` and `.venv/Scripts/mypy` (strict)
- Traceability: `.venv/Scripts/python -m pytest -q --air-evidence .air/test-evidence.json`
  then `.venv/Scripts/air requirements matrix --check`
- Schemas: `.venv/Scripts/air schema check` (after a contract version bump:
  `air schema export` and commit the new file; never edit a published schema)

All five must pass before committing. Versioning and deprecation rules:
`docs/policy/versioning.md`.

## Rules that keep the architecture honest

1. **Core stays dependency-free.** `src/agentic_robotics` uses only the standard library.
   Optional integrations declare their own extras.
2. **Contracts are strict and versioned.** New contract fields need a default and a minor
   version bump (`@contract("name", version="1.1")`); removing or retyping a field is a
   major bump. Unknown fields must keep failing loudly.
3. **The runtime never trusts a report.** Completion is VERIFIED only from observations.
   Anything that leaves the physical outcome unknown is UNRESOLVED and settled by
   `reconcile()`, never by a blind retry. Keep this distinction in every new path.
4. **Agents propose; the runtime disposes.** Model output, tool descriptions and retrieved
   text never grant authority. Every command goes through admission.
5. **Everything significant leaves evidence.** New runtime paths record trace events;
   new user-facing operations that change state should be visible in the project ledger.
6. **Traceability is part of the change.** Tag new modules with
   `__requirements__ = ("REQxx", ...)` and tests with `@pytest.mark.req("REQxx", "Gxx")`.
   Only claim `implemented`/`verified` (`air requirements claim REQxx=verified --by ...`)
   when the matrix check backs it.
7. **Record decisions.** Non-obvious choices get an ADR in `docs/decisions/` and, where
   useful, `air activity record --kind decision ...`.

## Layout

- `core/`: ids, schema/contract base, units and frames, clocks, errors
- `contracts/`: public data contracts (baseline section 6)
- `runtime/`: admission, authority, fenced resources, execution, plans, trace
- `agents/`: model-provider interface, context selection, task agent
- `provenance/`: tracing, ledger, project activities/entities, traceability, explain,
  context pack, `air` provenance CLI
- `hardware/`: typed catalog of robot classes, device types and protocols (JSON in
  `hardware/data/`, extended per project in `.air/hardware/`) and manifest checks
- `engineering/`: experiment backends, time policy, seeds, reproducibility probe, golden runs,
  search and release selection, evaluation validity, `air experiment`
- `releases/`: policy slots (load gate, origin authority, two-step start, stale-chunk
  refusal), trial-then-confirm release activation, TUF-style signed metadata, signing
- `health/`: expected-item health aggregation (STALE first-class), monitors, PSI probe,
  capability gate, readiness gate, diagnosis table
- `incidents/`: incident pipeline (novelty, hypotheses, bounded tests through admission)
- `maintenance/`: prognostic estimates (clean-room; capability gated on p5)
- `integrations/`: reference adapters (fake devices) behind the public adapter protocol
- `testing/`: pytest evidence plugin
- `plugins.py`, `conformance.py`, `registry.py`, `cli.py`: extension, conformance, assets, tool

## Project state

The `.air/` directory is this repository's own provenance project: the ledger records
development activities, decisions, recorded test runs and requirement-claim changes.
Commit `ledger.jsonl`, `requirements.json` and `project.json`; `artifacts/` and
`test-evidence.json` are local.
