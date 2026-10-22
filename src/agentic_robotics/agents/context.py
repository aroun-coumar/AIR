"""Context selection for agent decisions (baseline section 12).

Builds a compact, structured view of what a decision needs: current capability,
relevant observations with their age and quality, and explicit stale flags. It keeps
references (revisions, sources) rather than raw streams.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ..contracts.observation import Quality
from ..robot import Robot


class ContextBuilder:
    def __init__(self, stale_after_s: float = 2.0) -> None:
        self.stale_after_s = stale_after_s

    def robot_view(self, robot: Robot, now: float,
                   signals: Iterable[str] | None = None) -> dict[str, Any]:
        cap = robot.capability_report()
        wanted = set(signals) if signals is not None else None
        observations = {}
        for name, obs in sorted(robot.observe().items()):
            if wanted is not None and name not in wanted:
                continue
            age = obs.age(now)
            entry: dict[str, Any] = {"value": obs.value, "age_s": round(age, 3)}
            if obs.unit:
                entry["unit"] = obs.unit
            if obs.frame:
                entry["frame"] = obs.frame
            if obs.quality is not Quality.GOOD:
                entry["quality"] = obs.quality.value
            if age > self.stale_after_s:
                entry["stale"] = True
            observations[name] = entry
        return {
            "robot_id": robot.robot_id,
            "capability_revision": cap.capability_revision,
            "capabilities": {
                p.name: ({"status": p.status.value}
                         | ({"limiting_factors": list(p.limiting_factors)}
                            if p.limiting_factors else {}))
                for p in cap.profiles
            },
            "observations": observations,
        }

    @staticmethod
    def size(context: dict[str, Any]) -> int:
        return len(json.dumps(context, separators=(",", ":"), default=str))

    @staticmethod
    def fingerprint(goal: str, robots: Iterable[Robot]) -> str:
        """Identity of a decision situation: goal plus capability revisions.

        Observations are deliberately excluded so routine sensor updates do not force
        a new model call; a capability change does.
        """
        key = {"goal": goal, "robots": sorted(
            (r.robot_id, r.capability_report().capability_revision) for r in robots)}
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]
