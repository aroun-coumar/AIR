"""Message transport between coordinator and participants.

:class:`LocalTransport` delivers in-process but behaves like a network: messages are
asynchronous, and delay, loss, duplication and disconnection can be injected per
endpoint and per message kind. Coordination code must be correct under all of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..core.errors import FrameworkError
from .protocol import CoordinationMessage, MessageKind

__requirements__ = ("REQ06", "REQ09")

Handler = Callable[[CoordinationMessage], Awaitable[None]]


class Transport(Protocol):
    def register(self, endpoint: str, handler: Handler) -> None: ...

    async def send(self, message: CoordinationMessage) -> None: ...


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

    def register(self, endpoint: str, handler: Handler) -> None:
        self._handlers[endpoint] = handler

    def link(self, endpoint: str) -> LinkFaults:
        """Fault settings for messages to *or from* ``endpoint``."""
        return self.faults.setdefault(endpoint, LinkFaults())

    def disconnect(self, endpoint: str) -> None:
        self.link(endpoint).disconnected = True

    def reconnect(self, endpoint: str) -> None:
        self.link(endpoint).disconnected = False

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
        if self.faults.get(recipient, LinkFaults()).disconnected:
            self.stats.dropped += 1
            return
        self.stats.delivered += 1
        await self._handlers[recipient](message)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
