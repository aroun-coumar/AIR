"""Message transport between coordinator and participants.

:class:`LocalTransport` delivers in-process but behaves like a network: messages are
asynchronous, and delay, loss, duplication and disconnection can be injected per
endpoint and per message kind. Coordination code must be correct under all of them.

It also provides *liveliness* (the zenoh / rmw_zenoh pattern): an endpoint declares a
liveliness token and subscribers learn when tokens appear and disappear. A graceful
``undeclare_liveliness`` is an OFFLINE fact; a ``disconnect`` drops the token without a
goodbye, which subscribers see as CONNECTION_BROKEN (the MQTT last-will pattern VDA 5050
uses). Presence is derived from these facts, never configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..core.errors import FrameworkError
from .protocol import CoordinationMessage, MessageKind

__requirements__ = ("REQ06", "REQ09", "G14")

Handler = Callable[[CoordinationMessage], Awaitable[None]]
#: ``callback(endpoint, alive, graceful)``
LivelinessCallback = Callable[[str, bool, bool], None]


class Transport(Protocol):
    def register(self, endpoint: str, handler: Handler) -> None: ...

    async def send(self, message: CoordinationMessage) -> None: ...


@runtime_checkable
class LivelinessTransport(Protocol):
    """Optional transport capability: liveliness tokens and their subscribers."""

    def declare_liveliness(self, endpoint: str) -> None: ...

    def undeclare_liveliness(self, endpoint: str) -> None: ...

    def subscribe_liveliness(self, subscriber: str, callback: LivelinessCallback) -> None: ...

    def alive(self, endpoint: str) -> bool: ...


@dataclass
class LinkFaults:
    delay_s: float = 0.0
    disconnected: bool = False
    duplicate: bool = False
    drop_kinds: set[MessageKind] = field(default_factory=set)


@dataclass
class TransportStats:
    sent: int = 0
    delivered: int = 0
    dropped: int = 0
    duplicated: int = 0
    per_recipient: dict[str, int] = field(default_factory=dict)
    per_kind: dict[str, int] = field(default_factory=dict)


class LocalTransport:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.default_delay_s = delay_s
        self._handlers: dict[str, Handler] = {}
        self.faults: dict[str, LinkFaults] = {}
        self.stats = TransportStats()
        self.log: list[CoordinationMessage] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._tokens: set[str] = set()
        self._subscribers: dict[str, list[LivelinessCallback]] = {}

    def register(self, endpoint: str, handler: Handler) -> None:
        self._handlers[endpoint] = handler

    def link(self, endpoint: str) -> LinkFaults:
        """Fault settings for messages to *or from* ``endpoint``."""
        return self.faults.setdefault(endpoint, LinkFaults())

    def disconnect(self, endpoint: str) -> None:
        """Cut ``endpoint`` off without a goodbye (its liveliness token is lost)."""
        was = self.alive(endpoint)
        self.link(endpoint).disconnected = True
        if was:
            self._partition(endpoint, alive=False)

    def reconnect(self, endpoint: str) -> None:
        was = self.alive(endpoint)
        self.link(endpoint).disconnected = False
        if not was and endpoint in self._tokens:
            self._partition(endpoint, alive=True)

    # -- liveliness ------------------------------------------------------------------------
    def declare_liveliness(self, endpoint: str) -> None:
        self._tokens.add(endpoint)
        if self.alive(endpoint):
            self._notify_others(endpoint, alive=True, graceful=True)

    def undeclare_liveliness(self, endpoint: str) -> None:
        if endpoint not in self._tokens:
            return
        was = self.alive(endpoint)
        self._tokens.discard(endpoint)
        if was:
            self._notify_others(endpoint, alive=False, graceful=True)

    def subscribe_liveliness(self, subscriber: str, callback: LivelinessCallback) -> None:
        self._subscribers.setdefault(subscriber, []).append(callback)

    def alive(self, endpoint: str) -> bool:
        return endpoint in self._tokens and not self.link(endpoint).disconnected

    def _notify_others(self, endpoint: str, *, alive: bool, graceful: bool) -> None:
        for subscriber, callbacks in list(self._subscribers.items()):
            if subscriber == endpoint or self.link(subscriber).disconnected:
                continue
            for cb in list(callbacks):
                cb(endpoint, alive, graceful)

    def _partition(self, endpoint: str, *, alive: bool) -> None:
        """``endpoint`` joined or left the reachable side: both sides learn of it."""
        self._notify_others(endpoint, alive=alive, graceful=False)
        for cb in list(self._subscribers.get(endpoint, [])):
            for other in sorted(self._tokens - {endpoint}):
                if not self.link(other).disconnected:
                    cb(other, alive, False)

    # -- messages ---------------------------------------------------------------------------
    async def send(self, message: CoordinationMessage) -> None:
        self.stats.sent += 1
        self.log.append(message)
        self.stats.per_kind[message.kind.value] = self.stats.per_kind.get(
            message.kind.value, 0) + 1
        sender = self.faults.get(message.sender, LinkFaults())
        for recipient in message.recipients:
            if recipient not in self._handlers:
                raise FrameworkError("unknown endpoint", endpoint=recipient)
            link = self.faults.get(recipient, LinkFaults())
            if sender.disconnected or link.disconnected or \
                    message.kind in link.drop_kinds or message.kind in sender.drop_kinds:
                self.stats.dropped += 1
                continue
            self.stats.per_recipient[recipient] = self.stats.per_recipient.get(recipient, 0) + 1
            delay = max(self.default_delay_s, link.delay_s, sender.delay_s)
            copies = 2 if (link.duplicate or sender.duplicate) else 1
            self.stats.duplicated += copies - 1
            for _ in range(copies):
                task = asyncio.get_running_loop().create_task(
                    self._deliver(recipient, message, delay))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _deliver(self, recipient: str, message: CoordinationMessage,
                       delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        # A link that went down while the message was in flight loses it.
        if self.faults.get(recipient, LinkFaults()).disconnected or \
                self.faults.get(message.sender, LinkFaults()).disconnected:
            self.stats.dropped += 1
            return
        self.stats.delivered += 1
        await self._handlers[recipient](message)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
