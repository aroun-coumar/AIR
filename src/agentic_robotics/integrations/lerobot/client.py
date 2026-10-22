"""A LeRobot policy client over an injected transport: admit before motion, bounded waits,
staleness-checked chunks. The wire format is the transport's business (not modelled)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from ...core.clock import Clock, MonotonicClock
from ...core.errors import AdmissionRejected, FrameworkError
from ...core.ids import new_id
from .contracts import (
    ActionChunk,
    ActionProvenance,
    PolicyCapabilities,
    RobotInterface,
    compatibility_problems,
)

__requirements__ = ("REQ11", "REQ12")


class PolicyTransport(Protocol):
    async def request(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``kind`` is ``"describe"`` or ``"infer"``. The binding owns the wire format."""
        ...


class PolicyUnavailable(FrameworkError):
    code = "policy_unavailable"


class StaleChunk(FrameworkError):
    code = "stale_action_chunk"


class LeRobotPolicyClient:
    def __init__(self, transport: PolicyTransport, *, describe_timeout_s: float = 10.0,
                 action_timeout_s: float = 5.0, max_source_age_s: float = 5.0,
                 clock: Clock | None = None) -> None:
        self.transport = transport
        self.describe_timeout = describe_timeout_s
        self.action_timeout = action_timeout_s
        self.max_source_age = max_source_age_s
        self.clock = clock or MonotonicClock()
        self.capabilities: PolicyCapabilities | None = None
        self.admitted = False
        self.session_id = ""

    async def admit(self, robot: RobotInterface) -> PolicyCapabilities:
        """Fetch the advertised contract and refuse an incompatible policy before motion."""
        try:
            raw = await asyncio.wait_for(self.transport.request("describe", {}),
                                         self.describe_timeout)
        except TimeoutError as exc:
            raise PolicyUnavailable("policy server did not describe itself in time") from exc
        caps = PolicyCapabilities.from_dict(raw)
        problems = compatibility_problems(caps, robot)
        if problems:
            raise AdmissionRejected("policy_incompatible", "policy and robot contracts differ",
                                    problems=problems)
        self.capabilities, self.admitted, self.session_id = caps, True, new_id("sess")
        return caps

    async def infer(self, features: Mapping[str, Any], *, task: str,
                    capture_time: float) -> ActionChunk:
        if not self.admitted or self.capabilities is None:
            raise AdmissionRejected("policy_not_admitted", "admit() the policy first")
        age = self.clock.now() - capture_time
        if age > self.max_source_age:
            raise StaleChunk("observation is too old to infer from", age_s=age)
        rid = new_id("req")
        try:
            raw = await asyncio.wait_for(self.transport.request("infer", {
                "features": dict(features), "task": task, "request_id": rid,
                "session_id": self.session_id}), self.action_timeout)
        except TimeoutError as exc:
            raise PolicyUnavailable("no action chunk before the action timeout",
                                    timeout_s=self.action_timeout) from exc
        if raw.get("request_id") != rid:
            raise PolicyUnavailable("reply does not belong to this request")
        caps = self.capabilities
        chunk = ActionChunk(
            actions=tuple(tuple(float(v) for v in row) for row in raw["actions"]),
            provenance=ActionProvenance(capture_time=capture_time, task=task, request_id=rid,
                                        session_id=self.session_id,
                                        artifact_identity=str(raw.get("artifact_identity",
                                                                      ""))),
            execution_steps=int(raw.get("execution_steps", caps.execution_steps)),
            received_at=self.clock.now(),
            server_durations=dict(raw.get("server_durations", {})))
        chunk.validate(caps)
        return chunk

    def usable_steps(self, chunk: ActionChunk) -> int:
        """Steps still inside the policy horizon, counted from the observation capture."""
        assert self.capabilities is not None
        elapsed = self.clock.now() - chunk.provenance.capture_time
        left = chunk.execution_steps - int(elapsed / self.capabilities.action_interval)
        if left <= 0:
            raise StaleChunk("action chunk horizon has passed", elapsed_s=elapsed)
        return left


__all__ = ["LeRobotPolicyClient", "PolicyTransport", "PolicyUnavailable", "StaleChunk"]
