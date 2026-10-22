# 0018. Team cancellation, released prefix, liveness and scoped resource loans

Status: Accepted
Date: 2026-10-22

## Context

ADR 0006 gave AIR a transactional team coordinator. The fleet-coordination survey
(research source 02) found gaps in it:

- `CANCEL` was defined but never used. There was no CANCELLING team state, and an
  executing task could not be cancelled.
- A commitment covered exactly one step, with no update id. A robot that lost its
  connection had no defined rule for what it may still do.
- Messages had no per-sender sequence number. Connection state was not modelled: a lost
  robot showed up only as missed timeouts.
- Reservations were single-resource and could be released by id by anyone. There were no
  claim sequences, no allocation modules (doors, lifts, areas), no mode-switch
  acknowledgement and no graded integration of robots.

This ADR covers oss-adoption-plan items W2.3 (coordination side) and W2.4, the P1 rows
on liveness, claim-then-allocate and graded trust, and the catalog-backlog amendments
W1.1/leases and W3.7 (the liveness half).

## Decisions

1. **Cancellation and instant actions (W2.3).**
   - New team states: `CANCELLING`, and `KILLED` (distinct from `CANCELLED`).
     `EXECUTING -> CANCELLED` is illegal. A task ends CANCELLED or KILLED only after the
     robot confirms the stop (`stopped_confirmed`), or confirms the step never started.
   - An unconfirmed stop becomes `UNRESOLVED` (`stop_unconfirmed`), and its holds are
     kept.
   - `TeamCoordinator.cancel(mission, mode=cancel|kill)` sends a `CANCEL` and gets a
     `CANCEL_RESPONSE` matched by `causal_ref`, with accept or reject per command.
     Rejection reasons are `not_interruptible`, `already_finished` and
     `unknown_command`.
   - A rejected graceful cancel returns the task to EXECUTING. The task then finishes and
     is verified normally; it is never labelled cancelled.
   - A kill stops waiting: work that cannot be interrupted becomes UNRESOLVED
     (`killed_stop_unconfirmed`), to be reconciled later.
   - Not-started tasks stop at once. Released steps that have not started are reported
     `NOT_EXECUTED` (`not_started`).
   - After a cancel, the order accepts nothing more (`order_cancelled`). This holds even
     when the cancel arrives before the order update does.
   - Reservations are freed only through the existing release rule: no unfinished or
     unresolved need, and not observed occupied (W1.1).
   - `MissionState.CANCELLED` is new. A cancelled mission is never VERIFIED.
   - Instant actions (`INSTANT` / `INSTANT_RESPONSE`): `pause`, `resume`,
     `state_request` and `cancel`. They are valid at any revision.
   - Pause means "start nothing new". The runtime cannot pause a running command, and
     the response says so (`current_continues`). A paused participant answers
     NOT_READY (`paused`).
2. **Released prefix vs horizon, plus order updates (W2.4).**
   - `TeamPlan.release_window` (1.1) sets how many consecutive steps of one
     participant's sequence are released in one order update.
   - A step is released only if its dependencies are verified or inside the prefix, and
     only once its resources are held.
   - The update carries the released `steps` (fully specified and fenced), `stitch_after`
     and the `horizon` (task ids only, so not executable).
   - `OrderBook` applies the VDA 5050 acceptance rules:
     - an id at or below the current one is rejected (`outdated`);
     - the same id with the same content is ignored (a retransmission, acknowledged with
       results resent);
     - the same id with different content is rejected (`conflicting_update`);
     - an update after a cancel is rejected (`order_cancelled`);
     - an update that does not continue from the last released step is rejected
       (`stitch_mismatch`, `released_step_changed`);
     - on any rejection (`NACK`) the robot keeps its current work.
   - A released step whose released predecessor did not verify is `NOT_EXECUTED`
     (`dependency_not_verified`). The robot never guesses.
   - A participant that loses its coordinator keeps executing what is released, and
     nothing else. The coordinator advances `stitch_after` only when the robot has
     acknowledged the update, or reconciliation shows it received it.
   - Default `release_window=1` keeps the old step-by-step behaviour.
