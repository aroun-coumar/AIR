# `agentic_robotics.hardware`

The hardware catalog: robot classes, device types, protocols, domain packs, semantics and task context (`air hardware`).

## Files

- `catalog.py`, `validate.py`, `semantics.py`, `domains.py`, `context.py`, `data/*.json`

## Rules

- Describe, never prescribe: AIR never requires hardware.
- Only the CLI imports this package.

## Tests

tests/test_hardware.py, tests/test_hardware_context.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
