"""Clock abstractions.

Timestamps are only meaningful together with their clock domain (baseline section 14).
The runtime never reads wall time directly; it asks a :class:`Clock`, which lets the
same code run against real time, simulated time, or a manually stepped test clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SyncState(StrEnum):
    LOCKED = "locked"
    HOLDOVER = "holdover"
    UNSYNCHRONIZED = "unsynchronized"


@dataclass(frozen=True)
class ClockStatus:
    domain: str
    sync_state: SyncState
    offset_uncertainty_s: float


@runtime_checkable
class Clock(Protocol):
    domain: str

    def now(self) -> float:
        """Seconds in this clock's domain."""
        ...

    def status(self) -> ClockStatus: ...


class MonotonicClock:
    """Process-local monotonic clock. Always synchronized with itself."""

    def __init__(self, domain: str = "host.monotonic") -> None:
        self.domain = domain

    def now(self) -> float:
        return time.monotonic()

    def status(self) -> ClockStatus:
        return ClockStatus(self.domain, SyncState.LOCKED, 0.0)


class ManualClock:
    """A clock that only moves when told to. Used for simulation and tests."""

    def __init__(
        self,
        start: float = 0.0,
        domain: str = "sim",
        sync_state: SyncState = SyncState.LOCKED,
        offset_uncertainty_s: float = 0.0,
    ) -> None:
        self.domain = domain
        self._t = start
        self.sync_state = sync_state
        self.offset_uncertainty_s = offset_uncertainty_s

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("ManualClock cannot move backwards")
        self._t += seconds

    def status(self) -> ClockStatus:
        return ClockStatus(self.domain, self.sync_state, self.offset_uncertainty_s)
