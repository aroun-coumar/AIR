"""An openpi policy client with timeouts, bounded reconnects and a staleness bound.

Wire protocol, verified from ``Physical-Intelligence/openpi`` (HEAD 215abfb, Apache-2.0):
``websocket_client_policy.py`` and ``serving/websocket_policy_server.py``.

1. The client opens a websocket (``Authorization: Api-Key <key>`` when a key is set).
2. The server sends one binary frame first: msgpack-encoded **server metadata**.
3. Each request is one binary frame (msgpack observation dict); each reply is one binary
   frame (msgpack action dict, with ``server_timing.infer_ms``). A **text** frame is a
   server-side traceback, after which the server closes the connection.
4. ``GET /healthz`` answers ``OK``.

What AIR changes relative to the reference client (the counter-example in the survey):
every wait has a timeout; connecting is attempted a bounded number of times; after a
timeout the connection is discarded (a late reply would pair with the next request);
and action chunks carry their receive time and are refused once older than
``max_chunk_age_s`` instead of being replayed open-loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...core.clock import Clock, MonotonicClock
from ...core.errors import FrameworkError, UnsupportedOperation
from . import msgpack_codec as mp

__requirements__ = ("REQ11", "REQ12")


class PolicyError(FrameworkError):
    code = "policy_error"


class PolicyTimeout(PolicyError):
    code = "policy_timeout"


class PolicyServerError(PolicyError):
    """The server answered with a traceback (text frame) and closed the connection."""

    code = "policy_server_error"


class StaleActionChunk(PolicyError):
    code = "stale_action_chunk"


class WsTransport(Protocol):
    async def connect(self) -> None: ...

    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes | str: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ActionChunk:
    actions: Any                      # decoded; NdArray for arrays
    received_at: float
    request_seq: int
    server_timing: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def age(self, now: float) -> float:
        return now - self.received_at


class ActionQueue:
    """Hands out the steps of one chunk while it is fresh. Never replays a stale chunk."""

    def __init__(self, chunk: ActionChunk, *, max_age_s: float, execution_steps: int | None,
                 clock: Clock) -> None:
        rows = chunk.actions.tolist() if isinstance(chunk.actions, mp.NdArray) \
            else list(chunk.actions)
        self._rows = rows[:execution_steps] if execution_steps else rows
        self._chunk = chunk
        self._max_age = max_age_s
        self._clock = clock

    def __len__(self) -> int:
        return len(self._rows)

    def next(self) -> Any:
        age = self._chunk.age(self._clock.now())
        if age > self._max_age:
            self._rows.clear()
            raise StaleActionChunk("action chunk is older than its staleness bound",
                                   age_s=round(age, 4), max_age_s=self._max_age)
        if not self._rows:
            raise IndexError("action chunk exhausted")
        return self._rows.pop(0)

    def drop(self) -> None:
        """Interrupt: discard queued actions."""
        self._rows.clear()


class OpenPiPolicyClient:
    def __init__(self, transport: WsTransport, *, connect_timeout_s: float = 10.0,
                 infer_timeout_s: float = 5.0, max_connect_attempts: int = 3,
                 retry_delay_s: float = 1.0, max_chunk_age_s: float = 0.5,
                 clock: Clock | None = None) -> None:
        self.transport = transport
        self.connect_timeout = connect_timeout_s
        self.infer_timeout = infer_timeout_s
        self.max_attempts = max(1, max_connect_attempts)
        self.retry_delay = retry_delay_s
        self.max_chunk_age = max_chunk_age_s
        self.clock = clock or MonotonicClock()
        self.metadata: dict[str, Any] | None = None
        self.connected = False
        self._seq = 0

    async def connect(self) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                await asyncio.wait_for(self.transport.connect(), self.connect_timeout)
                first = await asyncio.wait_for(self.transport.recv(), self.connect_timeout)
                if not isinstance(first, bytes):
                    raise PolicyError("expected binary server metadata as the first frame")
                meta = mp.unpackb(first)
                if not isinstance(meta, dict):
                    raise PolicyError("server metadata is not a map")
                self.metadata = {_key(k): v for k, v in meta.items()}
                self.connected = True
                return self.metadata
            except (TimeoutError, ConnectionError, OSError, PolicyError, mp.CodecError) as exc:
                last = exc
                with contextlib.suppress(Exception):
                    await self.transport.close()
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(self.retry_delay)
        raise PolicyTimeout("could not reach the policy server", attempts=self.max_attempts,
                            error=repr(last))

    async def infer(self, observation: Mapping[str, Any]) -> ActionChunk:
        if not self.connected:
            raise PolicyError("not connected; call connect() first")
        self._seq += 1
        try:
            await asyncio.wait_for(self.transport.send(mp.packb(dict(observation))),
                                   self.infer_timeout)
            reply = await asyncio.wait_for(self.transport.recv(), self.infer_timeout)
        except TimeoutError as exc:
            await self._discard()
            raise PolicyTimeout("policy server did not answer in time",
                                timeout_s=self.infer_timeout) from exc
        except (ConnectionError, OSError) as exc:
            await self._discard()
            raise PolicyError("policy connection lost", error=repr(exc)) from exc
        if isinstance(reply, str):
            await self._discard()
            raise PolicyServerError("policy server error", traceback=reply[-2000:])
        decoded = mp.unpackb(reply)
        if not isinstance(decoded, dict):
            raise PolicyError("action reply is not a map")
        out = {_key(k): v for k, v in decoded.items()}
        if "actions" not in out:
            raise PolicyError("action reply has no 'actions'", keys=sorted(out))
        timing = out.pop("server_timing", {})
        return ActionChunk(actions=out.pop("actions"), received_at=self.clock.now(),
                           request_seq=self._seq,
                           server_timing=timing if isinstance(timing, dict) else {},
                           extra=out)

    def queue(self, chunk: ActionChunk, execution_steps: int | None = None) -> ActionQueue:
        return ActionQueue(chunk, max_age_s=self.max_chunk_age, execution_steps=execution_steps,
                           clock=self.clock)

    async def _discard(self) -> None:
        self.connected = False
        with contextlib.suppress(Exception):
            await self.transport.close()

    async def close(self) -> None:
        await self._discard()


def _key(k: Any) -> Any:
    return k.decode("utf-8") if isinstance(k, bytes) else k


class WebsocketsTransport:  # pragma: no cover - needs the websockets package and a server
    """``websockets`` (BSD-3-Clause) binding, extra ``[openpi]``."""

    def __init__(self, uri: str, api_key: str | None = None) -> None:
        self.uri = uri if uri.startswith("ws") else f"ws://{uri}"
        self.api_key = api_key
        self._ws: Any = None

    async def connect(self) -> None:
        try:
            from websockets.asyncio.client import connect  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:
            raise UnsupportedOperation("websockets is not installed (extra [openpi])",
                                       error=str(exc)) from exc
        headers = {"Authorization": f"Api-Key {self.api_key}"} if self.api_key else None
        self._ws = await connect(self.uri, compression=None, max_size=None,
                                 additional_headers=headers)

    async def send(self, data: bytes) -> None:
        await self._ws.send(data)

    async def recv(self) -> bytes | str:
        msg = await self._ws.recv()
        return msg if isinstance(msg, bytes | str) else bytes(msg)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


__all__ = ["ActionChunk", "ActionQueue", "OpenPiPolicyClient", "PolicyError",
           "PolicyServerError", "PolicyTimeout", "StaleActionChunk", "WebsocketsTransport",
           "WsTransport"]
