"""Task allocation. Products can supply their own :class:`Allocator`; the coordinator
only requires that it pick one of the bids it was given (or none)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .protocol import MissionTask

__requirements__ = ("REQ09",)


@dataclass(frozen=True)
class Bid:
    task_id: str
    participant: str
    cost: float
    capability_revision: int
    detail: str = ""


class Allocator(Protocol):
    name: str

    def choose(self, task: MissionTask, bids: list[Bid]) -> Bid | None: ...


class LowestCostAllocator:
    """Pick the cheapest bid; ties go to the lexicographically smallest participant so
    allocation is deterministic and reproducible."""

    name = "lowest_cost"

    def choose(self, task: MissionTask, bids: list[Bid]) -> Bid | None:
        if not bids:
            return None
        return min(bids, key=lambda b: (b.cost, b.participant))
