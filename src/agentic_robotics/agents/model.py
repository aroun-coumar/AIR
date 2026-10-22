"""Model-provider interface (baseline sections 5 and 8).

Any language model, local or hosted, plugs in by implementing :class:`ModelProvider`.
The framework sends a structured request and expects text back; it never hands a model
credentials, and nothing a model returns is executed without runtime admission.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__requirements__ = ("REQ05",)


@dataclass(frozen=True)
class ModelRequest:
    role: str
    instructions: str
    goal: str
    context: dict[str, Any]
    tools: list[dict[str, Any]]
    response_format: str = "task_plan/json"


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ScriptedModel:
    """Deterministic provider for tests, examples and offline use.

    Give it a sequence of canned responses, or a function of the request.
    """

    name = "scripted"

    def __init__(self, responses: Sequence[str] | Callable[[ModelRequest], str]) -> None:
        self._fn = responses if callable(responses) else None
        self._queue = list(responses) if not callable(responses) else []
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._fn is not None:
            return ModelResponse(self._fn(request))
        if not self._queue:
            raise RuntimeError("ScriptedModel has no responses left")
        return ModelResponse(self._queue.pop(0))
