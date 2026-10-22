# `agentic_robotics.provenance`

Tracing, the project ledger, activities and entities, traceability, explain, context pack and the `air` provenance CLI.

## Files

- `ledger.py`: hash-chained ledger (v2 canonical JSON)
- `traceability.py`: requirement matrix and evidence fingerprints
- `tracing.py`: spans
- `explain.py`, `context_pack.py`, `sources.py`
- `cli.py`: `air init|activity|ledger|lineage|impact|requirements|context`

## Rules

- Do not hand-edit `.air/ledger.jsonl`; ledger chains cannot be merged.
- Claim `implemented`/`verified` only when the matrix check backs it.

## Tests

tests/test_provenance.py, tests/test_ledger_v2.py, tests/test_tracing_w3c.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
