"""A fake openpi websocket policy server (in-memory), following
``serving/websocket_policy_server.py``: metadata frame first, then one msgpack reply per
msgpack observation, ``server_timing`` added, a text traceback frame on error."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from . import msgpack_codec as mp

__requirements__ = ("REQ11", "REQ18")


class FakeOpenPiServer:
    def __init__(self, metadata: dict[str, Any] | None = None, *, horizon: int = 4,
                 action_dim: int = 2, policy: Callable[[dict[Any, Any]], Any] | None = None
                 ) -> None:
        self.metadata = metadata if metadata is not None else {"action_horizon": horizon}
        self.horizon = horizon
        self.dim = action_dim
        self.policy = policy
        self.observations: list[dict[Any, Any]] = []
        self.refuse_connections = 0
        self.stall = False
        self.fail_next = False
        self.connections = 0

    def transport(self) -> FakeWsTransport:
        return FakeWsTransport(self)

    def answer(self, frame: bytes) -> bytes | str | None:
        obs = mp.unpackb(frame)
        self.observations.append(obs)
        if self.fail_next:
            self.fail_next = False
            return "Traceback (most recent call last):\n  ValueError: bad observation"
        if self.stall:
            return None
        if self.policy is not None:
            actions = self.policy(obs)
        else:
            actions = mp.NdArray.from_list([[0.1 * (i + 1)] * self.dim
                                            for i in range(self.horizon)])
        return mp.packb({"actions": actions, "server_timing": {"infer_ms": 1.5}})


class FakeWsTransport:
    def __init__(self, server: FakeOpenPiServer) -> None:
        self.server = server
        self._inbox: asyncio.Queue[bytes | str] = asyncio.Queue()
        self.open = False

    async def connect(self) -> None:
        if self.server.refuse_connections > 0:
            self.server.refuse_connections -= 1
            raise ConnectionRefusedError("connection refused")
        self.open = True
        self.server.connections += 1
        await self._inbox.put(mp.packb(self.server.metadata))

    async def send(self, data: bytes) -> None:
        if not self.open:
            raise ConnectionError("closed")
        reply = self.server.answer(data)
        if reply is not None:
            await self._inbox.put(reply)

    async def recv(self) -> bytes | str:
        if not self.open:
            raise ConnectionError("closed")
        return await self._inbox.get()

    async def close(self) -> None:
        self.open = False
        self._inbox = asyncio.Queue()


__all__ = ["FakeOpenPiServer", "FakeWsTransport"]