3. **Liveness, per-sender sequence numbers and HLC.**
   - The `CoordinationMessage` envelope goes to 1.1, adding `seq`, `update_id` and `hlc`.
   - The coordinator sends one message per recipient, so each channel has a contiguous
     `seq`. `SeqTracker` classifies messages as in order, gap, duplicate or reordered.
     Gaps go into the ledger (`coordination.message_gap`).
   - `ConnectionState` is ONLINE, OFFLINE or CONNECTION_BROKEN. `LivenessMonitor`
     derives it from transport facts and from heartbeats (`Team(heartbeat_s=…)`,
     `CoordinatorTimeouts.heartbeat_timeout_s`).
   - `LocalTransport` implements the zenoh liveliness-token pattern. A graceful
     undeclare means OFFLINE. `disconnect` drops the token without a goodbye, which reads
     as CONNECTION_BROKEN (MQTT last will).
   - When a participant's connection breaks:
     - its in-flight tasks become UNRESOLVED (`connection_broken`), without waiting out
       result deadlines;
     - its holds are kept;
     - it gets no new work.
     Nothing else in the team is reset.
   - Messages carry hybrid logical clock timestamps (`coordination/hlc.py`). A reply
     always orders after its request. A remote timestamp too far ahead is rejected
     (`hlc_drift_rejected`).
4. **Resources: scoped loans, claims, modules, mode switches, trust.**
   - `ResourceManager.acquire(resources, holder, mode=, params=)` returns a `Lease`.
     Acquisition is all-or-nothing and sorted. Nothing changes until every resource is
     free, vetted and prepared.
   - Only the lease's invocation token can release what it owns (`not_loan_holder`).
     Evidence records a digest (`loan`), never the token.
   - A resource the holder already holds is shared into the new lease, with a
     reference count.
   - `ResourceManager.claim()` gives a `Claim` (openTCS claim-then-allocate): declare the
     sequence of resource sets, then `allocate_next` / `try_allocate_next` per step.
     `release_step` is refused while a resource is observed occupied. `withdraw` keeps
     occupied steps.
   - `claims_on()` shows the declared future needs.
   - Pluggable `ResourceModule`s: `may_allocate` (veto), `prepare` (pending), `perform`,
     `released`. Reference modules:
     - `DoorModule`: granted only when observed open; unknown counts as closed. The door
       is asked to close on release.
     - `LiftModule`: the car must be at the requested floor with its doors open.
     - `AreaModule`: mutual-exclusion groups.
     - `ModeSwitchModule`: ros2_control prepare then perform. The hardware must
       acknowledge, and a requested mode with no module fails closed
       (`mode_unsupported`).
   - `ResourceReservation` goes to 1.2, adding `loan`, `claim_id` and `mode`.
   - `reserve()` stays the unscoped primitive, releasable by id, for existing callers.
   - The coordinator now reserves through leases and releases with their tokens.
   - `TrustLevel` (openTCS IntegrationLevel), set with `Team.set_trust`:
     - `ignored`: messages discarded;
     - `noticed`: reports can only make occupancy more conservative;
     - `respected`: no new work, and holds are kept (manual takeover);
     - `utilized`: full participant.
5. **Reference scenario.**
   - `air scenario handoff --variant disconnect`: the carrier is released three steps.
     It loses its link while driving, and finishes exactly those three. The horizon is
     untouched and its holds are kept. After reconnecting, `resume` reconciles by
     observation and the mission verifies, with 5 carrier commands and none twice.
   - `--variant cancel`: the cancel is accepted for the drive, and the released steps
     are confirmed not started. Every task ends CANCELLED and the mission is CANCELLED.
   - The exit code checks the expected state for each variant.

