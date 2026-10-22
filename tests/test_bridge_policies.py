"""openpi and LeRobot policy clients: verified wire format, timeouts, staleness bounds."""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import pytest

from agentic_robotics.core import AdmissionRejected, ManualClock, SchemaError
from agentic_robotics.integrations.lerobot import (
    ExecutionMode,
    FeatureSpec,
    LeRobotPolicyClient,
    PolicyUnavailable,
    RobotInterface,
    StaleChunk,
    compatibility_problems,
)
from agentic_robotics.integrations.lerobot import PolicyCapabilities as Caps
from agentic_robotics.integrations.openpi import (
    CodecError,
    FakeOpenPiServer,
    NdArray,
    OpenPiPolicyClient,
    PolicyServerError,
    PolicyTimeout,
    StaleActionChunk,
    packb,
    unpackb,
)

pytestmark = pytest.mark.req("REQ11", "REQ12")


# -- msgpack codec ---------------------------------------------------------------------
def test_codec_round_trips_and_follows_openpi_ndarray_convention() -> None:
    arr = NdArray.from_list([[1.0, 2.0], [3.0, 4.0]], "<f4")
    obj = {"state": arr, "prompt": "pick", "n": -5, "big": 2**40, "x": 1.5, "ok": True,
           "none": None, "raw": b"\x00\x01", "list": [1, "a", [2.0]]}
    back = unpackb(packb(obj))
    assert back["state"].shape == (2, 2) and back["state"].tolist() == [[1.0, 2.0], [3.0, 4.0]]
    assert {k: v for k, v in back.items() if k != "state"} == {
        k: v for k, v in obj.items() if k != "state"}
    raw = unpackb(packb({b"__ndarray__": False, b"dtype": "<f4"}))
    assert raw == {b"__ndarray__": False, b"dtype": "<f4"}
    with pytest.raises(CodecError):
        unpackb(packb([1, 2])[:-1])
    with pytest.raises(CodecError):
        NdArray("|O", (1,), b"x")                 # object dtype: never (no pickle)


@pytest.mark.skipif(importlib.util.find_spec("msgpack") is None
                    or importlib.util.find_spec("numpy") is None,
                    reason="msgpack/numpy not installed")
def test_codec_interoperates_with_msgpack_numpy_convention() -> None:  # pragma: no cover
    import msgpack
    import numpy as np

    def pack_array(o: Any) -> Any:
        return {b"__ndarray__": True, b"data": o.tobytes(), b"dtype": o.dtype.str,
                b"shape": o.shape} if isinstance(o, np.ndarray) else o

    wire = msgpack.packb({"a": np.arange(6, dtype=np.float32).reshape(2, 3)},
                         default=pack_array)
    assert unpackb(wire)["a"].tolist() == [[0, 1, 2], [3, 4, 5]]


# -- openpi client ---------------------------------------------------------------------
async def test_metadata_handshake_then_inference() -> None:
    server = FakeOpenPiServer({"action_horizon": 4, "model": "pi0-fake"})
    client = OpenPiPolicyClient(server.transport(), infer_timeout_s=1.0)
    meta = await client.connect()
    assert meta["model"] == "pi0-fake"
    chunk = await client.infer({"observation/state": NdArray.from_list([0.0, 1.0]),
                                "prompt": "pick up the cube"})
    assert chunk.server_timing["infer_ms"] == 1.5
    assert chunk.actions.shape == (4, 2)
    q = client.queue(chunk, execution_steps=2)
    assert len(q) == 2 and q.next() == pytest.approx([0.1, 0.1])
    assert server.observations[0]["prompt"] == "pick up the cube"


async def test_connect_is_bounded_not_forever() -> None:
    server = FakeOpenPiServer()
    server.refuse_connections = 10
    client = OpenPiPolicyClient(server.transport(), max_connect_attempts=3, retry_delay_s=0.0)
    with pytest.raises(PolicyTimeout):
        await client.connect()
    assert server.refuse_connections == 7


async def test_timeout_discards_the_connection() -> None:
    server = FakeOpenPiServer()
    client = OpenPiPolicyClient(server.transport(), infer_timeout_s=0.05)
    await client.connect()
    server.stall = True
    with pytest.raises(PolicyTimeout):
        await client.infer({"prompt": "x"})
    assert not client.connected
    with pytest.raises(Exception, match="not connected"):
        await client.infer({"prompt": "x"})


