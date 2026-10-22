# 0012. Runtime commitment semantics

Status: Accepted
Date: 2026-10-22

## Context
[ADR 0003](0003-verified-vs-unresolved-outcomes.md) separated reported, verified and
unresolved outcomes. The open-source survey
([oss-adoption-plan.md](../research/oss-adoption-plan.md) Wave 2 and P1, and
[catalog-backlog.md](../research/catalog-backlog.md) section 1) found gaps in how the
runtime keeps its commitments:
- Command handles lived only in memory (defect D2). A restart lost deduplication and
  UNRESOLVED state, so a retried command could execute twice.
- Nothing in the contracts stopped a report or a model judgment from acting as a verdict.
- Cancel had no answer of its own. There was no forced stop distinct from a cancel, and
  no pause.
- Human approval, continuous skills with an intent deadman, robot-local subsystem holds,
  explicit retry decisions and clamped commands had no runtime support.

This ADR covers the runtime side (group 1). Coordination-level cancellation, the
released-prefix horizon (W2.3 and W2.4 in `coordination/`) and the resource-manager lease
fixes belong to group 7.

## Decisions
1. **W2.1 Durable command intent journal** (`runtime/journal.py`, `CommandJournal`).
   - The journal is an append-only JSON-lines file. Each record carries a SHA-256 over its
     canonical body.
   - Each record is a single `os.write` on an `O_APPEND` descriptor, followed by `fsync`.
   - An OS lock file allows one writer at a time. A torn final line is dropped; corruption
     anywhere else raises `JournalCorrupt`.
   - Records: `accepted` (with the command), `awaiting_approval`, `dispatch` (written and
     fsynced *before* `adapter.execute`, with the applied arguments), and `outcome` (the
     result, written on every finish, including rejections).
   - `Runtime(journal=...)` rebuilds handles on the first `submit` or `start`:
     - A settled outcome is kept, so deduplication and `command_id_reused` survive.
     - A dispatched intent with no outcome becomes UNRESOLVED (`runtime_restarted`).
     - An intent that was never dispatched becomes CANCELLED
       (`not_dispatched_before_restart`).
   - `start()` reconciles every recovered UNRESOLVED command by observation. Nothing is
     resent.
   - A recovered command's `occupies` holds are re-taken until reconciliation settles it.
   - Recovered handles have no `started_at` (the old clock domain is gone), so any
     current observation counts as acquired after execution.
2. **W2.2 Facts vs verdict.**
   - `Observation` 1.1 adds two fields:
     - `origin`: measured, estimated, model or reported.
     - `association_quality`: authoritative or best_effort.

     `Observation.authoritative` is true only for a measured or estimated value that is
     authoritatively associated and not INVALID.
   - Postconditions are evaluated with `require_authoritative=True`. A model judgment or a
     best-effort association never satisfies them. It makes verification inconclusive,
     which leads to UNRESOLVED and never to VERIFIED.
   - Evidence carries a `phase` (before, during or after), measured against the execution
     window `[started_at, reported_at)`. Only `after` evidence can satisfy a
     postcondition. **This is stricter than ADR 0003**, which accepted any observation
     acquired after start. An observation from mid-execution now leaves the outcome
     UNRESOLVED.
   - `ExecutionResult.evidence` lists what was weighed.
   - `Runtime.judge()` records a non-authoritative judgment, for example from a VLM:
     - it never changes VERIFIED or FAILED, and a disagreement is traced as
       `judgment_disputes_verdict`;
     - a failure or inconclusive judgment on an unverifiable REPORTED_COMPLETE outcome
       moves it to UNRESOLVED (`judgment_disputes_report`).
   - An `AdapterReport` cannot carry a verdict. Effects keys `verified`, `verdict`,
     `state`, `outcome` and `success` are stripped and traced (`adapter_claim_ignored`).
   - New outcome kinds:
     - **APPROXIMATE** (terminal): every postcondition holds, but at least one only
       within its `Condition.accept_within` band. `verified` is false, and plans stop on
       it.
     - **SKIPPED** (terminal): the skill's `skip_if` conditions hold on fresh
       authoritative evidence at dispatch. Nothing moves, it is not a failure, and plans
       pass it.
   - `FailureKind` gives results a typed failure: start_state, goal, frame_transform,
     stale_state, stale_sensor, communication, preempted, timeout, keepalive_lapsed,
     authority, resource, capability, invalid_request, approval, evidence, hardware or
     unknown. `failure_kind_for(reason)` maps the runtime's stable reasons, and adapters
     may set `AdapterReport.failure`.
   - An estimated observation must carry its uncertainty: `uncertainty`, a square finite
     `covariance` with a non-negative diagonal, or a `score` in [0, 1].
   - `ParamSpec` now rejects NaN and infinite numbers. Before, they passed min/max checks.
