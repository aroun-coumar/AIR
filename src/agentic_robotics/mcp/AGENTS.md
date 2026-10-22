# `agentic_robotics.mcp`

An MCP server exposing AIR skills over stdio JSON-RPC (`air mcp`). Tools are proposals that go through `Runtime.submit`.

## Files

- `server.py`: dual-era server (2025-06-18/2025-11-25 `initialize`, 2026-07-28 stateless `_meta`)
- `cli.py`: `air mcp tools|serve`

## Rules

- A human approver can always deny; no decision means deny.
- Annotations are hints; nothing depends on them.
- No shell, no generic publish, no robot-connection tools.

## Tests

`tests/test_mcp.py`

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
