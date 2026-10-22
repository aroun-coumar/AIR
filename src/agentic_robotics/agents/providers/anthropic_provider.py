"""Claude via the official Anthropic SDK (optional extra: ``agentic-robotics[anthropic]``).

The SDK is imported lazily, so the core stays dependency-free. Credentials resolve the
SDK's usual way (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an ``ant auth login``
profile); AIR never stores them.

Defaults follow current API guidance for ``claude-opus-5-5``: thinking is always on
(the parameter is omitted), effort is set explicitly (``high`` for agentic work), tool
choice stays ``auto`` (forced tool choice is rejected on this model), and server-side
refusal fallbacks are enabled (``fallbacks: "default"``) so a declined request is re-run
on Anthropic's recommended fallback model. ``stop_reason == "refusal"`` is always
checked before content is used. The assistant ``response.content`` is appended back
unchanged so thinking blocks stay valid across turns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from .base import (
    ModelProfile,
    ModelRequestError,
    ModelUnavailable,
    StopReason,
    ToolCallRequest,
    ToolDefinition,
    ToolOutcome,
    Turn,
)

__requirements__ = ("AIR02", "REQ05",)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models on which the "default" server-side fallback mode applies on the Claude API.
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-fable-5-1", "claude-opus-5",
                             "claude-sonnet-5-5"})

_STOP = {"end_turn": StopReason.END, "tool_use": StopReason.TOOL_USE,
         "max_tokens": StopReason.MAX_TOKENS, "refusal": StopReason.REFUSAL,
         "stop_sequence": StopReason.END, "pause_turn": StopReason.OTHER}


def claude_profile(name: str = "claude", model: str = DEFAULT_MODEL, **kw: Any) -> ModelProfile:
    from .base import Location
    return ModelProfile(name=name, provider="anthropic", model=model, location=Location.CLOUD,
                        data_leaves_device=True, context_tokens=1_000_000, **kw)


class AnthropicProvider:
    def __init__(self, profile: ModelProfile, *, client: Any = None,
                 effort: str = "high", fallbacks: bool = True, max_retries: int = 2,
                 timeout_s: float = 600.0) -> None:
        self.profile = profile
        self.effort = effort
        self.fallbacks = fallbacks and profile.model in FALLBACK_MODELS
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ModelRequestError(
                    "the anthropic SDK is not installed; install agentic-robotics[anthropic]",
                    profile=profile.name) from exc
            kwargs: dict[str, Any] = {"max_retries": max_retries, "timeout": timeout_s}
            if profile.endpoint:
                kwargs["base_url"] = profile.endpoint
            if profile.api_key_env:
                import os
                key = os.environ.get(profile.api_key_env)
                if not key:
                    raise ModelRequestError("API key environment variable is not set",
                                            env=profile.api_key_env, profile=profile.name)
                kwargs["api_key"] = key
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def start(self, system: str, tools: Sequence[ToolDefinition]) -> AnthropicConversation:
        return AnthropicConversation(self, system, tools)

    def create(self, **params: Any) -> Any:
        import anthropic

        try:
            if self.fallbacks:
                return self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **params)
            return self.client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise ModelUnavailable("rate limited", profile=self.profile.name,
                                   error=str(exc)) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ModelUnavailable("Claude API unreachable", profile=self.profile.name,
                                   error=str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ModelUnavailable("Claude API error", status=exc.status_code,
                                       profile=self.profile.name) from exc
            raise ModelRequestError("Claude API rejected the request", status=exc.status_code,
                                    profile=self.profile.name, error=str(exc)) from exc


class AnthropicConversation:
    def __init__(self, provider: AnthropicProvider, system: str,
                 tools: Sequence[ToolDefinition]) -> None:
        self.provider = provider
        self.profile = provider.profile
        self.system = system
        self.tools = [{"name": t.name, "description": t.description,
                       "input_schema": t.input_schema} for t in tools]
        self.messages: list[dict[str, Any]] = []

    async def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return await self._complete()

    async def send_tool_results(self, outcomes: Sequence[ToolOutcome]) -> Turn:
        # All results for one assistant turn go back in a single user message.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": o.call_id, "content": o.content,
             "is_error": o.is_error} for o in outcomes]})
        return await self._complete()

    async def _complete(self) -> Turn:
        params: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": self.profile.max_output_tokens,
            "system": self.system,
            "messages": self.messages,
            "output_config": {"effort": self.provider.effort},
        }
        if self.tools:
            params["tools"] = self.tools
        response = await asyncio.to_thread(self.provider.create, **params)
        stop_raw = str(getattr(response, "stop_reason", "") or "")
        # Append the assistant content unchanged (keeps thinking blocks valid).
        self.messages.append({"role": "assistant", "content": response.content})
        if stop_raw == "refusal":
            details = getattr(response, "stop_details", None)
            return Turn(text="", tool_calls=[], stop=StopReason.REFUSAL,
                        model=str(getattr(response, "model", self.profile.model)),
                        usage=_usage(response), raw_stop=stop_raw,
                        refusal_category=getattr(details, "category", None))
        texts, calls = [], []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                calls.append(ToolCallRequest(block.id, block.name, dict(block.input or {})))
        stop = StopReason.TOOL_USE if calls else _STOP.get(stop_raw, StopReason.OTHER)
        return Turn(text="\n".join(texts), tool_calls=calls, stop=stop,
                    model=str(getattr(response, "model", self.profile.model)),
                    usage=_usage(response), raw_stop=stop_raw)


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    out = {}
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out
