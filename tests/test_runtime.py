"""Stage 2 evidence: one-robot execution path.

Successful and failed task traces; stale-input and revoked-authority rejection;
uncertain outcomes and reconciliation; interruption; fencing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agentic_robotics import (
    AdmissionRejected,
    CommandState,
    Delegation,
    ManualClock,
    OutcomeNotVerified,
    SkillRegistry,
    UnsupportedOperation,
)
from agentic_robotics.contracts import ExecutionContract, Occupancy
from agentic_robotics.core import InvalidTransition, SyncState
from agentic_robotics.integrations.fake import fake_skill_set
from agentic_robotics.runtime import check_transition

from .conftest import AGENT, ROOT, Env

S = CommandState
MakeEnv = Callable[..., Env]

pytestmark = pytest.mark.req("REQ05", "REQ12")
freshness = pytest.mark.req("G05", "REQ12")
authority = pytest.mark.req("G12", "G15", "REQ17")
ownership = pytest.mark.req("G06", "REQ09", "REQ06")
commitments = pytest.mark.req("G07", "REQ07")
uncertain = pytest.mark.req("REQ14", "G07")
capability = pytest.mark.req("REQ13", "G10")


def with_deadline(env: Env, skill_id: str, deadline_s: float) -> None:
    skills = []
    for s in fake_skill_set().skills:
        if s.skill_id == skill_id:
            s = s.evolve(execution=s.execution.evolve(deadline_s=deadline_s))
        skills.append(s)
    env.runtime.skills = SkillRegistry(skills)


@commitments
async def test_successful_navigation_is_verified_with_ordered_trace(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.5}))
    result = await h.wait_for_verified_result(timeout=5)
    assert result.state is S.VERIFIED
    assert all(c.satisfied for c in result.verification)
    kinds = env.runtime.trace.kinds(h.command_id)
    expected = ["submitted", "accepted", "started", "adapter_reported", "verification",
                "verified"]
    assert [k for k in kinds if k in expected] == expected
    assert "progress" in kinds


@pytest.mark.parametrize(("skill", "args", "reason"), [
    ("teleport", {}, "unknown_skill"),
    ("navigate_to", {"x": 1.0}, "invalid_arguments"),
    ("navigate_to", {"x": 1.0, "y": 0.0, "speed": 9}, "invalid_arguments"),
])
async def test_invalid_commands_are_rejected(make_env: MakeEnv, skill: str, args: dict,
                                             reason: str) -> None:
    env = make_env()
    h = env.runtime.submit(env.command(skill, args))
    result = await h.wait()
    assert result.state is S.REJECTED and result.reason == reason
    assert env.adapter.executed == []


async def test_unsupported_skill_fails_explicitly(make_env: MakeEnv) -> None:
    env = make_env(camera=False)
    result = await env.runtime.submit(env.command("capture_image")).wait()
    assert result.reason == "unsupported_skill"


async def test_unknown_robot_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    result = await env.runtime.submit(env.command("navigate_to", {"x": 0, "y": 0},
                                                  robot_id="ghost")).wait()
    assert result.reason == "unknown_robot"


@freshness
async def test_stale_input_is_rejected(make_env: MakeEnv) -> None:
    clock = ManualClock()
    env = make_env(clock=clock)
    env.adapter.inject("freeze_sensors")
    clock.advance(5.0)
    result = await env.runtime.submit(env.command("navigate_to", {"x": 1, "y": 0})).wait()
    assert result.state is S.REJECTED
    assert result.reason == "stale_input"
    assert result.observed_effects["details"]["age_s"] == 5.0


@freshness
async def test_inadequate_clock_sync_is_rejected(make_env: MakeEnv) -> None:
    env = make_env(clock=ManualClock(sync_state=SyncState.UNSYNCHRONIZED))
    result = await env.runtime.submit(env.command("navigate_to", {"x": 1, "y": 0})).wait()
    assert result.reason == "clock_quality"


@authority
async def test_revoked_authority_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    env.runtime.authority.revoke(env.delegation.delegation_id)
    result = await env.runtime.submit(env.command("navigate_to", {"x": 1, "y": 0})).wait()
    assert result.reason == "authority_revoked"


@authority
async def test_stale_authority_revision_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    result = await env.runtime.submit(
        env.command("navigate_to", {"x": 1, "y": 0}, authority_revision=0)).wait()
    assert result.reason == "authority_stale_revision"


@authority
async def test_out_of_scope_and_wrong_principal_are_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    narrow = env.runtime.authority.grant(Delegation(
        delegation_id="dlg_nav_only", grantor=ROOT, grantee=AGENT, skills=("navigate_to",)))
    r1 = await env.runtime.submit(env.command(
        "capture_image", delegation_id=narrow.delegation_id,
        authority_revision=env.runtime.authority.revision)).wait()
    assert r1.reason == "authority_out_of_scope"
    r2 = await env.runtime.submit(env.command("navigate_to", {"x": 0, "y": 0},
                                              issued_by="agent:intruder")).wait()
    assert r2.reason == "authority_wrong_principal"


@capability
async def test_unavailable_capability_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("camera_offline")
    result = await env.runtime.submit(env.command("capture_image")).wait()
    assert result.reason == "capability_unavailable"
    assert result.observed_effects["details"]["limiting_factors"] == ["camera_offline"]


@capability
async def test_plan_against_old_capability_revision_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    planned = env.robot.capability_report().capability_revision
    env.adapter.battery = 15.0  # mobility becomes restricted -> new capability revision
    result = await env.runtime.submit(env.command(
        "navigate_to", {"x": 1, "y": 0}, capability_revision=planned)).wait()
    assert result.reason == "capability_revision_changed"


async def test_failed_precondition_is_rejected(make_env: MakeEnv) -> None:
    env = make_env(battery_pct=8.0)  # restricted (usable) but below the skill's 10% gate
    result = await env.runtime.submit(env.command("navigate_to", {"x": 1, "y": 0})).wait()
    assert result.reason == "precondition_failed"


@commitments
async def test_postcondition_failure_is_a_failed_outcome(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("drift_m", 0.4)
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    with pytest.raises(OutcomeNotVerified) as exc:
        await h.wait_for_verified_result(timeout=5)
    result = exc.value.result
    assert result.state is S.FAILED and result.reason == "postcondition_failed"
    failing = [c for c in result.verification if not c.satisfied]
    assert failing and failing[0].observed == pytest.approx(1.4)


@uncertain
async def test_deadline_after_start_is_unresolved_not_failed(make_env: MakeEnv) -> None:
    env = make_env()
    with_deadline(env, "navigate_to", 0.05)
    env.adapter.inject("stall")
    h = env.runtime.submit(env.command("navigate_to", {"x": 1, "y": 0}))
    result = await h.wait(timeout=5)
    assert result.state is S.UNRESOLVED and result.reason == "deadline_exceeded"
    assert "interrupt_reported" in env.runtime.trace.kinds(h.command_id)
    # Reconciliation observes the world; the robot never reached the goal.
    assert env.runtime.reconcile(h.command_id).state is S.FAILED


@uncertain
async def test_lost_result_is_reconciled_by_observation_not_retry(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("lose_link_after_motion")
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    result = await h.wait(timeout=5)
    assert result.state is S.UNRESOLVED and result.reason == "outcome_unknown"
    reconciled = env.runtime.reconcile(h.command_id)
    assert reconciled.state is S.VERIFIED and reconciled.reason == "reconciled"
    assert len(env.adapter.executed) == 1


@commitments
async def test_duplicate_submission_is_deduplicated(make_env: MakeEnv) -> None:
    env = make_env()
    cmd = env.command("navigate_to", {"x": 0.5, "y": 0.0})
    h1 = env.runtime.submit(cmd)
    h2 = env.runtime.submit(cmd)
    assert h1 is h2
    await h1.wait(timeout=5)
    assert len(env.adapter.executed) == 1
    with pytest.raises(AdmissionRejected, match="different request"):
        env.runtime.submit(cmd.evolve(args={"x": 9.0, "y": 0.0}))


async def test_busy_robot_rejects_second_command(make_env: MakeEnv) -> None:
    env = make_env()
    h1 = env.runtime.submit(env.command("navigate_to", {"x": 2, "y": 0}))
    h2 = env.runtime.submit(env.command("navigate_to", {"x": 0, "y": 2}))
    assert (await h2.wait()).reason == "robot_busy"
    assert (await h1.wait(timeout=5)).verified


@authority
async def test_cancel_reports_confirmed_stop(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await asyncio.sleep(0.02)
    result = await h.cancel()
    assert result.state is S.CANCELLED
    assert result.observed_effects["stopped_confirmed"] is True
    assert env.adapter.x < 10


@authority
async def test_cancel_without_confirmed_stop_is_not_shown_as_stopped(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("unconfirmed_stop")
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await asyncio.sleep(0.02)
    result = await h.cancel()
    assert result.state is S.CANCELLED
    assert result.observed_effects["stopped_confirmed"] is False
    assert "NOT physically confirmed" in result.message


async def test_non_interruptible_skill_refuses_cancel(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("calibrate_camera"))
    await asyncio.sleep(0.005)
    with pytest.raises(UnsupportedOperation):
        await h.cancel()
    assert (await h.wait(timeout=5)).verified


@authority
async def test_revocation_during_execution_interrupts(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await asyncio.sleep(0.02)
    env.runtime.authority.revoke(env.delegation.delegation_id)
    result = await h.wait(timeout=5)
    assert result.state is S.CANCELLED
    assert "authority_revoked_during_execution" in env.runtime.trace.kinds(h.command_id)


@ownership
async def test_exclusive_resource_requires_current_fencing_token(make_env: MakeEnv) -> None:
    env = make_env()
    rm = env.runtime.resources
    missing = await env.runtime.submit(env.command("dock_at", {"station": "station_a"})).wait()
    assert missing.reason == "missing_fencing_token"

    mine = rm.reserve("station_a", AGENT)
    rm.set_occupancy("station_a", Occupancy.FREE)
    rm.reserve("station_a", "operator:bob", preempt=True)  # supersedes the agent
    stale = await env.runtime.submit(env.command(
        "dock_at", {"station": "station_a"}, fencing={"station_a": mine.fencing_token})).wait()
    assert stale.reason == "stale_fencing_token"
    assert env.adapter.executed == []


@ownership
async def test_fenced_dock_succeeds_for_current_holder(make_env: MakeEnv) -> None:
    env = make_env()
    r = env.runtime.resources.reserve("station_a", AGENT)
    result = await env.runtime.submit(env.command(
        "dock_at", {"station": "station_a"}, fencing={"station_a": r.fencing_token})).wait(5)
    assert result.verified


@commitments
def test_illegal_transitions_are_refused() -> None:
    check_transition(S.EXECUTING, S.UNRESOLVED)
    with pytest.raises(InvalidTransition):
        check_transition(S.SUBMITTED, S.VERIFIED)
    with pytest.raises(InvalidTransition):
        check_transition(S.VERIFIED, S.FAILED)


def test_execution_contract_rejects_nonsense() -> None:
    with pytest.raises(Exception, match="deadline_s"):
        ExecutionContract(deadline_s=0)