3. **W2.3 Runtime cancellation and instant actions.**
   - `request_cancel()` answers at once with a `CancelResponse` (1.0): accepted,
     rejected, unknown_command or already_terminal, plus `stop_confirmed`.
     `cancel()` keeps its old behaviour (request, then wait for the outcome).
   - `kill()` is a forced stop. It calls the new adapter hook
     `halt(command_id, exit_mode)`, which defaults to `interrupt`. It also works for
     skills that cannot be cancelled, and it ends **KILLED**, distinct from a cooperative
     **CANCELLED**.
   - If the adapter does not return within `kill_grace_s`, the call is abandoned and the
     outcome is UNRESOLVED (`kill_unconfirmed`).
   - A command that completes before a stop takes effect keeps its real, verified
     outcome. It is marked `completed_despite_halt` and never reported as cancelled. A
     cancel is never reported as success.
   - Instant actions: `pause()` and `resume_execution()` (state PAUSED) apply only to
     skills declaring `interruption: pause`, through new adapter hooks. `state_request()`
     returns a `CommandStatus` snapshot.
   - Deadlines keep running while a command is paused (fail safe).
4. **W2.5 Human approval as an interrupt.**
   - When `SkillSpec.requires_approval` is set, an admitted command enters
     AWAITING_APPROVAL with an `ApprovalRequest` (1.0) that carries
     `Command.request_digest()`.
   - `resume(command_id, decision, approver, authority_revision, *, args_digest,
     delegation_id)` is bound by id, never by position. The decision is refused when:
     - the digest differs;
     - the approver is the requester;
     - the cited revision is not the current one;
     - the approver is neither root nor holding an `approve`-operation delegation for the
       robot and skill.
   - A refused decision leaves the request pending. A valid one consumes it: approvals
     are single-use.
   - An approved command is re-admitted in full at dispatch. Approval grants no authority.
   - No decision before `approval_timeout_s` means a default deny (`approval_timeout`).
     `expire_approvals()` applies it on a manual clock.
   - `Runtime(unattended=True)` turns every ask into a deny.
   - Revoking the command's authority rejects a pending approval.
   - Decisions are recorded as `ApprovalDecision` (1.0) on the handle and in the trace.
5. **W2.6 Intent deadman.**
   - `ExecutionContract` 1.1 adds `keepalive_period_s`, which must be strictly between 0
     and `max_input_age_s`, and `exit_mode` (hold or damp).
   - For a continuous skill, a watchdog checks how long ago the last
     `Runtime.keepalive()` call came. If it was more than `max_input_age_s` ago, the
     runtime kills the command with the declared exit mode. The result is KILLED with
     reason and failure `keepalive_lapsed`.
   - Only the issuing principal, still holding authority, can refresh the intent.
   - The fake adapter's `drive_velocity` skill implements it.
6. **P1 skill items.**
   - `SkillSpec` 1.1 `occupies` names robot-local subsystems. They are held
     all-or-nothing in `runtime/holds.py`, scoped by the invocation's token: only that
     token releases them, and a conflict names the holder.
   - Holds of an UNRESOLVED command are kept until reconciliation. A new command that
     needs them is rejected (`subsystem_held_unresolved`) rather than waiting forever.
   - `blocking` (hard, soft or none, after VDA 5050 `blockingType`) replaces the per-robot
     lock with a FIFO scheduler: hard excludes everything, soft excludes soft and hard,
     none excludes only hard, and overlapping `occupies` always conflict.
   - The default stays `hard`, so existing skills behave as before. The fake
     `navigate_to` is now `soft`, so `blink_lights` (none) can run beside it.
   - Retriable failures: `AdapterReport.retriable` or `SkillSpec.retriable_failures`
     marks a FAILED result `retriable`. The runtime never resends.
     `decide(command_id, retry|skip, decided_by)` by the issuer or a root principal:
     - `retry` submits a *new* command with `Command.retry_of` (1.2) through full
       admission;
     - `skip` records abandonment.

     The decision is single-use, and an UNRESOLVED outcome cannot be retried.
   - `on_halted(command_id, reason)` is called exactly once per halted started command
     (cancel, kill, deadline, deadman, revocation), after it stopped.
   - Motion limits:
     - `WorkspaceBox` (per-argument [min, max] in a frame, reject or clamp);
     - `RelativeTargetLimit` (`max_relative_target` against a fresh authoritative
       observation; clamp or reject; unobservable means reject).

     Non-finite targets are always rejected.
   - The adapter receives the applied arguments (`ExecutionContext.command.args`) and the
     requested ones (`requested_args`). `ExecutionResult` 1.1 reports `applied_args` and
     `limited_by`, and `as_requested` is false when anything was clamped.
   - Verification is against the applied command.
   - An adapter that clipped further reports `applied`/`limited_by` as facts. Verification
     stays against what the runtime dispatched.

