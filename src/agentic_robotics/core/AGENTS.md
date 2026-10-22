# `agentic_robotics.core`

Identifiers, the strict versioned contract base, units and frames, clocks, errors, deprecation helpers and JSON Schema export.

## Files

- `schema.py`: `@contract(name, version)`, strict load/dump; unknown fields fail
- `jsonschema.py`: `air schema export|check` (drift and minor-is-superset)
- `clock.py`: `MonotonicClock`, `ManualClock`; the runtime never reads wall time
- `ids.py`: W3C-compatible trace and span ids
- `units.py`: unit dimensions

## Rules

- Standard library only, no imports from other AIR packages.
- A new contract field needs a default and a minor bump; removal is major.
- Never edit a published file under `schemas/`; export a new version.

## Tests

tests/test_contracts.py, tests/test_jsonschema.py, tests/test_deprecation.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
