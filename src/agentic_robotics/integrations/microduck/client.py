"""A JSON-RPC/NDJSON client for robotd with bounded waits (never blocks forever)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from . import protocol as p
from .transport import LineTransport

__requirements__ = ("REQ03", "REQ12")


class RobotdClient:
    def __init__(self, transport: LineTransport, *, timeout_s: float = 2.0,
                 on_state: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.on_state = on_state
        self.connected = False
        self.hello: dict[str, Any] | None = None
        self.protocol_errors = 0
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> dict[str, Any]:
        """Start reading and perform the ``hello`` handshake. Returns ``HelloResult``."""
        self.connected = True
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        result = await self.call(p.Method.HELLO, {"api_version": p.API_VERSION})
        if not isinstance(result, dict) or not isinstance(result.get("api_version"), int):
            raise p.ProtocolError("malformed hello result", result=result)
        self.hello = result
        return result

    async def close(self) -> None:
        self.connected = False
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        with contextlib.suppress(Exception):
            await self.transport.close()
        self._fail_pending(ConnectionError("client closed"))

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   timeout_s: float | None = None) -> Any:
        if not self.connected:
            raise ConnectionError("not connected to robotd")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.transport.send(p.request(rid, method, params))
            return await asyncio.wait_for(fut, timeout_s or self.timeout_s)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.connected:
            raise ConnectionError("not connected to robotd")
        await self.transport.send(p.notification(method, params))

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.transport.recv()
                if not line.strip():
                    continue
                try:
                    msg = p.decode(line)
                except p.ProtocolError:
                    self.protocol_errors += 1
                    continue
                if msg.is_notification:
                    if msg.method == p.Method.STATE and isinstance(msg.params, dict) \
                            and self.on_state is not None:
                        self.on_state(msg.params)
                    continue
                fut = self._pending.get(msg.id) if isinstance(msg.id, int) else None
                if fut is None or fut.done():
                    continue
                if msg.error is not None:
                    fut.set_exception(p.RpcError(int(msg.error["code"]),
                                                 str(msg.error.get("message", "")),
                                                 msg.error.get("data")))
                else:
                    fut.set_result(msg.result)
        except ConnectionError as exc:
            self.connected = False
            self._fail_pending(exc)
        except asyncio.CancelledError:
            raise

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)


__all__ = ["RobotdClient"]
