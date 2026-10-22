# `agentic_robotics.agents`

Model-provider interface, context selection and the task agent. Agents propose; the runtime disposes.

## Files

- `model.py`: `ModelProvider`
- `context.py`: bounded context building
- `task_agent.py`: plan proposals
- `providers/`: OpenAI-compatible, Anthropic (extra), router, scripted, catalog

## Rules

- Model output never grants authority; every command goes through admission.
- Provider SDKs are extras, imported lazily.

## Tests

tests/test_agent_and_plans.py, tests/test_models.py

## Before you change anything here

- Read the root [`AGENTS.md`](../../../AGENTS.md) and [`CLAUDE.md`](../../../CLAUDE.md).
- Tag new modules with `__requirements__ = ("REQxx", ...)` and tests with
  `@pytest.mark.req(...)`.
- All five checks pass before a commit: pytest, ruff, mypy (strict), `air schema check`,
  `air requirements matrix --check`.
