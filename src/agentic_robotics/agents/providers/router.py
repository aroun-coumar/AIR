"""Model routing by policy: which models may see this work, in which order to try them.

    router = ModelRouter(trace=runtime.trace)
    router.register(OpenAICompatibleProvider(duck_llm))        # on the robot
    router.register(OpenAICompatibleProvider(workstation_llm)) # on this host
    router.register(AnthropicProvider(claude_profile()))       # cloud API

    conv = router.start(ModelPolicy(allow_data_egress=False), system, tools)

Ordering keeps data close: models on the policy's robot, then this host, then edge,
then cloud, unless ``prefer`` names an explicit order. A model that is unavailable on
the first turn (robot offline, server down, rate limited) falls through to the next
permitted one. After the first successful turn the conversation stays on that model,
because transcripts are provider-native. Every call is traced (model, location, data
egress, tokens, latency), never the prompt or response content.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ...robot import Robot
from ...runtime.trace import Trace
from .base import (
    ChatProvider,
    Conversation,
    Location,
    ModelProfile,
    ModelRequestError,
    ModelUnavailable,
    ToolDefinition,
    ToolOutcome,
    Turn,
)

__requirements__ = ("AIR02", "REQ05", "REQ08", "REQ12", "F07")

_PROXIMITY = {Location.ON_ROBOT: 0, Location.LOCAL_HOST: 1, Location.EDGE: 2,
              Location.CLOUD: 3}


@dataclass(frozen=True)
class ModelPolicy:
    allowed_locations: frozenset[Location] = field(
        default_factory=lambda: frozenset(Location))
    allow_data_egress: bool = True     # False: only models where data stays on the device
    robot_id: str | None = None        # prefer (or, with on_robot only, require) this robot
    require_tools: bool = False        # needs native tool calling
    prefer: tuple[str, ...] = ()       # explicit order of profile names

    @classmethod
    def on_robot_only(cls, robot_id: str) -> ModelPolicy:
        return cls(allowed_locations=frozenset({Location.ON_ROBOT}), allow_data_egress=False,
                   robot_id=robot_id)

    @classmethod
    def no_cloud(cls) -> ModelPolicy:
        return cls(allowed_locations=frozenset({Location.ON_ROBOT, Location.LOCAL_HOST,
                                                Location.EDGE}), allow_data_egress=False)


class ModelRouter:
    def __init__(self, trace: Trace | None = None) -> None:
        self.trace = trace
        self._providers: list[ChatProvider] = []

    def register(self, provider: ChatProvider) -> ChatProvider:
        if any(p.profile.name == provider.profile.name for p in self._providers):
            raise ModelRequestError("model profile name already registered",
                                    name=provider.profile.name)
        self._providers.append(provider)
        return provider

    def register_robot(self, robot: Robot,
                       factory: Callable[[ModelProfile], ChatProvider] | None = None
                       ) -> list[ChatProvider]:
        """Register every model a robot hosts itself (``RobotAdapter.hosted_models``)."""
        from .openai_compat import OpenAICompatibleProvider

        added = []
        for spec in robot.adapter.hosted_models():
            profile = ModelProfile(
                name=str(spec["name"]), provider=str(spec.get("provider", "openai_compatible")),
                model=str(spec["model"]), location=Location.ON_ROBOT, robot_id=robot.robot_id,
                data_leaves_device=False, endpoint=spec.get("endpoint"),
                supports_tools=bool(spec.get("supports_tools", True)))
            make = factory or OpenAICompatibleProvider
            added.append(self.register(make(profile)))
        return added

    def profiles(self) -> list[ModelProfile]:
        return [p.profile for p in self._providers]

    def candidates(self, policy: ModelPolicy) -> list[ChatProvider]:
        def permitted(p: ModelProfile) -> bool:
            if p.location not in policy.allowed_locations:
                return False
            if not policy.allow_data_egress and p.data_leaves_device:
                return False
            if policy.require_tools and not p.supports_tools:
                return False
            # On-robot-only policies for a named robot never use another robot's model.
            return not (policy.allowed_locations == frozenset({Location.ON_ROBOT})
                        and policy.robot_id and p.robot_id != policy.robot_id)

        def order(p: ModelProfile) -> tuple[int, int, int]:
            preferred = policy.prefer.index(p.name) if p.name in policy.prefer else len(
                policy.prefer)
            own_robot = 0 if (policy.robot_id and p.robot_id == policy.robot_id) else 1
            return preferred, own_robot, _PROXIMITY[p.location]

        allowed = [p for p in self._providers if permitted(p.profile)]
        return sorted(allowed, key=lambda p: order(p.profile))

    def start(self, policy: ModelPolicy, system: str,
              tools: Sequence[ToolDefinition]) -> RoutedConversation:
        candidates = self.candidates(policy)
        if not candidates:
            raise ModelRequestError("no registered model satisfies the policy",
                                    policy=repr(policy),
                                    registered=[p.describe() for p in self.profiles()])
        return RoutedConversation(self, candidates, system, tools)


class RoutedConversation:
    """A conversation that falls through permitted models on the first turn only."""

    def __init__(self, router: ModelRouter, candidates: list[ChatProvider], system: str,
                 tools: Sequence[ToolDefinition]) -> None:
        self.router = router
        self.candidates = candidates
        self.system = system
        self.tools = list(tools)
        self._index = 0
        self._inner: Conversation = candidates[0].start(system, self.tools)
        self._pinned = False
        self.calls = 0

    @property
    def profile(self) -> ModelProfile:
        return self._inner.profile

    async def send_user(self, text: str) -> Turn:
        while True:
            try:
                return await self._traced(self._inner.send_user(text))
            except ModelUnavailable as exc:
                if self._pinned or self._index + 1 >= len(self.candidates):
                    raise
                failed = self._inner.profile
                self._index += 1
                self._inner = self.candidates[self._index].start(self.system, self.tools)
                if self.router.trace is not None:
                    self.router.trace.record(
                        "model_fallback", failed.name, error=exc.to_dict(),
                        next=self._inner.profile.describe())

    async def send_tool_results(self, outcomes: Sequence[ToolOutcome]) -> Turn:
        return await self._traced(self._inner.send_tool_results(outcomes))

    async def _traced(self, call: object) -> Turn:
        started = time.monotonic()
        turn: Turn = await call  # type: ignore[misc]
        self._pinned = True
        self.calls += 1
        if self.router.trace is not None:
            self.router.trace.record(
                "model_call", self._inner.profile.name, **self._inner.profile.describe(),
                stop=turn.stop.value, raw_stop=turn.raw_stop, served_by=turn.model,
                usage=turn.usage, tool_calls=[c.name for c in turn.tool_calls],
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                refusal_category=turn.refusal_category)
        return turn