Contract versions: `observation` 1.1, `execution_contract` 1.1, `command` 1.2,
`execution_result` 1.1, `skill_spec` 1.1, `skill_set` 1.1 (it embeds `skill_spec`), and
new `cancel_response` 1.0, `approval_request` 1.0 and `approval_decision` 1.0. All
changes are additive with defaults. Schemas are exported, and the minor-is-superset check
passes.

## Consequences
- Tests changed because of intentional semantic or version changes:
  - `tests/test_contracts.py`: the fake skill set is now `ar.skill_set/1.1`.
  - `tests/test_provenance.py`: the context pack lists `command/1.2`.

  No runtime test needed changing.
- Callers must handle the new states. `CommandState.terminal` now includes KILLED,
  APPROXIMATE and SKIPPED.
- `PlanExecutor` passes SKIPPED, stops on APPROXIMATE, and keeps reservations when a
  CANCELLED or KILLED step's stop is not physically confirmed.
- With a journal, every state change costs an fsync. The journal has no compaction yet.
- New trace kinds (`COMMITMENT_TRACE_KINDS`) are not in `Project.runtime_sink`'s default
  set. To ledger them, pass `DEFAULT_RUNTIME_KINDS | COMMITMENT_TRACE_KINDS`.

## Sources (patterns adopted, no code copied)
- PhyAgentOS-core (MIT): execution record vs verdict, evidence phase and association
  quality, reconcile-don't-resend.
- LangGraph (MIT): pending writes before side effects, and `interrupt`/`Command(resume=)`.
  AIR binds by id, not by index.
- google/adk-python (Apache-2.0), openai-agents-python (MIT), inspect_ai (MIT) and
  agentscope (Apache-2.0): approval bound to a pending call, single use, deny on timeout,
  unattended deny.
- ROS 2 actions (`CancelGoal` return codes); Open-RMF cancel vs kill; Nav2
  (Apache-2.0/BSD-3; its CANCELED→SUCCESS default is the counter-example).
- BehaviorTree.CPP (MIT): `onHalted` exactly once. VDA 5050 (repo MIT): `blockingType`
  and instant actions.
- moveit_msgs and OMPL (BSD-3-Clause): error codes and planner status. geometry2/tf2
  (BSD-3-Clause): transform failures. vision_msgs (Apache-2.0): hypotheses with a score
  and covariance.
- dimos (Apache-2.0) and ros2_control (Apache-2.0): token-scoped, all-or-nothing holds.
- LeRobot (Apache-2.0): `send_action` returns the applied action, and
  `max_relative_target`. RAI (Apache-2.0): workspace bounds that reject NaN.
- Microduck core (Apache-2.0), Lite3_MotionSDK (MIT) and booster_deploy (Apache-2.0):
  the command deadman and the damp/hold exit mode.

## Not done / needs the owner
- Coordination-level cancel and kill, the released prefix and horizon, and `update_id`
  (W2.3 and W2.4 in `coordination/`) belong to group 7. The coordinator currently maps
  KILLED, APPROXIMATE and SKIPPED results to a failed task; group 7 should map SKIPPED
  as a pass.
- `provenance/explain.py` lists terminal kinds without killed, skipped and approximate,
  and `Project.runtime_sink` defaults lack the new kinds (other owners).
- Approval:
  - no approver chains or escalation;
  - no "bypass-immune" asks;
  - no edited-argument approvals (an edit must be a new command);
  - no two-step operator-confirmed policy start.
- The journal has no compaction or rotation. Recovery assumes the skill registry and
  robots are configured the same after a restart. A command whose robot is not
  registered stays UNRESOLVED.
- Skills without postconditions stay UNRESOLVED once a judgment disputes them. Only new
  authoritative evidence channels or an operator process (not built) can settle them.
- No real-time guarantee: the deadman watchdog polls on the asyncio loop
  (`watchdog_poll_s`). The device-side deadman (as in vendor SDKs) remains the primary
  protection.
