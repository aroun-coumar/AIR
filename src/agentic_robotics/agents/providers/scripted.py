"""Deterministic chat provider for tests, examples and offline development.

A script is a list of steps, each a :class:`Turn` to return or a callable receiving the
conversation's transcript so far (for responses that depend on tool results).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .base import (
    Location,
    ModelProfile,
    ModelRequestError,
    ModelUnavailable,
    StopReason,
    ToolCallRequest,
    ToolDefinition,
    ToolOutcome,
    Turn,
)

__requirements__ = ("AIR02", "REQ18",)

Step = Turn | Callable[[list[dict[str, Any]]], Turn]


def say(text: str) -> Turn:
    return Turn(text=text, tool_calls=[], stop=StopReason.END, model="scripted")


def call(tool_name: str, call_id: str | None = None, /, **arguments: Any) -> Turn:
    """A scripted tool call. Positional-only parameters, so any tool argument name
    (including ``name`` or ``call_id``) can be passed as a keyword."""
    return Turn(text="", tool_calls=[ToolCallRequest(call_id or f"c_{tool_name}", tool_name,
                                                     arguments)],
                stop=StopReason.TOOL_USE, model="scripted")


class ScriptedChat:
    def __init__(self, script: Sequence[Step], *, name: str = "scripted",
                 location: Location = Location.LOCAL_HOST, robot_id: str | None = None,
                 unavailable: bool = False) -> None:
        self.profile = ModelProfile(name=name, provider="scripted", model="scripted",
                                    location=location, robot_id=robot_id,
                                    data_leaves_device=location is Location.CLOUD)
        self.script = list(script)
        self.unavailable = unavailable
        self.transcripts: list[list[dict[str, Any]]] = []

    def start(self, system: str, tools: Sequence[ToolDefinition]) -> ScriptedConversation:
        conv = ScriptedConversation(self, system, tools)
        self.transcripts.append(conv.transcript)
        return conv


class ScriptedConversation:
    def __init__(self, chat: ScriptedChat, system: str,
                 tools: Sequence[ToolDefinition]) -> None:
        self.chat = chat
        self.profile = chat.profile
        self.transcript: list[dict[str, Any]] = [
            {"role": "system", "content": system, "tools": [t.name for t in tools]}]

    async def send_user(self, text: str) -> Turn:
        self.transcript.append({"role": "user", "content": text})
        return self._next()

    async def send_tool_results(self, outcomes: Sequence[ToolOutcome]) -> Turn:
        self.transcript.append({"role": "tool", "results": [
            {"call_id": o.call_id, "content": o.content, "is_error": o.is_error}
            for o in outcomes]})
        return self._next()

    def _next(self) -> Turn:
        if self.chat.unavailable:
            raise ModelUnavailable("scripted model marked unavailable", profile=self.profile.name)
        if not self.chat.script:
            raise ModelRequestError("scripted model has no steps left", profile=self.profile.name)
        step = self.chat.script.pop(0)
        turn = step(self.transcript) if callable(step) else step
        self.transcript.append({"role": "assistant", "text": turn.text,
                                "tool_calls": [c.name for c in turn.tool_calls]})
        return turn
