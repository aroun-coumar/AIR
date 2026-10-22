"""VDA 5050 bridge: messages checked against the published v2/v3 JSON schemas (vendored
under tests/data/vda5050, MIT), and the bridge against a fake vehicle."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from agentic_robotics import CommandState
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import ProfileStatus
from agentic_robotics.core import SchemaError
from agentic_robotics.integrations.vda5050 import FakeVehicle, VdaAdapter, messages, vda_skill_set

from .bridge_env import make_env, manifest, until

pytestmark = pytest.mark.req("REQ03", "REQ06", "REQ09", "REQ17")

DATA = Path(__file__).parent / "data" / "vda5050"
VERSIONS = {"v2": "2.1.0", "v3": "3.0.0"}


def _schema(major: str, name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((DATA / major / f"{name}.schema").read_text("utf-8"))


def _validate(major: str, name: str, msg: dict) -> None:  # type: ignore[type-arg]
    schema = _schema(major, name)
    jsonschema.validators.validator_for(schema)(schema).validate(msg)


@pytest.mark.parametrize("major", ["v2", "v3"])
def test_order_and_instant_actions_match_the_published_schemas(major: str) -> None:
    d = messages.dialect(VERSIONS[major])
    h = messages.Headers(d, "ExampleCo", "AGV0001")
    assert h.topic("order") == ("uagv/v2/ExampleCo/AGV0001/order" if major == "v2"
                                else "vda5050/v3/ExampleCo/AGV0001/order")
    nodes = [messages.node("n0", 0, released=True, x=0.0, y=0.0, map_id="floor1"),
             messages.node("n1", 2, released=True, x=1.0, y=0.0, map_id="floor1"),
             messages.node("n2", 4, released=False, x=2.0, y=0.0, map_id="floor1")]
    edges = [messages.edge(d, "e01", 1, released=True, start="n0", end="n1", max_speed=1.0),
             messages.edge(d, "e12", 3, released=False, start="n1", end="n2")]
    order = messages.order(h, "o1", 0, nodes, edges)
    _validate(major, "order", order)
    ia = messages.instant_actions(h, [messages.action("cancelOrder", "a1", "NONE",
                                                      {"orderId": "o1"}, d=d)])
    _validate(major, "instantActions", ia)
    assert messages.order(h, "o1", 1, nodes, edges)["headerId"] == 1      # per-topic counter
    assert ia["headerId"] == 0


def test_dialect_rules() -> None:
    v2 = messages.dialect("2.1.0")
    with pytest.raises(SchemaError):
        messages.action("x", "a", "SINGLE", d=v2)          # SINGLE is v3-only
    assert messages.dialect("3.0.0").broken == "CONNECTION_BROKEN"
    assert v2.broken == "CONNECTIONBROKEN"
    h = messages.Headers(v2, "Bad/Name", "1")
    with pytest.raises(SchemaError):
        h.topic("order")
    d = messages.dialect("3.0.0")
    h = messages.Headers(d, "M", "S")
    horizon_first = [messages.node("a", 0, released=False, x=0, y=0, map_id="m")]
    with pytest.raises(SchemaError):
        messages.order(h, "o", 0, horizon_first, [])
    with pytest.raises(SchemaError):
        messages.dialect("1.1.0")
    with pytest.raises(SchemaError):
        messages.instant_actions(h, [messages.action("cancelOrder", "a", "HARD", d=d)])


@pytest.mark.parametrize("major", ["v2", "v3"])
def test_fake_vehicle_state_matches_the_published_schema(major: str) -> None:
    v = FakeVehicle(messages.dialect(VERSIONS[major]), "ExampleCo", "AGV0001")
    _validate(major, "state", v.state())


async def _env(major: str = "v3", **config: object):  # type: ignore[no-untyped-def]
    cfg: dict[str, object] = {"version": VERSIONS[major], "manufacturer": "ExampleCo",
                              "serial_number": "AGV0001", "map_id": "floor1",
                              "state_timeout_s": 1.0}
    cfg.update(config)
    m = manifest("vda5050", "agv-1", ("mobility",), **cfg)
    adapter = VdaAdapter(m, m.adapter.config)
    vehicle = FakeVehicle(adapter.d, "ExampleCo", "AGV0001")
    adapter.use_publisher(vehicle)
    env = await make_env(adapter, vda_skill_set().skills)
    vehicle.attach(adapter.on_message)
    assert await until(lambda: adapter.state is not None)
    return env, adapter, vehicle


@pytest.mark.parametrize("major", ["v2", "v3"])
async def test_drive_to_node_is_verified_from_state(major: str) -> None:
    env, adapter, vehicle = await _env(major)
    try:
        report = await check_adapter(env.runtime.robot("agv-1"), env.runtime.skills,
                                     env.runtime.clock)
        assert report.passed, report.summary()
        r = await env.run("drive_to_node", node_id="dock", x=0.5, y=0.2)
        assert r.state is CommandState.VERIFIED, r
        sent = [msg for name, msg in vehicle.received if name == "order"]
        assert len(sent) == 1
        _validate(major, "order", sent[0])
        assert [n["nodeId"] for n in sent[0]["nodes"]] == ["home", "dock"]
        assert adapter.observe()["battery.charge"].value == 80.0
    finally:
        await env.runtime.stop()
        await vehicle.close()


async def test_order_error_is_a_failure_with_the_vehicle_reason() -> None:
    env, _, vehicle = await _env()
    try:
        vehicle.reject_next_order("orderUpdateError")
        r = await env.run("drive_to_node", node_id="dock", x=0.5, y=0.0)
        assert r.state is CommandState.FAILED and r.reason == "order_error:orderUpdateError"
    finally:
        await env.runtime.stop()
        await vehicle.close()


async def test_connection_broken_mid_order_is_unresolved() -> None:
    env, adapter, vehicle = await _env(major="v2")
    try:
        h = env.runtime.submit(env.command("drive_to_node", node_id="far", x=50.0, y=0.0))
        assert await until(lambda: vehicle.x > 0.05)
        vehicle.break_connection()
        r = await h.wait(5)
        assert r.state is CommandState.UNRESOLVED
        assert adapter.capability_report().get("mobility").status is ProfileStatus.UNAVAILABLE
    finally:
        await env.runtime.stop()
        await vehicle.close()


async def test_cancel_order_confirmed_when_cancel_finishes() -> None:
    env, _, vehicle = await _env()
    try:
        h = env.runtime.submit(env.command("drive_to_node", node_id="far", x=50.0, y=0.0))
        assert await until(lambda: vehicle.x > 0.05)
        r = await h.cancel()
        assert r.state is CommandState.CANCELLED
        assert r.observed_effects["stopped_confirmed"] is True
        cancels = [a for name, msg in vehicle.received if name == "instantActions"
                   for a in msg["actions"] if a["actionType"] == "cancelOrder"]
        assert cancels and cancels[0]["actionParameters"][0]["key"] == "orderId"
    finally:
        await env.runtime.stop()
        await vehicle.close()


async def test_pause_resume_and_operating_mode_gate() -> None:
    env, adapter, vehicle = await _env()
    try:
        assert (await env.run("pause")).verified
        r = await env.run("drive_to_node", node_id="dock", x=0.5, y=0.0)
        assert r.state is CommandState.REJECTED                  # paused precondition
        assert (await env.run("resume")).verified
        vehicle.set_operating_mode("MANUAL")
        assert await until(lambda: adapter.capability_report().get("mobility").status
                           is ProfileStatus.UNAVAILABLE)
        r = await env.run("drive_to_node", node_id="dock", x=0.5, y=0.0)
        assert r.state is CommandState.REJECTED
    finally:
        await env.runtime.stop()
        await vehicle.close()


async def test_messages_for_other_vehicles_are_ignored() -> None:
    env, adapter, vehicle = await _env()
    try:
        adapter.on_message("vda5050/v3/ExampleCo/OTHER/state",
                           json.dumps({**vehicle.state(), "serialNumber": "OTHER"}))
        adapter.on_message("vda5050/v3/ExampleCo/AGV0001/state", b"{not json")
        assert len(adapter.rejected) == 2
    finally:
        await env.runtime.stop()
        await vehicle.close()
