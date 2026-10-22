"""AgileX Piper bridge against a fake CAN layer in SDK units."""

from __future__ import annotations

import asyncio

import pytest

from agentic_robotics import CommandState
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import ProfileStatus
from agentic_robotics.integrations.piper import (
    FakePiperCan,
    PiperAdapter,
    piper_skill_set,
    read_interface_state,
)

from .bridge_env import make_env, manifest

pytestmark = pytest.mark.req("REQ03", "REQ05", "REQ13")

HOME = {"j1": 0.0, "j2": 0.5, "j3": -0.5, "j4": 0.0, "j5": 0.3, "j6": 0.0}


async def _env(operstate: str = "up", **config: object):  # type: ignore[no-untyped-def]
    m = manifest("agilex_piper", "piper-1", ("manipulation",), **config)
    fake = FakePiperCan(operstate=operstate)
    adapter = PiperAdapter(m, m.adapter.config)
    adapter.use_can(fake)
    env = await make_env(adapter, piper_skill_set().skills)
    return env, adapter, fake


async def test_conformance() -> None:
    env, _, _ = await _env()
    try:
        report = await check_adapter(env.runtime.robot("piper-1"), env.runtime.skills,
                                     env.runtime.clock)
        assert report.passed, report.summary()
    finally:
        await env.runtime.stop()


async def test_can_down_is_a_health_fact_and_a_precondition() -> None:
    env, adapter, fake = await _env(operstate="down")
    try:
        prof = adapter.capability_report().get("manipulation")
        assert prof.status is ProfileStatus.UNAVAILABLE
        assert any("can_activate.sh" in f for f in prof.limiting_factors)
        r = await env.run("enable_arm")
        assert r.state is CommandState.REJECTED
        assert fake.log == []          # AIR never touched the bus or the interface
    finally:
        await env.runtime.stop()


async def test_bitrate_mismatch_is_unavailable() -> None:
    env, adapter, _fake = await _env(expected_bitrate=500000)
    try:
        prof = adapter.capability_report().get("manipulation")
        assert "can_bitrate_mismatch" in prof.limiting_factors
    finally:
        await env.runtime.stop()


async def test_enable_then_move_is_verified_from_joint_feedback() -> None:
    env, adapter, fake = await _env()
    try:
        assert adapter.capability_report().get("manipulation").status is ProfileStatus.RESTRICTED
        assert (await env.run("enable_arm")).verified
        r = await env.run("move_joints", **HOME)
        assert r.state is CommandState.VERIFIED, r
        assert ("motion_ctrl_2", (0x01, 0x01, 30)) in fake.log
        assert (await env.run("set_gripper", opening_m=0.03)).verified
    finally:
        await env.runtime.stop()


async def test_out_of_range_target_is_clamped_and_not_verified() -> None:
    env, _, _fake = await _env()
    try:
        assert (await env.run("enable_arm")).verified
        r = await env.run("move_joints", **{**HOME, "j3": 0.5})   # j3 limit is [-2.967, 0]
        assert r.state is CommandState.FAILED and r.reason == "postcondition_failed"
        assert r.observed_effects["applied"]["j3"] == 0.0
        assert r.observed_effects["limited_by"] == ["joint_limit:j3"]
    finally:
        await env.runtime.stop()


async def test_collision_fails_and_stale_feedback_is_unresolved() -> None:
    env, _, fake = await _env()
    try:
        assert (await env.run("enable_arm")).verified
        fake.collide()
        r = await env.run("move_joints", **{**HOME, "j1": 1.0})
        assert r.state is CommandState.REJECTED          # arm.status precondition
        fake.status = 0
        h = env.runtime.submit(env.command("move_joints", **{**HOME, "j1": 2.5}))
        await asyncio.sleep(0.02)
        fake.set_interface("down")
        r = await h.wait(5)
        assert r.state in (CommandState.UNRESOLVED, CommandState.FAILED)
        if r.state is CommandState.FAILED:
            assert r.reason == "can_interface_down"
    finally:
        await env.runtime.stop()


async def test_cancel_is_a_quick_stop_confirmed_by_status() -> None:
    env, _, fake = await _env()
    try:
        assert (await env.run("enable_arm")).verified
        fake.step = 50
        h = env.runtime.submit(env.command("move_joints", **{**HOME, "j1": 2.5}))
        await asyncio.sleep(0.03)
        r = await h.cancel()
        assert r.state is CommandState.CANCELLED
        assert r.observed_effects["stopped_confirmed"] is True
        assert ("emergency_stop", 0x01) in fake.log
        assert (await env.run("reset_estop")).verified
    finally:
        await env.runtime.stop()


def test_sysfs_reader_reports_absent_interface() -> None:
    assert read_interface_state("definitely_not_a_can_if")["operstate"] == "absent"
