# `agentic_robotics.work`

Agentic engineering work: sandboxes, work contracts, guarded tools, the work agent and proposals (`air work`).

## Files

- `sandbox.py`, `contracts.py`, `tools.py`, `agent.py`, `cli.py`

## Rules

- Deliverables are proposals decided by a different principal (AIR03).

## Tests

`tests/test_work.py`

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
