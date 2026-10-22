"""Hybrid logical clocks for coordination messages.

A hybrid logical clock (Kulkarni et al., "Logical Physical Clocks", 2014; the pattern
dora-rs uses through ``uhlc``) gives every message a timestamp that

- never runs backwards on one node,
- is always greater than the timestamp of any message the node has received
  (so ``send < receive`` holds across nodes: causality is visible), and
- stays close to physical time (unlike a pure Lamport counter).

A timestamp is ``(l, c, node)``: ``l`` is the largest physical time seen, in integer
microseconds of the clock domain; ``c`` is a logical counter that breaks ties. A remote
timestamp further ahead of local physical time than ``max_offset_s`` is rejected
instead of dragging the local clock forward (uhlc's ``delta`` check).

Implemented from the paper's algorithm; no code was copied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.errors import FrameworkError

__requirements__ = ("REQ10", "REQ12")


class ClockDriftError(FrameworkError):
    """A remote HLC timestamp is too far ahead of local physical time."""

    code = "clock_drift"


@dataclass(frozen=True, order=True)
class HlcTimestamp:
    l: int  # noqa: E741 - the paper's name for the physical component
    c: int
    node: str

    def __str__(self) -> str:
        return f"{self.l}.{self.c}@{self.node}"

    @classmethod
    def parse(cls, text: str) -> HlcTimestamp:
        try:
            stamp, node = text.split("@", 1)
            l_text, c_text = stamp.split(".", 1)
            return cls(int(l_text), int(c_text), node)
        except ValueError as exc:
            raise FrameworkError("malformed HLC timestamp", value=text) from exc


class HybridLogicalClock:
    def __init__(self, node: str, physical: Callable[[], float], *,
                 max_offset_s: float = 60.0) -> None:
        self.node = node
        self._physical = physical
        self.max_offset_us = int(max_offset_s * 1_000_000)
        self._l = 0
        self._c = 0

    def _pt(self) -> int:
        return int(self._physical() * 1_000_000)

    def now(self) -> HlcTimestamp:
        """Timestamp for a local or send event."""
        pt = self._pt()
        if pt > self._l:
            self._l, self._c = pt, 0
        else:
            self._c += 1
        return HlcTimestamp(self._l, self._c, self.node)

    def update(self, remote: HlcTimestamp | str) -> HlcTimestamp:
        """Merge a received timestamp; returns the receive event's timestamp."""
        m = HlcTimestamp.parse(remote) if isinstance(remote, str) else remote
        pt = self._pt()
        if m.l - pt > self.max_offset_us:
            raise ClockDriftError("remote HLC timestamp exceeds the allowed offset",
                                  remote=str(m), local_physical_us=pt,
                                  max_offset_us=self.max_offset_us)
        l_new = max(self._l, m.l, pt)
        if l_new == self._l == m.l:
            c_new = max(self._c, m.c) + 1
        elif l_new == self._l:
            c_new = self._c + 1
        elif l_new == m.l:
            c_new = m.c + 1
        else:
            c_new = 0
        self._l, self._c = l_new, c_new
        return HlcTimestamp(self._l, self._c, self.node)

    @property
    def last(self) -> HlcTimestamp:
        return HlcTimestamp(self._l, self._c, self.node)
