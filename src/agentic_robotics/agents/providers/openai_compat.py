"""OpenAI-compatible chat-completions client (stdlib only).

Covers the servers most open models are served with: Ollama, vLLM, llama.cpp
``llama-server``, LM Studio, LocalAI, TGI, and hosted APIs that speak the same
protocol. The same profile shape points at a model on the developer's machine, an edge
server, or a model the robot hosts itself::

    ModelProfile(name="duck-a-llm", provider="openai_compatible", model="qwen2.5:3b",
                 location=Location.ON_ROBOT, robot_id="duck-a", data_leaves_device=False,
                 endpoint="http://duck-a.local:11434/v1")

Models without native tool calling (``supports_tools=False``) use a JSON text protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
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
    parse_text_protocol,
    text_protocol_system,
    tool_results_as_text,
)

__requirements__ = ("AIR02", "REQ05", "REQ12")

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]

_FINISH = {"stop": StopReason.END, "tool_calls": StopReason.TOOL_USE,
           "function_call": StopReason.TOOL_USE, "length": StopReason.MAX_TOKENS,
           "content_filter": StopReason.REFUSAL}


def urllib_transport(url: str, headers: dict[str, str], body: bytes,
                     timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


class OpenAICompatibleProvider:
    def __init__(self, profile: ModelProfile, *, transport: Transport = urllib_transport,
                 timeout_s: float = 120.0, max_retries: int = 2) -> None:
        if not profile.endpoint:
            raise ModelRequestError("openai_compatible profile needs an endpoint",
                                    profile=profile.name)
        self.profile = profile
        self.transport = transport
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def start(self, system: str, tools: Sequence[ToolDefinition]) -> OpenAICompatConversation:
        return OpenAICompatConversation(self, system, tools)

    # -- HTTP --------------------------------------------------------------------------
    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.profile.endpoint.rstrip("/") + "/chat/completions"  # type: ignore[union-attr]
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key_env:
            key = os.environ.get(self.profile.api_key_env)
            if not key:
                raise ModelRequestError("API key environment variable is not set",
                                        env=self.profile.api_key_env, profile=self.profile.name)
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps(payload).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                status, resp_headers, data = self.transport(url, headers, body, self.timeout_s)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = ModelUnavailable("model endpoint unreachable", profile=self.profile.name,
                                        error=str(exc))
            else:
                if status == 200:
                    try:
                        parsed: dict[str, Any] = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ModelRequestError("endpoint returned invalid JSON",
                                                profile=self.profile.name) from exc
                    return parsed
                text = data[:500].decode("utf-8", "replace")
                if status == 429 or status >= 500:
                    last = ModelUnavailable("model endpoint busy or failing", status=status,
                                            profile=self.profile.name, body=text)
                    retry_after = _retry_after(resp_headers)
                    if retry_after is not None and attempt < self.max_retries:
                        time.sleep(min(retry_after, 30.0))
                        continue
                else:
                    raise ModelRequestError("model request rejected", status=status,
                                            profile=self.profile.name, body=text)
            if attempt < self.max_retries:
                time.sleep(min(0.5 * 2 ** attempt + random.uniform(0, 0.25), 10.0))
        assert last is not None
        raise last


def _retry_after(headers: dict[str, str]) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except ValueError:
                return None
    return None


class OpenAICompatConversation:
    def __init__(self, provider: OpenAICompatibleProvider, system: str,
                 tools: Sequence[ToolDefinition]) -> None:
        self.provider = provider
        self.profile = provider.profile
        self.native_tools = self.profile.supports_tools and bool(tools)
        self.tools = list(tools)
        sys_text = system if self.native_tools or not tools else text_protocol_system(system,
                                                                                     tools)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text}]
        self._turns = 0

    async def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return await self._complete()

    async def send_tool_results(self, outcomes: Sequence[ToolOutcome]) -> Turn:
        if self.native_tools:
            for o in outcomes:
                self.messages.append({"role": "tool", "tool_call_id": o.call_id,
                                      "content": ("ERROR: " if o.is_error else "") + o.content})
        else:
            self.messages.append({"role": "user", "content": tool_results_as_text(outcomes)})
        return await self._complete()

    async def _complete(self) -> Turn:
        payload: dict[str, Any] = {"model": self.profile.model, "messages": self.messages,
                                   "max_tokens": self.profile.max_output_tokens}
        if self.native_tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in self.tools]
            payload["tool_choice"] = "auto"
        payload.update(self.profile.options.get("extra_body", {}))
        data = await asyncio.to_thread(self.provider.post, payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelRequestError("unexpected chat-completions response",
                                    profile=self.profile.name) from exc
        self.messages.append(message)  # append-only native transcript
        self._turns += 1
        text = message.get("content") or ""
        calls: list[ToolCallRequest] = []
        if self.native_tools:
            for tc in message.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": fn.get("arguments")}
                calls.append(ToolCallRequest(str(tc.get("id")), str(fn.get("name")),
                                             args if isinstance(args, dict) else {}))
        elif self.tools:
            text, calls = parse_text_protocol(text, self._turns)
        finish = str(choice.get("finish_reason") or "")
        stop = StopReason.TOOL_USE if calls else _FINISH.get(finish, StopReason.END)
        usage = {k: int(v) for k, v in (data.get("usage") or {}).items()
                 if isinstance(v, int)}
        return Turn(text=text, tool_calls=calls, stop=stop,
                    model=str(data.get("model", self.profile.model)), usage=usage,
                    raw_stop=finish)
