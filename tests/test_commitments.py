"""ADR 0012 evidence: runtime commitment semantics.

Durable intent journal (W2.1); facts vs verdict, APPROXIMATE/SKIPPED and the failure
taxonomy (W2.2); cancellation, kill and instant actions (W2.3); approval as an interrupt
(W2.5); the intent deadman (W2.6); token-scoped holds, blocking, explicit retry
decisions, ``on_halted`` and applied-vs-requested commands (P1).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_robotics import (
    AdmissionRejected,
    CommandState,
    Delegation,
    ManualClock,
    OutcomeNotVerified,
    Robot,
    Runtime,
    SkillRegistry,
    UnsupportedOperation,
)
from agentic_robotics.contracts import (
    ApprovalOutcome,
    CancelCode,
    Condition,
    ExecutionContract,
    FailureKind,
    Interruption,
    LimitAction,
    Observation,
    Op,
    Origin,
    RelativeTargetLimit,
    RetryDecision,
    TaskPlan,
    TaskSpec,
    WorkspaceBox,
)
from agentic_robotics.core import AuthorityError, SchemaError, new_id
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set
from agentic_robotics.runtime import (
    CommandJournal,
    HoldRegistry,
    JournalCorrupt,
    JournalLocked,
    PlanExecutor,
)

from .conftest import AGENT, ROOT, Env, manifest

S = CommandState
MakeEnv = Callable[..., Env]

pytestmark = pytest.mark.req("REQ07", "REQ14")
journal_req = pytest.mark.req("REQ07", "REQ14", "G07")
verdict_req = pytest.mark.req("REQ14", "G07")
cancel_req = pytest.mark.req("REQ17", "G07")
approval_req = pytest.mark.req("REQ17", "G12", "REQ07")
deadman_req = pytest.mark.req("REQ12", "G05")
skill_req = pytest.mark.req("REQ05", "REQ14")

EXTRA = ["drive_velocity", "blink_lights"]


def patch_skill(env: Env, skill_id: str, **changes: Any) -> None:
    skills = []
    for s in env.runtime.skills:
        if s.skill_id == skill_id:
            execution = changes.pop("execution", None)
            if execution:
                s = s.evolve(execution=s.execution.evolve(**execution))
            s = s.evolve(**changes)
        skills.append(s)
    env.runtime.skills = SkillRegistry(skills)


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


def journal_runtime(path: Path, **kw: Any) -> tuple[Runtime, FakeRobotAdapter, CommandJournal]:
    journal = CommandJournal(path)
    rt = Runtime(SkillRegistry(fake_skill_set().skills), root_principals=[ROOT],
                 journal=journal, **kw)
    robot = rt.register(Robot.from_manifest(manifest()))
    rt.authority.grant(Delegation(delegation_id="dlg_test", grantor=ROOT, grantee=AGENT,
                                  robots=("carrier-1",), valid_from=0.0))
    assert isinstance(robot.adapter, FakeRobotAdapter)
    return rt, robot.adapter, journal


def nav(rt: Runtime, x: float = 1.0, y: float = 0.0, **kw: Any) -> Any:
    from agentic_robotics import Command
    fields: dict[str, Any] = {
        "command_id": new_id("cmd"), "robot_id": "carrier-1", "skill_id": "navigate_to",
        "issued_by": AGENT, "delegation_id": "dlg_test",
        "authority_revision": rt.authority.revision, "args": {"x": x, "y": y}}
    fields.update(kw)
    return Command(**fields)


# -- W2.1 durable intent journal ----------------------------------------------------
@journal_req
async def test_intent_is_journaled_before_dispatch(tmp_path: Path) -> None:
    rt, adapter, journal = journal_runtime(tmp_path / "j.jsonl")
    seen: list[list[str]] = []
    original = adapter.execute

    async def spy(ctx: Any) -> Any:
        seen.append(list(CommandJournal(tmp_path / "j.jsonl", lock=False)
                         .entries[ctx.command.command_id].records))
        return await original(ctx)

    adapter.execute = spy  # type: ignore[method-assign]
    h = rt.submit(nav(rt))
    assert (await h.wait(5)).verified
    assert seen == [["accepted", "dispatch"]]
    assert journal.entries[h.command_id].records == ["accepted", "dispatch", "outcome"]
    journal.close()


@journal_req
async def test_restart_reconciles_dispatched_intent_and_never_resends(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    rt1, _adapter1, j1 = journal_runtime(path)
    cmd = nav(rt1, 1.0, 0.0)
    # The process dies after the intent was written and the robot moved, before any
    # outcome was recorded.
    j1.record_accepted(cmd)
    j1.record_dispatch(cmd.command_id, dict(cmd.args))
    j1.close()

    rt2, adapter2, j2 = journal_runtime(path)
    adapter2.x = 1.0  # the world: the robot did arrive
    await rt2.start()
    h = rt2.handle(cmd.command_id)
    assert h.recovered and h.state is S.VERIFIED and h.result is not None
    assert h.result.reason == "reconciled"
    assert adapter2.executed == []          # observed, not resent
    assert rt2.submit(cmd) is h             # deduplication survived the restart
    assert "recovered" in rt2.trace.kinds(cmd.command_id)
    await rt2.stop()
    j2.close()


@journal_req
async def test_restart_without_motion_reconciles_to_failed(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    _, _, j1 = journal_runtime(path)
    cmd = nav(Runtime(SkillRegistry(())), 2.0, 0.0, authority_revision=1)
    j1.record_accepted(cmd)
    j1.record_dispatch(cmd.command_id, dict(cmd.args))
    j1.close()
    rt2, adapter2, j2 = journal_runtime(path)
    await rt2.start()
    result = rt2.handle(cmd.command_id).result
    assert result is not None and result.state is S.FAILED and adapter2.executed == []
    j2.close()


@journal_req
async def test_restart_cancels_intents_that_were_never_dispatched(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    _, _, j1 = journal_runtime(path)
    cmd = nav(Runtime(SkillRegistry(())), authority_revision=1)
    j1.record_accepted(cmd)
    j1.close()
    rt2, adapter2, j2 = journal_runtime(path)
    await rt2.start()
    h = rt2.handle(cmd.command_id)
    assert h.state is S.CANCELLED and h.result is not None
    assert h.result.reason == "not_dispatched_before_restart"
    assert adapter2.executed == []
    j2.close()


@journal_req
async def test_settled_outcomes_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    rt1, _adapter1, j1 = journal_runtime(path)
    cmd = nav(rt1, 0.5, 0.0)
    assert (await rt1.submit(cmd).wait(5)).verified
    rejected = await rt1.submit(nav(rt1, 0.0, 0.0, issued_by="agent:x")).wait()
    j1.close()
    rt2, adapter2, j2 = journal_runtime(path)
    h = rt2.submit(cmd)
    assert h.recovered and h.state is S.VERIFIED and adapter2.executed == []
    assert rt2.handle(rejected.command_id).state is S.REJECTED
    j2.close()


@journal_req
def test_journal_detects_corruption_and_ignores_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    j = CommandJournal(path)
    cmd = nav(Runtime(SkillRegistry(())), authority_revision=1)
    j.record_accepted(cmd)
    j.record_dispatch(cmd.command_id, {})
    with pytest.raises(JournalLocked):
        CommandJournal(path)
    j.close()
    with path.open("ab") as fh:
        fh.write(b'{"kind":"outcome","command_id"')  # died mid-write
    j2 = CommandJournal(path)
    assert j2.torn_tail and j2.entries[cmd.command_id].dispatched
    j2.close()
    lines = path.read_bytes().split(b"\n")
    lines[0] = lines[0].replace(b'"accepted"', b'"acceptex"')
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(JournalCorrupt):
        CommandJournal(path)


# -- W2.2 facts vs verdict ---------------------------------------------------------
@verdict_req
async def test_adapter_cannot_claim_a_verdict(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("claim_verified")
    env.adapter.inject("drift_m", 0.4)
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    result = await h.wait(5)
    assert result.state is S.FAILED and result.failure is FailureKind.GOAL
    assert "verified" not in result.observed_effects
    assert "adapter_claim_ignored" in env.runtime.trace.kinds(h.command_id)


@verdict_req
async def test_model_evidence_can_at_most_make_an_outcome_unresolved(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("model_pose")
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    result = await h.wait(5)
    assert result.state is S.UNRESOLVED and result.reason == "verification_inconclusive"
    assert result.failure is FailureKind.EVIDENCE
    assert result.evidence and not any(e.authoritative for e in result.evidence)
    env.adapter.clear("model_pose")
    assert env.runtime.reconcile(h.command_id).state is S.VERIFIED


@verdict_req
async def test_judgments_are_evidence_not_verdicts(make_env: MakeEnv) -> None:
    env = make_env()
    ok = await env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0})).wait(5)
    judged = env.runtime.judge(ok.command_id, "failure", "vlm:demo", confidence=0.9)
    assert judged.state is S.VERIFIED and judged.evidence[-1].verdict == "failure"
    assert "judgment_disputes_verdict" in env.runtime.trace.kinds(ok.command_id)

    patch_skill(env, "undock", postconditions=())
    rep = await env.runtime.submit(env.command("undock")).wait(5)
    assert rep.state is S.REPORTED_COMPLETE
    assert env.runtime.judge(rep.command_id, "success", "vlm:demo").state is S.REPORTED_COMPLETE
    moved = env.runtime.judge(rep.command_id, "failure", "vlm:demo")
    assert moved.state is S.UNRESOLVED and moved.reason == "judgment_disputes_report"
    with pytest.raises(UnsupportedOperation):
        env.runtime.judge(rep.command_id, "verified", "vlm:demo")


@verdict_req
async def test_evidence_from_before_execution_cannot_verify(make_env: MakeEnv) -> None:
    clock = ManualClock()
    env = make_env(clock=clock)
    env.adapter.x = 1.0
    env.adapter.inject("freeze_sensors")   # pose at the goal, but measured before start
    clock.advance(0.5)                     # still fresh enough to admit
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    result = await h.wait(5)
    assert result.state is S.UNRESOLVED
    assert {e.phase.value for e in result.evidence} == {"before"}


@verdict_req
async def test_within_acceptance_band_is_approximate_never_verified(make_env: MakeEnv) -> None:
    env = make_env()
    nav_skill = env.runtime.skills.get("navigate_to")
    patch_skill(env, "navigate_to", postconditions=tuple(
        Condition(c.signal, c.op, c.value, c.field, c.tolerance, c.description,
                  accept_within=0.5) for c in nav_skill.postconditions))
    env.adapter.inject("drift_m", 0.3)
    h = env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0.0}))
    with pytest.raises(OutcomeNotVerified) as exc:
        await h.wait_for_verified_result(5)
    result = exc.value.result
    assert result.state is S.APPROXIMATE and result.approximate and not result.verified
    assert any(c.approximate for c in result.verification)


@verdict_req
async def test_skip_if_skips_without_moving_and_plans_pass_it(make_env: MakeEnv) -> None:
    env = make_env()
    patch_skill(env, "dock_at", skip_if=(Condition("base.docked_at", Op.EQ, "$args.station"),))
    plan = TaskPlan(plan_id="p1", goal="dock twice", steps=(
        TaskSpec("t1", "dock_at", "carrier-1", {"station": "station_a"}),
        TaskSpec("t2", "dock_at", "carrier-1", {"station": "station_a"}, depends_on=("t1",))))
    result = await PlanExecutor(env.runtime).submit(
        plan, principal=AGENT, delegation_id=env.delegation.delegation_id).wait(5)
    assert result.verified
    assert result.steps["t2"].state is S.SKIPPED and result.steps["t2"].failure is None
    assert len(env.adapter.executed) == 1


@verdict_req
async def test_failure_taxonomy(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("lose_link_after_motion")
    lost = await env.runtime.submit(env.command("navigate_to", {"x": 0.2, "y": 0})).wait(5)
    assert lost.failure is FailureKind.COMMUNICATION
    env.adapter.clear()
    env.adapter.battery = 8.0
    pre = await env.runtime.submit(env.command("navigate_to", {"x": 0.2, "y": 0})).wait(5)
    assert pre.failure is FailureKind.START_STATE
    auth = await env.runtime.submit(env.command("navigate_to", {"x": 0, "y": 0},
                                                issued_by="agent:x")).wait()
    assert auth.failure is FailureKind.AUTHORITY


def test_estimates_carry_uncertainty() -> None:
    base = {"source": "s", "signal": "obj.pose", "clock_domain": "c", "acquired_at": 0.0,
            "received_at": 0.0, "value": {"x": 1}}
    with pytest.raises(SchemaError, match="uncertainty"):
        Observation(**base, origin=Origin.ESTIMATED)
    with pytest.raises(SchemaError, match="square"):
        Observation(**base, covariance=(1.0, 0.0, 0.0))
    with pytest.raises(SchemaError, match="score"):
        Observation(**base, score=1.5)
    ok = Observation(**base, origin=Origin.ESTIMATED, score=0.8)
    assert Observation.from_json(ok.to_json()) == ok and ok.authoritative
    model = Observation(**base, origin=Origin.MODEL, score=0.99)
    assert not model.authoritative


# -- W2.3 cancellation, kill, instant actions --------------------------------------
@cancel_req
async def test_cancel_has_its_own_response_and_is_never_success(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await until(lambda: h.state is S.EXECUTING)
    resp = await env.runtime.request_cancel(h.command_id)
    assert resp.code is CancelCode.ACCEPTED and resp.started and resp.stop_confirmed
    result = await h.wait(5)
    assert result.state is S.CANCELLED and not result.verified
    again = await env.runtime.request_cancel(h.command_id)
    assert again.code is CancelCode.ALREADY_TERMINAL
    assert (await env.runtime.request_cancel("cmd_nope")).code is CancelCode.UNKNOWN_COMMAND
    assert env.adapter.halted == [(h.command_id, "cancel_requested")]


@cancel_req
async def test_cancel_of_non_interruptible_skill_is_rejected(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("calibrate_camera"))
    await until(lambda: h.state is S.EXECUTING)
    resp = await env.runtime.request_cancel(h.command_id)
    assert resp.code is CancelCode.REJECTED
    assert (await h.wait(5)).verified


@cancel_req
async def test_kill_is_distinct_from_cancel(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await until(lambda: h.state is S.EXECUTING)
    resp = await env.runtime.kill(h.command_id, reason="operator_stop")
    assert resp.code is CancelCode.ACCEPTED and resp.stop_confirmed
    result = await h.wait(5)
    assert result.state is S.KILLED and result.reason == "operator_stop"
    assert result.observed_effects["stopped_confirmed"] is True
    assert env.adapter.exit_mode == "hold"


@cancel_req
async def test_unhonoured_kill_leaves_the_outcome_unresolved(make_env: MakeEnv) -> None:
    env = make_env(extra_skills=EXTRA)
    env.runtime.kill_grace_s = 0.02
    env.adapter.inject("ignore_halt")
    h = env.runtime.submit(env.command("drive_velocity", {"vx": 0.1, "vy": 0.0}))
    await until(lambda: h.state is S.EXECUTING)
    resp = await env.runtime.kill(h.command_id)
    assert resp.stop_confirmed is False
    result = await h.wait(5)
    assert result.state is S.UNRESOLVED and result.reason == "kill_unconfirmed"
    # The base subsystem stays held: it may still be moving.
    blocked = await env.runtime.submit(env.command("drive_velocity",
                                                   {"vx": 0.0, "vy": 0.0})).wait()
    assert blocked.reason == "subsystem_held_unresolved"


@cancel_req
async def test_pause_resume_and_state_request(make_env: MakeEnv) -> None:
    env = make_env()
    patch_skill(env, "navigate_to", interruption=Interruption.PAUSE)
    h = env.runtime.submit(env.command("navigate_to", {"x": 3.0, "y": 0.0}))
    await until(lambda: env.adapter.x > 0.2)
    status = await env.runtime.pause(h.command_id)
    assert status.state is S.PAUSED and status.started
    x = env.adapter.x
    await asyncio.sleep(0.02)
    assert env.adapter.x == x
    assert env.runtime.state_request(h.command_id).last_progress is not None
    assert (await env.runtime.resume_execution(h.command_id)).state is S.EXECUTING
    assert (await h.wait(5)).verified


@cancel_req
async def test_pause_requires_declared_support(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 3.0, "y": 0.0}))
    await until(lambda: h.state is S.EXECUTING)
    with pytest.raises(UnsupportedOperation, match="pause"):
        await env.runtime.pause(h.command_id)
    await h.cancel()


@cancel_req
async def test_on_halted_is_called_exactly_once(make_env: MakeEnv) -> None:
    env = make_env()
    h = env.runtime.submit(env.command("navigate_to", {"x": 10, "y": 0}))
    await until(lambda: h.state is S.EXECUTING)
    env.runtime.authority.revoke(env.delegation.delegation_id)   # cancels
    await env.runtime.kill(h.command_id)                          # and kills
    await h.wait(5)
    await asyncio.sleep(0.01)
    assert [c for c, _ in env.adapter.halted] == [h.command_id]


# -- W2.5 approval as an interrupt --------------------------------------------------
def approval_env(make_env: MakeEnv, **kw: Any) -> Env:
    env = make_env(**kw)
    patch_skill(env, "navigate_to", requires_approval=True)
    return env


@approval_req
async def test_approval_is_bound_to_id_and_digest_and_single_use(make_env: MakeEnv) -> None:
    env = approval_env(make_env)
    rt = env.runtime
    h = rt.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0}))
    assert h.state is S.AWAITING_APPROVAL and env.adapter.executed == []
    [req] = rt.pending_approvals()
    assert req.command_id == h.command_id and req.args == {"x": 0.5, "y": 0.0}
    rev = rt.authority.revision
    with pytest.raises(AuthorityError, match="different arguments"):
        rt.resume(h.command_id, "approve", ROOT, rev, args_digest="sha256:00")
    with pytest.raises(AuthorityError, match="own command"):
        rt.resume(h.command_id, "approve", AGENT, rev, args_digest=req.args_digest)
    with pytest.raises(AuthorityError, match="stale"):
        rt.resume(h.command_id, "approve", ROOT, rev - 1, args_digest=req.args_digest)
    with pytest.raises(AuthorityError, match="approve delegation"):
        rt.resume(h.command_id, "approve", "operator:bob", rev, args_digest=req.args_digest)
    rt.resume(h.command_id, ApprovalOutcome.APPROVE, ROOT, rev, args_digest=req.args_digest)
    assert (await h.wait(5)).verified
    with pytest.raises(UnsupportedOperation, match="single-use"):
        rt.resume(h.command_id, "approve", ROOT, rev, args_digest=req.args_digest)
    kinds = rt.trace.kinds(h.command_id)
    assert kinds.index("approval_decided") < kinds.index("started")


@approval_req
async def test_delegated_approver_and_rejection(make_env: MakeEnv) -> None:
    env = approval_env(make_env)
    rt = env.runtime
    rt.authority.grant(Delegation(delegation_id="dlg_bob", grantor=ROOT,
                                  grantee="operator:bob", operations=("approve",)))
    h1 = rt.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0},
                               authority_revision=rt.authority.revision))
    req = h1.approval_request
    assert req is not None
    rt.resume(h1.command_id, "approve", "operator:bob", rt.authority.revision,
              args_digest=req.args_digest, delegation_id="dlg_bob")
    assert (await h1.wait(5)).verified
    h2 = rt.submit(env.command("navigate_to", {"x": 0.0, "y": 0.0},
                               authority_revision=rt.authority.revision))
    assert h2.approval_request is not None
    rt.resume(h2.command_id, "reject", ROOT, rt.authority.revision,
              args_digest=h2.approval_request.args_digest, note="not now")
    r2 = await h2.wait()
    assert r2.state is S.REJECTED and r2.reason == "approval_rejected"
    assert r2.failure is FailureKind.APPROVAL


@approval_req
async def test_nobody_deciding_denies_by_default(make_env: MakeEnv) -> None:
    env = approval_env(make_env)
    patch_skill(env, "navigate_to", approval_timeout_s=0.02)
    h = env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0}))
    result = await h.wait(5)
    assert result.state is S.REJECTED and result.reason == "approval_timeout"
    assert env.adapter.executed == []

    clock = ManualClock()
    env2 = approval_env(make_env, clock=clock)
    h2 = env2.runtime.submit(env2.command("navigate_to", {"x": 0.5, "y": 0.0}))
    clock.advance(61.0)
    assert env2.runtime.expire_approvals() == [h2.command_id]
    assert (await h2.wait()).reason == "approval_timeout"


@approval_req
async def test_unattended_mode_turns_asks_into_denies(make_env: MakeEnv) -> None:
    env = approval_env(make_env)
    env.runtime.unattended = True
    result = await env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0})).wait()
    assert result.state is S.REJECTED and result.reason == "approval_unavailable_unattended"


@approval_req
async def test_revocation_while_awaiting_approval_rejects(make_env: MakeEnv) -> None:
    env = approval_env(make_env)
    h = env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0.0}))
    env.runtime.authority.revoke(env.delegation.delegation_id)
    assert (await h.wait()).reason == "authority_revoked"


# -- W2.6 intent deadman ------------------------------------------------------------
def fast_deadman(env: Env) -> None:
    patch_skill(env, "drive_velocity",
                execution={"max_input_age_s": 0.06, "keepalive_period_s": 0.02})


@deadman_req
async def test_missing_keepalive_stops_with_declared_exit_mode(make_env: MakeEnv) -> None:
    env = make_env(extra_skills=EXTRA)
    fast_deadman(env)
    h = env.runtime.submit(env.command("drive_velocity", {"vx": 0.5, "vy": 0.0}))
    await until(lambda: h.state is S.EXECUTING)
    for _ in range(6):                      # the operator holds the stick
        await asyncio.sleep(0.02)
        assert env.runtime.keepalive(h.command_id, AGENT)
    assert h.state is S.EXECUTING and env.adapter.vx == 0.5
    result = await h.wait(5)                # ... then lets go
    assert result.state is S.KILLED and result.reason == "keepalive_lapsed"
    assert result.failure is FailureKind.KEEPALIVE_LAPSED
    assert result.observed_effects["exit_mode"] == "damp" and env.adapter.vx == 0.0
    assert env.adapter.halted == [(h.command_id, "keepalive_lapsed")]
    assert not env.runtime.keepalive(h.command_id, AGENT)


@deadman_req
async def test_only_the_issuer_keeps_a_command_alive(make_env: MakeEnv) -> None:
    env = make_env(extra_skills=EXTRA)
    fast_deadman(env)
    h = env.runtime.submit(env.command("drive_velocity", {"vx": 0.1, "vy": 0.0}))
    await until(lambda: h.state is S.EXECUTING)
    assert not env.runtime.keepalive(h.command_id, "agent:intruder")
    assert (await h.wait(5)).state is S.KILLED
    with pytest.raises(UnsupportedOperation):
        env.runtime.keepalive(
            (await env.runtime.submit(env.command("undock")).wait(5)).command_id, AGENT)


@deadman_req
def test_keepalive_period_must_be_below_max_input_age() -> None:
    with pytest.raises(SchemaError, match="keepalive"):
        ExecutionContract(max_input_age_s=0.5, keepalive_period_s=0.5)


# -- P1 skill items ------------------------------------------------------------------
@skill_req
def test_holds_are_released_only_by_their_token() -> None:
    reg = HoldRegistry()
    reg.acquire("r", ("base", "arm"), "inv_1", "cmd_1", 0.0)
    with pytest.raises(AdmissionRejected) as exc:
        reg.acquire("r", ("arm", "gripper"), "inv_2", "cmd_2", 0.0)  # all or nothing
    assert exc.value.details["holder"] == "cmd_1"
    assert reg.holder("r", "gripper") is None
    assert reg.release("r", "inv_stale") == ()
    assert {h.subsystem for h in reg.release("r", "inv_1")} == {"base", "arm"}


@skill_req
async def test_non_blocking_skill_runs_beside_motion(make_env: MakeEnv) -> None:
    env = make_env(extra_skills=EXTRA)
    moving = env.runtime.submit(env.command("navigate_to", {"x": 2.0, "y": 0.0}))
    await until(lambda: moving.state is S.EXECUTING)
    blink = env.runtime.submit(env.command("blink_lights", {"times": 2}))
    hard = await env.runtime.submit(env.command("calibrate_camera")).wait()
    assert hard.reason == "robot_busy"
    assert (await blink.wait(5)).verified
    assert moving.state is S.EXECUTING      # still driving when the blink finished
    assert (await moving.wait(5)).verified


@skill_req
async def test_occupied_subsystem_is_reported_with_its_holder(make_env: MakeEnv) -> None:
    env = make_env(extra_skills=EXTRA)
    fast_deadman(env)
    h = env.runtime.submit(env.command("drive_velocity", {"vx": 0.1, "vy": 0.0}))
    await until(lambda: h.state is S.EXECUTING)
    assert env.runtime.state_request(h.command_id).holds == ("base",)
    assert env.runtime.holds.holder("carrier-1", "base").command_id == h.command_id
    await env.runtime.cancel(h.command_id)
    assert env.runtime.holds.held("carrier-1") == []


@skill_req
async def test_retriable_failure_needs_an_explicit_decision(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("path_blocked")
    first = await env.runtime.submit(env.command("navigate_to", {"x": 0.5, "y": 0})).wait(5)
    assert first.state is S.FAILED and first.retriable and first.failure is FailureKind.GOAL
    await asyncio.sleep(0.02)
    assert len(env.adapter.executed) == 1   # nothing was resent by itself
    with pytest.raises(AuthorityError):
        env.runtime.decide(first.command_id, "retry", "agent:other")
    env.adapter.clear("path_blocked")
    retry = env.runtime.decide(first.command_id, RetryDecision.RETRY, AGENT)
    assert retry is not None and retry.command.retry_of == first.command_id
    result = await retry.wait(5)
    assert result.verified and result.retry_of == first.command_id
    with pytest.raises(UnsupportedOperation, match="already"):
        env.runtime.decide(first.command_id, "skip", AGENT)
    with pytest.raises(UnsupportedOperation, match="retriable"):
        env.runtime.decide(result.command_id, "retry", AGENT)


@skill_req
async def test_workspace_box_clamps_or_rejects_and_reports_applied(make_env: MakeEnv) -> None:
    env = make_env()
    patch_skill(env, "navigate_to", workspace=WorkspaceBox(
        "map", {"x": (-1.0, 1.0), "y": (-1.0, 1.0)}, action=LimitAction.CLAMP))
    result = await env.runtime.submit(env.command("navigate_to", {"x": 3.0, "y": 0.5})).wait(5)
    assert result.verified and not result.as_requested
    assert result.applied_args == {"x": 1.0, "y": 0.5}
    assert result.limited_by == ("workspace:x",)
    patch_skill(env, "navigate_to", workspace=WorkspaceBox("map", {"x": (-1.0, 1.0)}))
    rejected = await env.runtime.submit(env.command("navigate_to", {"x": 3.0, "y": 0})).wait()
    assert rejected.reason == "outside_workspace" and rejected.failure is FailureKind.GOAL
    nan = await env.runtime.submit(env.command("navigate_to",
                                               {"x": math.nan, "y": 0.0})).wait()
    assert nan.reason == "invalid_arguments"


@skill_req
async def test_max_relative_target_clamps_from_the_observed_value(make_env: MakeEnv) -> None:
    env = make_env()
    patch_skill(env, "navigate_to", max_relative_target=(
        RelativeTargetLimit("x", "base.pose", 0.5, field="x"),))
    result = await env.runtime.submit(env.command("navigate_to", {"x": 2.0, "y": 0})).wait(5)
    assert result.verified and result.applied_args == {"x": 0.5, "y": 0}
    assert result.limited_by == ("max_relative_target:x",)
    assert env.adapter.x == pytest.approx(0.5)
    with pytest.raises(SchemaError, match="undeclared"):
        env.runtime.skills.get("navigate_to").evolve(
            max_relative_target=(RelativeTargetLimit("z", "base.pose", 0.5),))


@skill_req
async def test_adapter_clipping_is_reported_and_not_verified(make_env: MakeEnv) -> None:
    env = make_env()
    env.adapter.inject("clip_x", 0.5)
    result = await env.runtime.submit(env.command("navigate_to", {"x": 1.0, "y": 0})).wait(5)
    assert result.state is S.FAILED and result.reason == "postcondition_failed"
    assert result.limited_by == ("adapter:max_x",)
    assert result.observed_effects["adapter_applied"]["x"] == 0.5


def test_new_terminal_and_significant_kinds_reach_debugging_and_the_ledger() -> None:
    from agentic_robotics.provenance.project import DEFAULT_RUNTIME_KINDS
    from agentic_robotics.runtime import runtime as rt

    significant = rt.COMMITMENT_TRACE_KINDS - {"paused", "resumed", "hold_acquired",
                                               "hold_released", "keepalive_refused"}
    assert significant <= DEFAULT_RUNTIME_KINDS
