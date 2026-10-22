# `agentic_robotics.runtime`

Admission, authority, fenced resources, execution, plans and the trace. The only path from a command to an adapter.

## Files

- `runtime.py`: submit -> admit -> execute -> verify; UNRESOLVED and `reconcile()`
- `admission.py`: the ordered admission checks
- `authority.py`: delegations and revocation
- `resources.py`: fenced leases
- `plans.py`: plan execution
- `trace.py`: runtime events

## Rules

- VERIFIED only from observations; an unknown physical outcome is UNRESOLVED.
- Never retry blindly; reconcile from observations.
- New paths record trace events.

## Tests

tests/test_runtime.py, tests/test_authority_resources.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
