"""Unitree bridge against a fake DDS graph behind the injected channel layer."""

from __future__ import annotations

import sys

import pytest

from agentic_robotics import CommandState
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import ProfileStatus
from agentic_robotics.integrations.unitree import (
    FakeUnitree,
    UnitreeAdapter,
    api,
    unitree_skill_set,
)

from .bridge_env import make_env, manifest, until

pytestmark = pytest.mark.req("REQ03", "REQ05", "REQ12")


async def _env(**config: object):  # type: ignore[no-untyped-def]
    cfg: dict[str, object] = {"move_refresh_s": 0.005, "state_timeout_s": 0.5,
                              "posture_timeout_s": 1.0}
    cfg.update(config)
    m = manifest("unitree_sport", "go2-1", ("locomotion", "low_level"), **cfg)
    fake = FakeUnitree(dt_s=0.01, tick_s=0.001)
    adapter = UnitreeAdapter(m, m.adapter.config)
    adapter.use_channels(fake)
    env = await make_env(adapter, unitree_skill_set().skills)
    assert await until(lambda: adapter.sport is not None and adapter.low is not None)
    return env, adapter, fake


def test_no_dds_import() -> None:
    assert "cyclonedds" not in sys.modules and "unitree_sdk2py" not in sys.modules


async def test_conformance_and_rpc_ids() -> None:
    env, _adapter, fake = await _env()
    try:
        report = await check_adapter(env.runtime.robot("go2-1"), env.runtime.skills,
                                     env.runtime.clock)
        assert report.passed, report.summary()
        assert fake.calls[0][:2] == (api.MOTION_SWITCHER_SERVICE, api.MotionSwitcher.CHECK_MODE)
    finally:
        await env.runtime.stop()


async def test_stand_then_walk_is_verified_and_move_is_fire_and_forget() -> None:
    env, _, fake = await _env()
    try:
        assert (await env.run("stand_up")).state is CommandState.VERIFIED
        r = await env.run("walk_to", timeout=20, x=0.6, y=0.3)
        assert r.state is CommandState.VERIFIED, r
        moves = [c for c in fake.calls if c[1] == api.Sport.MOVE]
        assert moves and all(c[3] is False for c in moves)      # _CallNoReply
        assert set(moves[0][2]) == {"x", "y", "z"}
        assert fake.calls[-1][1] == api.Sport.STOP_MOVE
    finally:
        await env.runtime.stop()


async def test_walk_requires_standing_and_lying_down_verifies() -> None:
    env, _, _ = await _env()
    try:
        r = await env.run("walk_to", x=1.0, y=0.0)
        assert r.state is CommandState.REJECTED and r.reason == "precondition_failed"
        assert (await env.run("stand_up")).verified
        assert (await env.run("stand_down")).verified
    finally:
        await env.runtime.stop()


async def test_rpc_timeout_is_unresolved_never_retried() -> None:
    env, _, fake = await _env()
    try:
        fake.timeout_next_call()
        r = await env.run("stand_up")
        assert r.state is CommandState.UNRESOLVED
        assert sum(1 for c in fake.calls if c[1] == api.Sport.STAND_UP) == 1
    finally:
        await env.runtime.stop()


async def test_stale_sport_state_during_walk_is_unresolved() -> None:
    env, _, fake = await _env()
    try:
        assert (await env.run("stand_up")).verified
        h = env.runtime.submit(env.command("walk_to", x=15.0, y=0.0))
        assert await until(lambda: fake.x > 0.01)
        fake.silence()
        r = await h.wait(10)
        assert r.state is CommandState.UNRESOLVED
    finally:
        await env.runtime.stop()


async def test_low_level_is_gated_by_capability_config_and_freshness() -> None:
    env, adapter, fake = await _env()
    try:
        r = await env.run("set_joint_position", joint="FR_1", q=0.5)
        assert r.state is CommandState.REJECTED and r.reason == "capability_unavailable"
        low = adapter.capability_report().get("low_level")
        assert {"disabled_by_config", "no_joint_limits_configured",
                "sport_mode_active"} <= set(low.limiting_factors)
        assert fake.lowcmds == 0
    finally:
        await env.runtime.stop()


async def test_low_level_after_release_clamps_and_verifies() -> None:
    env, adapter, fake = await _env(low_level=True, joint_limits={"FR_1": [-1.0, 0.4]},
                                    low_state_max_age_s=0.2)
    try:
        assert (await env.run("release_sport_mode")).verified
        assert await until(lambda: adapter.capability_report().get("low_level").status
                           is ProfileStatus.AVAILABLE)
        ok = await env.run("set_joint_position", joint="FR_1", q=0.3, kp=40.0, duration_s=0.05)
        assert ok.state is CommandState.VERIFIED, ok
        assert fake.lowcmds > 0
        clamped = await env.run("set_joint_position", joint="FR_1", q=0.9, kp=40.0,
                                duration_s=0.05)
        assert clamped.state is CommandState.FAILED and clamped.reason == "postcondition_failed"
        assert clamped.observed_effects["applied"] == 0.4
        assert clamped.observed_effects["limited_by"] == ["joint_limit"]
        assert (await env.run("walk_to", x=1.0, y=0.0)).state is CommandState.REJECTED
    finally:
        await env.runtime.stop()


async def test_cancel_walk_confirms_stop_from_state() -> None:
    env, _, fake = await _env()
    try:
        assert (await env.run("stand_up")).verified
        h = env.runtime.submit(env.command("walk_to", x=15.0, y=0.0))
        assert await until(lambda: fake.x > 0.01)
        r = await h.cancel()
        assert r.state is CommandState.CANCELLED
        assert r.observed_effects["stopped_confirmed"] is True
    finally:
        await env.runtime.stop()


def test_adapter_registers_explicitly() -> None:
    from agentic_robotics.integrations import unitree
    from agentic_robotics.plugins import adapter_class

    unitree.register()
    assert adapter_class("unitree_sport") is UnitreeAdapter
