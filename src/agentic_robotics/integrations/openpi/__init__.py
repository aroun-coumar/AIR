"""openpi policy client (websocket + msgpack) with timeouts and a staleness bound.
Optional; ``websockets`` is imported only by :class:`WebsocketsTransport` (extra
``[openpi]``). A policy client is not a robot adapter: an adapter's policy skill uses it,
and AIR admission and verification still apply to everything the robot does."""

from .client import (
    ActionChunk,
    ActionQueue,
    OpenPiPolicyClient,
    PolicyError,
    PolicyServerError,
    PolicyTimeout,
    StaleActionChunk,
    WebsocketsTransport,
    WsTransport,
)
from .fake_server import FakeOpenPiServer, FakeWsTransport
from .msgpack_codec import CodecError, NdArray, packb, unpackb

__requirements__ = ("REQ11",)

__all__ = ["ActionChunk", "ActionQueue", "CodecError", "FakeOpenPiServer", "FakeWsTransport",
           "NdArray", "OpenPiPolicyClient", "PolicyError", "PolicyServerError", "PolicyTimeout",
           "StaleActionChunk", "WebsocketsTransport", "WsTransport", "packb", "unpackb"]