## Consequences

- Protocol contract bumps: `coordination_message` 1.1 (new fields and message kinds),
  `team_plan` 1.1 (`release_window`) and `resource_reservation` 1.2. 1.0/1.1 documents
  still load, and unknown fields still fail.
- Multi-recipient coordinator messages are now one message per recipient. Transport
  stats count more messages for proposals.
- A COMMIT payload is now an order update (`order_id`, `steps`, `stitch_after`,
  `horizon`) instead of one command. Participants built against the old payload must be
  updated. There are no third-party participants yet.
- `LocalTransport` also drops in-flight messages whose *sender* went down.
- The participant reports itself busy while released steps are pending, so it is not
  offered unrelated work mid-prefix.
- New ledger kinds: `coordination.cancel_requested`, `cancel_response`, `cancelled`,
  `killed`, `connection`, `message_gap`, `trust_changed`, `instant_action`. New trace
  kinds are in `TEAM_TRACE_KINDS`.

## Sources and licenses

Only patterns were adopted. No code or specification text was copied. The behaviour
comes from the survey notes in research source 02, which record the files read. This
change did not fetch the sources again. AIR implements no external wire format (there
is no VDA 5050 bridge yet), so nothing here depends on exact upstream message shapes.

| Source | License | Used for |
|---|---|---|
| VDA 5050 v3.0.0 (§4.1 base/horizon, §6.1.4 order update acceptance, `cancelOrder`, instantActions, connection topic) | repo MIT; spec text VDA-owned (not reproduced) | released prefix, update rules, instant actions, ONLINE/OFFLINE/CONNECTION_BROKEN, headerId |
| ROS 2 actions design (CANCELING; a cancel is a request that can be rejected) | Apache-2.0 | cancel with its own response |
| Open-RMF `rmf_api_msgs` (cancel vs kill) | Apache-2.0 | CANCELLED vs KILLED |
| Nav2 `bt_action_node` (CANCELED mapped to SUCCESS) | Apache-2.0 | counter-example: a cancel is never success |
| openTCS `Scheduler`, `Vehicle.IntegrationLevel`, block modules | MIT | claim-then-allocate, modules, trust levels |
| ros2_control `resource_manager`, `LoanedCommandInterface`, prepare/perform switch | Apache-2.0 | scoped loans, acknowledged mode switches |
| zenoh / rmw_zenoh liveliness tokens | EPL-2.0 OR Apache-2.0 | presence derived from liveliness |
| dora-rs / `uhlc`; Kulkarni et al., "Logical Physical Clocks" (2014) | Apache-2.0 (dora) | HLC algorithm and drift bound |

## Not done / needs the owner

- **Runtime-level semantics (group 1).** `Runtime.cancel` returns CANCELLED even when
  the stop is not confirmed (with `stopped_confirmed: false` in effects). The
  coordinator turns that into UNRESOLVED, but the robot-local command state machine
  should do so too.
- **No real pause.** `pause` stops new starts only. Interrupting and resuming a running
  skill (`Interruption.PAUSE`) needs a runtime hook.
- **Claims are not wired into the coordinator.** It still reserves per released prefix
  with leases. Declaring the horizon as a `Claim`, and allocating step by step through
  doors and lifts, is the next step. It needs async module preparation, because prepare
  is synchronous today.
- `plans.PlanExecutor` (group 1) still uses unscoped `reserve`/`release`. Moving it to
  leases is that group's call.
- **Heartbeats are opt-in.** They are off by default to keep existing timings, and
  liveness then comes only from transport facts. Production transports need a real
  last-will or liveliness binding (MQTT, zenoh), which is an optional adapter.
- **No VDA 5050 bridge adapter** (P1 A16), and no robot-initiated zone requests (A15).
- **The NOTICED and IGNORED gradings are AIR's fail-closed reading** of openTCS. The
  owner should confirm that discarding all IGNORED messages, including results, is
  wanted.
