"""Microduck bridge against a fake robotd speaking the verified duck-ipc-proto subset."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentic_robotics import Command, CommandState, Delegation, Robot, Runtime, SkillRegistry
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import AdapterRef, ComponentManifest, ProfileStatus
from agentic_robotics.core import new_id
from agentic_robotics.integrations.microduck import (
    FakeRobotd,
    MicroduckAdapter,
    memory_pair,
    microduck_skill_set,
)
from agentic_robotics.integrations.microduck import protocol as p

pytestmark = pytest.mark.req("G02", "REQ03", "REQ05")

ROOT = "operator:root"
AGENT = "agent:duck"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "robots" / "microduck.manifest.json"


def _manifest(**config: object) -> ComponentManifest:
    cfg: dict[str, object] = {"refresh_s": 0.005, "state_timeout_s": 0.5, "max_speed_mps": 0.3}
    cfg.update(config)
    return ComponentManifest(robot_id="duck-1", name="duck", hardware_revision="t",
                             adapter=AdapterRef(kind="microduck", config=cfg),
                             nominal_capabilities=("locomotion", "head", "control_loop"))


async def _env(sim_dt_s: float = 0.02, **config: object
               ) -> tuple[Runtime, MicroduckAdapter, FakeRobotd, Delegation]:
    fake = FakeRobotd(sim_dt_s=sim_dt_s, tick_s=0.001)
    ours, theirs = memory_pair()
    fake.serve(theirs)
    adapter = MicroduckAdapter(_manifest(**config), _manifest(**config).adapter.config)
    adapter.use_transport(ours)
    rt = Runtime(SkillRegistry(microduck_skill_set().skills), root_principals=[ROOT])
    rt.register(Robot(adapter.manifest, adapter))
    await rt.start()
    d = rt.authority.grant(Delegation(delegation_id="dlg_duck", grantor=ROOT, grantee=AGENT,
                                      robots=("duck-1",), valid_from=rt.clock.now()))
    assert await _until(lambda: adapter.state is not None)
    return rt, adapter, fake, d


async def _until(pred: object, timeout: float = 3.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():  # type: ignore[operator]
            return True
        await asyncio.sleep(0.005)
    return False


def _cmd(rt: Runtime, d: Delegation, skill_id: str, **args: object) -> Command:
    return Command(command_id=new_id("cmd"), robot_id="duck-1", skill_id=skill_id, issued_by=AGENT,
                   delegation_id=d.delegation_id, authority_revision=rt.authority.revision,
                   args=dict(args))


def test_wire_format_matches_duck_ipc_proto() -> None:
    line = p.request(1, p.Method.HELLO, {"api_version": p.API_VERSION})
    assert line.endswith("\n") and "\n" not in line[:-1]
    assert json.loads(line) == {"jsonrpc": "2.0", "id": 1, "method": "hello",
                                "params": {"api_version": 44}}
    assert json.loads(p.request(2, p.Method.STOP)) ["params"] == {}
    note = json.loads(p.notification(p.Method.MOVE, {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}))
    assert "id" not in note
    with pytest.raises(p.ProtocolError):
        p.request(3, p.Method.MOVE, {"vx": 0.1})
    with pytest.raises(p.ProtocolError):
        p.decode('{"jsonrpc":"1.0"}')


async def test_conformance_against_fake_robotd() -> None:
    rt, adapter, fake, _ = await _env()
    try:
        report = await check_adapter(rt.robot("duck-1"), rt.skills, rt.clock)
        assert report.passed, report.summary()
        assert fake.received[0]["method"] == "hello"
        assert adapter.client is not None and adapter.client.hello["api_version"] == 44
    finally:
        await rt.stop()
        await fake.close()


async def test_walk_to_is_verified_from_odometry_and_intents_are_notifications() -> None:
    rt, _adapter, fake, d = await _env()
    try:
        stand = rt.submit(_cmd(rt, d, "stand_up"))
        assert (await stand.wait(5)).state is CommandState.VERIFIED
        walk = rt.submit(_cmd(rt, d, "walk_to", x=0.3, y=0.2))
        result = await walk.wait(20)
        assert result.state is CommandState.VERIFIED, result
        moves = [m for m in fake.received if m["method"] == "robot.move"]
        assert moves and all("id" not in m for m in moves)
        assert set(moves[0]["params"]) == {"vx", "vy", "vyaw"}
        assert fake.received[-1]["method"] in ("robot.stop", "robot.health")
    finally:
        await rt.stop()
        await fake.close()


async def test_walk_without_enabled_policy_is_rejected_at_admission() -> None:
    rt, _, fake, d = await _env()
    try:
        h = rt.submit(_cmd(rt, d, "walk_to", x=0.3, y=0.0))
        r = await h.wait(2)
        assert r.state is CommandState.REJECTED and r.reason == "precondition_failed"
    finally:
        await rt.stop()
        await fake.close()


async def test_fall_restricts_capability_and_fails_the_walk() -> None:
    rt, adapter, fake, d = await _env(sim_dt_s=0.002)
    try:
        assert (await rt.submit(_cmd(rt, d, "stand_up")).wait(5)).verified
        walk = rt.submit(_cmd(rt, d, "walk_to", x=8.0, y=0.0))
        assert await _until(lambda: fake.x > 0.02)
        fake.fall()
        r = await walk.wait(10)
        assert r.state is CommandState.FAILED and r.reason == "fallen"
        assert await _until(lambda: adapter.capability_report().get("locomotion").status
                            is ProfileStatus.RESTRICTED)
        prof = adapter.capability_report().get("locomotion")
        assert "fallen" in prof.limiting_factors and "joint_limits:none" in prof.evidence
        refused = await rt.submit(_cmd(rt, d, "stand_up")).wait(5)
        assert refused.state is CommandState.REJECTED
    finally:
        await rt.stop()
        await fake.close()


async def test_refusal_is_a_failure_with_robotd_reason() -> None:
    rt, _, fake, d = await _env()
    try:
        fake.refuse_enable("battery critical")
        r = await rt.submit(_cmd(rt, d, "stand_up")).wait(5)
        assert r.state is CommandState.FAILED and r.reason == "refused_by_robot"
        assert "battery critical" in r.message
    finally:
        await rt.stop()
        await fake.close()


async def test_lost_link_mid_walk_is_unresolved_not_failed() -> None:
    rt, _, fake, d = await _env(sim_dt_s=0.002)
    try:
        assert (await rt.submit(_cmd(rt, d, "stand_up")).wait(5)).verified
        walk = rt.submit(_cmd(rt, d, "walk_to", x=8.0, y=0.0))
        assert await _until(lambda: fake.x > 0.02)
        await fake.drop_link()
        r = await walk.wait(10)
        assert r.state is CommandState.UNRESOLVED
    finally:
        await rt.stop()
        await fake.close()


async def test_do_skill_is_never_claimed_verified() -> None:
    rt, _, fake, d = await _env()
    try:
        assert (await rt.submit(_cmd(rt, d, "stand_up")).wait(5)).verified
        r = await rt.submit(_cmd(rt, d, "do_skill", skill="sitstand")).wait(5)
        assert r.state is CommandState.REPORTED_COMPLETE
        bad = await rt.submit(_cmd(rt, d, "do_skill", skill="moonwalk")).wait(5)
        assert bad.state is CommandState.FAILED
    finally:
        await rt.stop()
        await fake.close()


async def test_cancel_sends_stop_and_confirms_from_state() -> None:
    rt, _, fake, d = await _env(sim_dt_s=0.002)
    try:
        assert (await rt.submit(_cmd(rt, d, "stand_up")).wait(5)).verified
        walk = rt.submit(_cmd(rt, d, "walk_to", x=8.0, y=0.0))
        assert await _until(lambda: fake.x > 0.02)
        r = await walk.cancel()
        assert r.state is CommandState.CANCELLED
        assert r.observed_effects.get("stopped_confirmed") is True
    finally:
        await rt.stop()
        await fake.close()


def test_refresh_must_beat_the_deadman() -> None:
    m = _manifest(refresh_s=0.6)
    with pytest.raises(ValueError):
        MicroduckAdapter(m, m.adapter.config)


@pytest.mark.parametrize("name,kind", [("microduck", "microduck"),
                                       ("unitree", "unitree_sport"),
                                       ("piper", "agilex_piper"), ("vda5050", "vda5050")])
def test_example_manifests_use_catalog_types(name: str, kind: str) -> None:
    from agentic_robotics.hardware import builtin_catalog, check_manifest
    from agentic_robotics.hardware.validate import Level

    man = ComponentManifest.load(EXAMPLE.with_name(f"{name}.manifest.json"))
    assert man.adapter.kind == kind
    findings = check_manifest(man, builtin_catalog())
    assert not [f for f in findings if f.level is Level.ERROR], findings
    assert all(c.type_id for c in man.components)