async def test_text_frame_is_a_server_error() -> None:
    server = FakeOpenPiServer()
    client = OpenPiPolicyClient(server.transport())
    await client.connect()
    server.fail_next = True
    with pytest.raises(PolicyServerError):
        await client.infer({"prompt": "x"})
    assert not client.connected


async def test_stale_chunk_is_refused_not_replayed() -> None:
    clock = ManualClock()
    server = FakeOpenPiServer()
    client = OpenPiPolicyClient(server.transport(), max_chunk_age_s=0.2, clock=clock)
    await client.connect()
    q = client.queue(await client.infer({"prompt": "x"}))
    q.next()
    clock.advance(0.3)
    with pytest.raises(StaleActionChunk):
        q.next()
    assert len(q) == 0


# -- LeRobot contracts -----------------------------------------------------------------
STATE = FeatureSpec("observation.state", (6,), "float32", semantics="joint positions rad")
CAM = FeatureSpec("observation.images.front", (480, 640, 3), "uint8", kind="rgb",
                  semantics="front camera HWC")
ACT = FeatureSpec("action", (6,), "float32", semantics="joint position targets rad")


def _caps(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "modes": ["chunk"], "prediction_steps": 50, "execution_steps": 25,
        "action_interval": 0.02,
        "features": [{"name": f.name, "shape": list(f.shape), "dtype": f.dtype, "kind": f.kind,
                      "semantics": f.semantics} for f in (STATE, CAM)],
        "action_feature": {"name": "action", "shape": [6], "dtype": "float32",
                           "semantics": "joint position targets rad"},
        "artifact_identity": "sha256:abc"}
    d.update(kw)
    return d


def test_feature_and_capability_validation_mirror_upstream() -> None:
    with pytest.raises(SchemaError):
        FeatureSpec("x", (2,), "float32")                       # semantics required
    with pytest.raises(SchemaError):
        FeatureSpec("img", (480, 640, 4), "uint8", kind="rgb", semantics="s")
    with pytest.raises(SchemaError):
        Caps.from_dict(_caps(execution_steps=60))
    caps = Caps.from_dict(_caps())
    assert caps.horizon_s == pytest.approx(0.5)
    robot = RobotInterface((STATE, CAM), ACT, 0.02)
    assert compatibility_problems(caps, robot) == []
    bad = RobotInterface((CAM, STATE), ACT, 0.05, mode=ExecutionMode.RTC_GUIDED,
                         expected_artifact="sha256:other")
    problems = compatibility_problems(caps, bad)
    assert len(problems) == 4


class _Transport:
    def __init__(self, caps: dict[str, Any], delay: float = 0.0) -> None:
        self.caps, self.delay = caps, delay

    async def request(self, kind: str, payload: Any) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        if kind == "describe":
            return self.caps
        return {"request_id": payload["request_id"], "actions": [[0.0] * 6] * 25,
                "artifact_identity": "sha256:abc"}


async def test_lerobot_client_admits_before_motion_and_bounds_staleness() -> None:
    clock = ManualClock()
    client = LeRobotPolicyClient(_Transport(_caps()), clock=clock)
    with pytest.raises(AdmissionRejected):
        await client.infer({}, task="t", capture_time=clock.now())
    with pytest.raises(AdmissionRejected):
        await client.admit(RobotInterface((STATE,), ACT, 0.02))
    await client.admit(RobotInterface((STATE, CAM), ACT, 0.02))
    chunk = await client.infer({"observation.state": [0.0] * 6}, task="t",
                               capture_time=clock.now())
    assert client.usable_steps(chunk) == 25
    clock.advance(0.3)
    assert client.usable_steps(chunk) == 10
    clock.advance(0.3)
    with pytest.raises(StaleChunk):
        client.usable_steps(chunk)
    with pytest.raises(StaleChunk):
        await client.infer({}, task="t", capture_time=clock.now() - 10)


async def test_lerobot_client_times_out() -> None:
    client = LeRobotPolicyClient(_Transport(_caps(), delay=0.2), describe_timeout_s=0.05)
    with pytest.raises(PolicyUnavailable):
        await client.admit(RobotInterface((STATE, CAM), ACT, 0.02))
