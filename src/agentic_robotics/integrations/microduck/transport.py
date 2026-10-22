"""Line transports for robotd: an injectable seam between the adapter and the socket.

``UnixSocketTransport`` is the production path (``/run/robotd.sock`` on the robot, or the
socket ``duck-sim`` exposes); ``TcpTransport`` reaches a forwarded socket; tests use
:func:`memory_pair` with :class:`~.fake_robotd.FakeRobotd` on the other end.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from ...core.errors import UnsupportedOperation

__requirements__ = ("REQ03",)


class LineTransport(Protocol):
    async def send(self, line: str) -> None: ...

    async def recv(self) -> str:
        """Next line (with or without the newline). Raises ``ConnectionError`` at EOF."""
        ...

    async def close(self) -> None: ...


class _StreamTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, line: str) -> None:
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def recv(self) -> str:
        data = await self._reader.readline()
        if not data:
            raise ConnectionError("robotd closed the connection")
        return data.decode("utf-8")

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def UnixSocketTransport(path: str) -> LineTransport:
    if not hasattr(asyncio, "open_unix_connection"):
        raise UnsupportedOperation("unix sockets are not available on this platform",
                                   path=path)
    reader, writer = await asyncio.open_unix_connection(path)
    return _StreamTransport(reader, writer)


async def TcpTransport(host: str, port: int) -> LineTransport:
    reader, writer = await asyncio.open_connection(host, port)
    return _StreamTransport(reader, writer)


class MemoryTransport:
    """One end of an in-process line pipe."""

    def __init__(self, inbox: asyncio.Queue[str | None],
                 outbox: asyncio.Queue[str | None]) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self.closed = False

    async def send(self, line: str) -> None:
        if self.closed:
            raise ConnectionError("transport closed")
        await self._outbox.put(line)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is None:
            self.closed = True
            raise ConnectionError("peer closed the connection")
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self._outbox.put(None)


def memory_pair() -> tuple[MemoryTransport, MemoryTransport]:
    a: asyncio.Queue[str | None] = asyncio.Queue()
    b: asyncio.Queue[str | None] = asyncio.Queue()
    return MemoryTransport(a, b), MemoryTransport(b, a)


__all__ = ["LineTransport", "MemoryTransport", "TcpTransport", "UnixSocketTransport",
           "memory_pair"]
