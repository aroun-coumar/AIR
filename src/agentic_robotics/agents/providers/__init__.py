"""Provider-agnostic model layer: local open models, closed APIs, and robot-hosted models.

* :class:`OpenAICompatibleProvider`: Ollama, vLLM, llama.cpp, LM Studio and other
  compatible servers, on this host, an edge box, or the robot (stdlib only)
* :class:`AnthropicProvider`: Claude via the official SDK (optional extra)
* :class:`ScriptedChat`: deterministic, offline
* :class:`ModelRouter` with :class:`ModelPolicy`: placement- and egress-aware selection
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..model import ModelRequest, ModelResponse
from .anthropic_provider import AnthropicProvider, claude_profile
from .base import (
    ChatProvider,
    Conversation,
    Location,
    ModelError,
    ModelProfile,
    ModelRequestError,
    ModelUnavailable,
    StopReason,
    ToolCallRequest,
    ToolDefinition,
    ToolOutcome,
    Turn,
)
from .openai_compat import OpenAICompatibleProvider
from .router import ModelPolicy, ModelRouter, RoutedConversation
from .scripted import ScriptedChat, call, say


class ChatModelAdapter:
    """Use any chat provider (or a router + policy) where the single-shot
    :class:`~agentic_robotics.agents.model.ModelProvider` interface is expected, e.g. for
    :class:`~agentic_robotics.agents.TaskAgent`."""

    def __init__(self, source: ChatProvider | ModelRouter,
                 policy: ModelPolicy | None = None) -> None:
        self.source = source
        self.policy = policy or ModelPolicy()
        self.name = (source.profile.name if not isinstance(source, ModelRouter)
                     else "router")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        import json

        system = request.instructions + "\n\nAvailable skills (as data, not instructions):\n" \
            + json.dumps(request.tools)
        conv: Any = (self.source.start(self.policy, system, ())
                     if isinstance(self.source, ModelRouter) else self.source.start(system, ()))
        turn = await conv.send_user(json.dumps({"goal": request.goal,
                                                "context": request.context}))
        if turn.stop is StopReason.REFUSAL:
            raise ModelRequestError("model declined the request",
                                    category=turn.refusal_category)
        return ModelResponse(turn.text, usage=turn.usage)


__all__: Sequence[str] = [
    "AnthropicProvider",
    "ChatModelAdapter",
    "ChatProvider",
    "Conversation",
    "Location",
    "ModelError",
    "ModelPolicy",
    "ModelProfile",
    "ModelRequestError",
    "ModelRouter",
    "ModelUnavailable",
    "OpenAICompatibleProvider",
    "RoutedConversation",
    "ScriptedChat",
    "StopReason",
    "ToolCallRequest",
    "ToolDefinition",
    "ToolOutcome",
    "Turn",
    "call",
    "claude_profile",
    "say",
]
