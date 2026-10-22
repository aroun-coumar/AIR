# `agentic_robotics.coordination`

Team coordination: protocol messages, transport, allocation, participants and the team coordinator.

## Files

- `protocol.py`, `transport.py`, `allocation.py`, `participant.py`, `coordinator.py`, `team.py`

## Rules

- Cancellation is never success; released work is not rewritten.

## Tests

`tests/test_coordination.py`

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
