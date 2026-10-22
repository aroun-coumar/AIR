# `agentic_robotics.contracts`

The public data contracts of baseline section 6: manifests, capability reports, observations, skills, commands and results, authority, resources.

## Files

- `skill.py`: `SkillSpec`, `Condition` (postconditions the runtime evaluates)
- `execution.py`: `Command`, `CommandState`, `ExecutionResult`
- `manifest.py`: `ComponentManifest` with catalog `type_id`s
- `capability.py`, `observation.py`, `authority.py`, `resources.py`

## Rules

- Contracts are data. No behaviour that needs a runtime.
- Every change is a schema change: bump, export, keep compatibility.

## Tests

`tests/test_contracts.py`

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
