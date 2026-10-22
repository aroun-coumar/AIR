"""Provider-neutral model layer: where a model runs, what it can do, and a multi-turn
tool-calling conversation that any provider implements.

AIR never assumes one vendor. A model is described by a :class:`ModelProfile`:
*where it runs* (cloud API, an edge box, the developer's host, or on the robot itself)
and *whether data leaves the device*. That lets policy decide, per task, which models
may see which data. Robots that host their own intelligence are just profiles with
``location=ON_ROBOT``.

Each provider keeps its native transcript append-only (the shape its API returns), so
provider-specific state such as Claude thinking blocks is passed back unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ...core.errors import FrameworkError

__requirements__ = ("AIR02", "REQ05", "REQ08", "REQ12")


class Location(StrEnum):
    CLOUD = "cloud"            # a hosted API (Anthropic, OpenAI, ...)
    EDGE = "edge"              # a shared on-premises inference server
    LOCAL_HOST = "local_host"  # the machine running AIR (Ollama, llama.cpp, LM Studio...)
    ON_ROBOT = "on_robot"      # inference running on the robot itself


@dataclass(frozen=True)
class ModelProfile:
    """Identity and placement of one model endpoint."""

    name: str                       # logical name used by policy ("planner", "duck-a-llm")
    provider: str                   # "anthropic" | "openai_compatible" | "scripted" | ...
    model: str                      # provider model id
    location: Location
    data_leaves_device: bool
    endpoint: str | None = None     # base URL for HTTP providers
    robot_id: str | None = None     # for ON_ROBOT models
    supports_tools: bool = True     # native tool calling; else a JSON text protocol is used
    context_tokens: int | None = None
    max_output_tokens: int = 16000
    api_key_env: str | None = None  # environment variable holding the key; never stored
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """Safe to record in traces and the ledger (no secrets)."""
        return {"name": self.name, "provider": self.provider, "model": self.model,
                "location": self.location.value, "data_leaves_device": self.data_leaves_device,
                "robot_id": self.robot_id, "endpoint_host": _host(self.endpoint)}


def _host(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    from urllib.parse import urlparse
    return urlparse(endpoint).netloc or None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


class StopReason(StrEnum):
    END = "end"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCallRequest]
    stop: StopReason
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    raw_stop: str = ""
    refusal_category: str | None = None


class ModelError(FrameworkError):
    code = "model_error"


class ModelUnavailable(ModelError):
    """Transient: connection failure, timeout, rate limit, overload, 5xx. Retryable,
    and a reason for the router to try another permitted model."""

    code = "model_unavailable"


class ModelRequestError(ModelError):
    """Permanent for this request: bad request, auth, unknown model. Not retryable."""

    code = "model_request_error"


class Conversation(Protocol):
    profile: ModelProfile

    async def send_user(self, text: str) -> Turn: ...

    async def send_tool_results(self, outcomes: Sequence[ToolOutcome]) -> Turn: ...


class ChatProvider(Protocol):
    profile: ModelProfile

    def start(self, system: str, tools: Sequence[ToolDefinition]) -> Conversation: ...


# -- JSON text protocol for models without native tool calling ------------------------------
TEXT_PROTOCOL = (
    "\n\nYou can call tools. To call tools, reply with ONLY a JSON object: "
    '{"tool_calls": [{"name": <tool name>, "arguments": {...}}]}. '
    'When you are finished, reply with ONLY {"final": <your answer>}. Available tools:\n'
)


def text_protocol_system(system: str, tools: Sequence[ToolDefinition]) -> str:
    spec = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools]
    return system + TEXT_PROTOCOL + json.dumps(spec, indent=1)


def parse_text_protocol(text: str, turn_index: int) -> tuple[str, list[ToolCallRequest]]:
    """Parse a text-protocol reply into (final_text, tool_calls). Unparseable text is
    treated as a final answer, never as a tool call."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return text, []
    if not isinstance(data, Mapping):
        return text, []
    calls = []
    for i, c in enumerate(data.get("tool_calls") or []):
        if isinstance(c, Mapping) and isinstance(c.get("name"), str):
            args = c.get("arguments") or {}
            calls.append(ToolCallRequest(f"txt_{turn_index}_{i}", c["name"],
                                         dict(args) if isinstance(args, Mapping) else {}))
    final = data.get("final")
    return (str(final) if final is not None else ""), calls


def tool_results_as_text(outcomes: Sequence[ToolOutcome]) -> str:
    return json.dumps({"tool_results": [
        {"call_id": o.call_id, "is_error": o.is_error, "content": o.content}
        for o in outcomes]})
