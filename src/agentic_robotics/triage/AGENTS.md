# `agentic_robotics.triage`

One queue for everything AIR finds wrong (`air triage`).

## Files

- `book.py`, `collectors.py`, `contracts.py`, `policy.py`, `cli.py`

## Rules

- Collectors read; they never fix things on their own.

## Tests

`tests/test_triage.py`

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
