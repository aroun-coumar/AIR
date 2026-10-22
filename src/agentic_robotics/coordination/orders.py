"""Orders: released prefix, horizon, and update acceptance (VDA 5050 v3 sections 4.1 and
6.1.4, adopted as a pattern; no code or spec text copied).

The coordinator releases work to a participant as *order updates*. An update carries
the newly released steps (each a fully specified, fenced command), the ``stitch_after``
step it continues from, and the remaining *horizon* (task ids only: planned, not
executable). The released part is immutable: an update can only append after the last
released step.

Acceptance rules, applied by the robot side (:class:`OrderBook`):

- an update id lower than the current one is rejected (``outdated``);
- the same id with identical content is ignored (a retransmission);
- the same id with different content is rejected (``conflicting_update``);
- nothing is accepted after the order was cancelled (``order_cancelled``);
- a higher id must stitch onto the last released step (``stitch_mismatch``) and must not
  re-release a step (``released_step_changed``);
- on rejection the robot keeps its current work unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__requirements__ = ("REQ07", "REQ09", "G07", "G14")


@dataclass(frozen=True)
class OrderStep:
    """One released step: what the robot may execute without asking again."""

    task_id: str
    task_revision: int
    command_id: str
    skill_id: str
    args: dict[str, Any] = field(default_factory=dict)
    fencing: dict[str, int] = field(default_factory=dict)
    after: tuple[str, ...] = ()   # released steps (task ids) that must verify first

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "task_revision": self.task_revision,
                "command_id": self.command_id, "skill_id": self.skill_id,
                "args": dict(self.args), "fencing": dict(self.fencing),
                "after": list(self.after)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OrderStep:
        return cls(task_id=str(d["task_id"]), task_revision=int(d.get("task_revision", 0)),
                   command_id=str(d["command_id"]), skill_id=str(d["skill_id"]),
                   args=dict(d.get("args", {})),
                   fencing={str(k): int(v) for k, v in dict(d.get("fencing", {})).items()},
                   after=tuple(str(a) for a in d.get("after", ())))


@dataclass(frozen=True)
class OrderUpdate:
    order_id: str
    update_id: int
    steps: tuple[OrderStep, ...]
    stitch_after: str | None = None
    horizon: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "stitch_after": self.stitch_after,
                "steps": [s.to_dict() for s in self.steps], "horizon": list(self.horizon)}

    @classmethod
    def from_payload(cls, update_id: int, p: dict[str, Any]) -> OrderUpdate:
        return cls(order_id=str(p["order_id"]), update_id=update_id,
                   steps=tuple(OrderStep.from_dict(s) for s in p.get("steps", ())),
                   stitch_after=p.get("stitch_after"),
                   horizon=tuple(str(h) for h in p.get("horizon", ())))

    def digest(self) -> str:
        """Content identity of the update (horizon included: changing it is a change)."""
        body = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Acceptance(StrEnum):
    ACCEPTED = "accepted"
    IGNORED_IDENTICAL = "ignored_identical"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Verdict:
    acceptance: Acceptance
    reason: str = ""

    @property
    def rejected(self) -> bool:
        return self.acceptance is Acceptance.REJECTED


@dataclass
class OrderBook:
    """Robot-side state of one order: what has been released and what may follow."""

    order_id: str
    update_id: int = 0
    steps: list[OrderStep] = field(default_factory=list)
    horizon: tuple[str, ...] = ()
    cancelled: bool = False
    digests: dict[int, str] = field(default_factory=dict)

    @property
    def last_released(self) -> str | None:
        return self.steps[-1].task_id if self.steps else None

    def released(self, task_id: str) -> OrderStep | None:
        return next((s for s in self.steps if s.task_id == task_id), None)

    def check(self, update: OrderUpdate) -> Verdict:
        """Apply the acceptance rules without changing anything."""
        if update.order_id != self.order_id:
            return Verdict(Acceptance.REJECTED, "wrong_order")
        if self.cancelled:
            return Verdict(Acceptance.REJECTED, "order_cancelled")
        if update.update_id == self.update_id and self.update_id in self.digests:
            if self.digests[self.update_id] == update.digest():
                return Verdict(Acceptance.IGNORED_IDENTICAL, "identical")
            return Verdict(Acceptance.REJECTED, "conflicting_update")
        if update.update_id <= self.update_id:
            return Verdict(Acceptance.REJECTED, "outdated")
        if update.stitch_after != self.last_released:
            return Verdict(Acceptance.REJECTED, "stitch_mismatch")
        known = {s.task_id for s in self.steps}
        if any(s.task_id in known for s in update.steps):
            return Verdict(Acceptance.REJECTED, "released_step_changed")
        return Verdict(Acceptance.ACCEPTED)

    def offer(self, update: OrderUpdate) -> Verdict:
        verdict = self.check(update)
        if verdict.acceptance is Acceptance.ACCEPTED:
            self.update_id = update.update_id
            self.digests[update.update_id] = update.digest()
            self.steps.extend(update.steps)
            self.horizon = update.horizon
        return verdict
