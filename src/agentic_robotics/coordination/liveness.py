"""Liveness and message ordering for team coordination.

- :class:`Stamper` gives every outgoing message a per-channel sequence number (VDA 5050
  ``headerId``: +1 per message from one sender to one recipient) and an HLC timestamp.
- :class:`SeqTracker` classifies what arrives per sender: in order, a gap (messages
  lost), a duplicate, or reordered. Gaps are evidence, not errors: revision and order
  update rules keep the protocol correct under loss.
- :class:`LivenessMonitor` derives each peer's :class:`ConnectionState` from transport
  facts (liveliness tokens, the zenoh/rmw_zenoh pattern, and last-will style
  ``CONNECTION_BROKEN`` events) and from heartbeats. Liveness is *derived*, never
  configured: a peer that stops being heard from becomes CONNECTION_BROKEN.

What a broken connection means is decided by the coordinator: holds are kept and the
peer's in-flight tasks become UNRESOLVED. One lost peer never resets the team.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .hlc import HybridLogicalClock

__requirements__ = ("REQ06", "REQ10", "REQ12", "G14")


class ConnectionState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"                      # left gracefully
    CONNECTION_BROKEN = "connection_broken"  # lost without saying goodbye (last will)


class SeqStatus(StrEnum):
    UNSTAMPED = "unstamped"
    FIRST = "first"
    IN_ORDER = "in_order"
    GAP = "gap"
    DUPLICATE = "duplicate"
    REORDERED = "reordered"


@dataclass(frozen=True)
class SeqVerdict:
    status: SeqStatus
    expected: int = 0
    missing: int = 0


class Stamper:
    """Per-recipient sequence numbers plus HLC timestamps for one sender."""

    def __init__(self, hlc: HybridLogicalClock) -> None:
        self.hlc = hlc
        self._next: dict[str, int] = {}

    def stamp(self, recipient: str) -> tuple[int, str]:
        seq = self._next.get(recipient, 0) + 1
        self._next[recipient] = seq
        return seq, str(self.hlc.now())


class SeqTracker:
    """Classify incoming sequence numbers per sender (receiver side)."""

    def __init__(self) -> None:
        self._high: dict[str, int] = {}
        self._seen: dict[str, set[int]] = {}

    def observe(self, sender: str, seq: int) -> SeqVerdict:
        if seq <= 0:
            return SeqVerdict(SeqStatus.UNSTAMPED)
        seen = self._seen.setdefault(sender, set())
        high = self._high.get(sender)
        if seq in seen:
            return SeqVerdict(SeqStatus.DUPLICATE, expected=(high or 0) + 1)
        seen.add(seq)
        if high is None:
            self._high[sender] = seq
            return SeqVerdict(SeqStatus.FIRST if seq == 1 else SeqStatus.GAP, expected=1,
                              missing=seq - 1)
        if seq == high + 1:
            self._high[sender] = seq
            return SeqVerdict(SeqStatus.IN_ORDER, expected=seq)
        if seq > high + 1:
            self._high[sender] = seq
            return SeqVerdict(SeqStatus.GAP, expected=high + 1, missing=seq - high - 1)
        return SeqVerdict(SeqStatus.REORDERED, expected=high + 1)

    def high(self, sender: str) -> int:
        return self._high.get(sender, 0)


@dataclass
class PeerLiveness:
    endpoint: str
    state: ConnectionState = ConnectionState.ONLINE
    last_heard: float | None = None
    changes: list[tuple[float, str, str]] = field(default_factory=list)


Listener = Callable[[str, ConnectionState, ConnectionState, str], None]


class LivenessMonitor:
    """Connection state per peer from heartbeats and transport liveliness facts."""

    def __init__(self, now: Callable[[], float], *,
                 heartbeat_timeout_s: float | None = None) -> None:
        self._now = now
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.peers: dict[str, PeerLiveness] = {}
        self._listeners: list[Listener] = []

    def on_change(self, listener: Listener) -> None:
        """``listener(endpoint, old, new, cause)``."""
        self._listeners.append(listener)

    def state(self, endpoint: str) -> ConnectionState:
        peer = self.peers.get(endpoint)
        return peer.state if peer else ConnectionState.ONLINE

    def heard(self, endpoint: str) -> None:
        """Any message from a peer proves it is reachable now."""
        peer = self.peers.setdefault(endpoint, PeerLiveness(endpoint))
        peer.last_heard = self._now()
        if peer.state is not ConnectionState.ONLINE:
            self._set(peer, ConnectionState.ONLINE, "heard_from")

    def liveliness(self, endpoint: str, alive: bool, graceful: bool) -> None:
        """A transport fact: the peer's liveliness token appeared or disappeared."""
        peer = self.peers.setdefault(endpoint, PeerLiveness(endpoint))
        if alive:
            peer.last_heard = self._now()
            self._set(peer, ConnectionState.ONLINE, "liveliness_token")
        else:
            self._set(peer, ConnectionState.OFFLINE if graceful
                      else ConnectionState.CONNECTION_BROKEN,
                      "graceful_leave" if graceful else "liveliness_lost")

    def check(self) -> list[str]:
        """Mark peers silent for longer than the heartbeat timeout as broken."""
        if self.heartbeat_timeout_s is None:
            return []
        now, broken = self._now(), []
        for peer in self.peers.values():
            if (peer.state is ConnectionState.ONLINE and peer.last_heard is not None
                    and now - peer.last_heard > self.heartbeat_timeout_s):
                self._set(peer, ConnectionState.CONNECTION_BROKEN, "heartbeat_timeout")
                broken.append(peer.endpoint)
        return broken

    def _set(self, peer: PeerLiveness, new: ConnectionState, cause: str) -> None:
        old = peer.state
        if old is new:
            return
        peer.state = new
        peer.changes.append((self._now(), new.value, cause))
        for listener in list(self._listeners):
            listener(peer.endpoint, old, new, cause)
