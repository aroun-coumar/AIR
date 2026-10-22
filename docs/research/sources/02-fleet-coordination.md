# Research source 02: fleet coordination, allocation, reservation, execution frameworks

Raw findings from the 2026-10-22 survey (researcher report, condensed). **[U]** means
unverified. The synthesis is in [../oss-adoption-plan.md](../oss-adoption-plan.md).

## Findings about AIR (confirmed against the source)

- **Lease expiry ignores physical occupancy.** `ResourceManager._expire()` drops the
  reservation when the lease runs out, even if `physical_occupancy` is OCCUPIED. This
  violates ADR 0006 whenever `lease_s` is used.
- **`reserve(preempt=True)` supersedes without checking occupancy.** Fencing blocks the
  old holder's late commands, but nothing physically clears the zone.
- No per-sender sequence number, and no update id within a revision.
- `CANCEL` is defined but unused. There is no CANCELLING team state, and EXECUTING cannot
  go to CANCELLED.

## Projects

| Project | License | Mechanism (files read) |
|---|---|---|
| Open-RMF `rmf_traffic` | Apache-2.0 | `schedule/Negotiation.hpp`: a negotiation table tree with versioned submit, reject (with alternatives) and forfeit; a pluggable `Evaluator` (`QuickestFinishEvaluator`). Local proposal search, with no global planner. |
| Open-RMF `rmf_api_msgs` | Apache-2.0 | `task_state.json`: 12 execution statuses plus a **separate dispatch status** (queued/selected/dispatched/failed_to_assign/canceled_in_flight). Interruptions are keyed by token with labels. Cancellation and kill are separate. Request/response schemas exist for cancel/kill/interrupt/resume/skip. **Schema bug:** `resumed_by.required` names a field that doesn't exist. |
| Open-RMF `rmf_task` | Apache-2.0 | `TaskPlanner.hpp`: `plan(time, agent states, requests)` returns assignments or typed errors (`low_battery`, `limited_capacity`). Greedy or A*. A `finishing_request` sends a robot to charge or park when its tasks end. |
| Open-RMF fleet adapter | Apache-2.0 | Mutex groups. Discourse **#466: "Forcibly releasing mutex group … idle for an excessive amount of time"** while the robot is still in the zone. This is the anti-pattern. |
| `rmf_reservation` | license not shown | requests with alternatives × time windows × costs; SAT or greedy solver; experimental |
| free_fleet | Apache-2.0 | zenoh bridge to Nav2; disconnect behavior undocumented |
| **VDA 5050 v3.0.0** | repo MIT; spec text is VDA-owned | MQTT JSON. Header `headerId` (per topic, +1 per sent message), timestamp, version. Orders: nodes and edges, with **released base vs unreleased horizon** and `orderUpdateId` stitching at the last base node; the base cannot change. **Acceptance:** an update id at or below the current one is rejected (OUTDATED); a same id with identical content is ignored, with different content rejected; any update after a cancel is rejected; on rejection the robot keeps its current order. `cancelOrder` stays RUNNING until the robot has stopped. Action states INITIALIZING/RUNNING/PAUSED/FINISHED/FAILED/RETRIABLE; `blockingType` NONE/SINGLE/SOFT/HARD. Connection: last will `CONNECTION_BROKEN`, ONLINE/OFFLINE. **A disconnected robot completes up to its last released node.** v3 zone and edge requests with grants and `leaseExpiry`: **no answer in time means not granted**; `releaseLossBehavior` is STOP or RETURN. `operatingMode` includes INTERVENED/MANUAL/SERVICE. |
| openTCS | MIT (SPDX headers) | `Scheduler.java`: **claim** (declare the future sequence of resource sets) vs **allocate** (next set in order, asynchronous callback), `allocateNow` for emergencies, pluggable modules (`mayAllocate`/`prepareAllocation`, block modules). `VehicleCommAdapter`: **bounded command capacity**. `TransportOrder` states include WITHDRAWN (removed, cleanup pending). `Vehicle.IntegrationLevel`: TO_BE_IGNORED/NOTICED/RESPECTED/UTILIZED. |
| MassRobotics AMR interop | license [U]; dormant since 2021 | status reports only (`operationalState` enum, e.g. manualOverride, waitingHumanEvent) |
| Nav2 | Apache-2.0 (mostly) | Lifecycle manager bond heartbeats (timeout 4 s, period 0.25 s). "If one is down, bring them all down." `bt_action_node.hpp`: rejection or timeout gives FAILURE; preemption by goal id; **CANCELED maps to SUCCESS by default (anti-pattern)**. |
| BehaviorTree.CPP | MIT | `StatefulActionNode` `onStart`/`onRunning`/`onHalted`; `halt()` is final and owned by the framework |
| ros2_control | Apache-2.0 | `resource_manager.cpp:1892` raises "already claimed"; **move-only `LoanedCommandInterface`** releases the claim in its destructor; failed hardware is removed from the available interfaces; controller switches are STRICT vs BEST_EFFORT, with prepare then perform |
| ROS 2 actions design | - | client-generated goal UUID; ACCEPTED/EXECUTING/CANCELING; SUCCEEDED/ABORTED/CANCELED; a cancel is a request that can be rejected; results cached; **disconnect not specified** |
| PlanSys2 | Apache-2.0 | `ActionExecutor`: REQUEST → RESPONSE → CONFIRM/REJECT → FEEDBACK/FINISH; fails the action if the chosen performer disappears |
| py_trees | BSD | `terminate(new_status)` as the single cleanup hook |
| Gitee | mostly no licenses | openTCS mirrors and derivatives; AgvDispatchSystem (no license; "stop if an AGV is ahead", no reservation model). Nothing novel; do not copy code. |

## Adoption items

| # | Pattern | AIR module | Req | P | Effort |
|---|---|---|---|---|---|
| A1 | **A lease never expires, and preemption never takes a zone, while the robot may still be in it** (OCCUPIED or UNKNOWN become EXPIRED_OCCUPIED and keep blocking); fail closed | `runtime/resources.py` | G06, REQ06 | **P0** | S |
| A2 | **Released prefix vs horizon** in COMMIT; updates stitch at the last released step; the released part is immutable; a disconnected robot finishes the released prefix and then stops | `coordination/*` | REQ07, REQ09, G07, G14 | **P0** | M |
| A3 | **`update_id` within a revision with VDA acceptance rules** (outdated rejected; identical ignored; conflicting rejected; nothing accepted after cancel; on rejection keep the current work) | `protocol.py`, `participant.py` | REQ09, G07 | **P0** | S |
| A4 | Per-sender monotonic `seq` in the envelope; ledger gaps and duplicates | `protocol.py`, `transport.py` | REQ10, REQ12 | P1 | S |
| A5 | **Cancellation state machine**: a CANCELLING state; the cancel has its own response; uncancellable work stays EXECUTING; reservations freed only after a confirmed stop and the A1 condition; CANCELLED distinct from KILLED | `protocol.py`, `coordinator.py`, `runtime.py` | REQ17, REQ07, REQ10 | **P0** | M |
| A6 | **Instant actions** pause/resume/cancel/state_request, plus interrupt/resume keyed by token with labels | `protocol.py`, `runtime.py` | REQ17 | **P0** | M |
| A7 | Liveness: connection state (ONLINE/OFFLINE/CONNECTION_BROKEN) and heartbeats; when broken, reservations stay held and tasks become UNRESOLVED | `transport.py`, `coordinator.py` | G14, G06 | P1 | M |
| A8 | **Claim then allocate**: declare an ordered sequence of resource sets, allocate step by step, with pluggable modules for doors, lifts and areas; incremental release | `resources.py`, `coordinator.py`, `plugins.py` | REQ06, REQ09 | P1 | L |
| A9 | **Graded trust per robot** (ignored/noticed/respected/utilized); manual takeover means respected (no new work, its zone stays reserved) | `team.py`, `authority.py` | REQ17, G06 | P1 | S |
| A10 | Exclusive claim handles that cannot be copied; faulted components removed from availability | `resources.py`, `skill.py` | G06 | P1 | S |
| A11 | STRICT vs BEST_EFFORT multi-resource commits, with prepare then perform | `coordinator.py` | REQ09 | P2 | S |
| A12 | Skill `blocking` semantics (none/single/soft/hard) and a RETRIABLE state with explicit retry/skip | `contracts/skill.py`, `runtime.py` | REQ17, REQ12 | P1 | S |
| A13 | Dispatch status separate from execution status; typed allocation errors; a finishing request (park or charge) | `protocol.py`, `allocation.py` | REQ09, REQ10 | P1 | S |
| A14 | Timing limits announced (minimum command and state intervals, heartbeat) and checked at admission | `participant.py`, `admission.py` | REQ12 | P2 | S |
| A15 | Robot-initiated zone requests with leases, fail-closed | `resources.py`, `protocol.py` | G06, REQ06 | P1 | M |
| A16 | **VDA 5050 v3 bridge adapter** (AIR stays the fleet manager) | new plugin | REQ06, G14 | P1 | L |
| A17 | Open-RMF fleet-adapter bridge (pin and validate schemas) | plugin | REQ06 | P2 | L |
| A18 | Negotiation-style conflict resolution for multiple authority domains | future | REQ06 | P2 | L |
| A19 | MassRobotics outbound status export | plugin | REQ06 | P2 | S |
| A20 | Skill `on_halted` contract (cleanup exactly once on interruption; the framework owns halt) | `skill.py`, `runtime.py` | REQ17 | P1 | S |

## Anti-patterns

1. releasing a lock on timeout or idleness while the robot is still in the zone (RMF #466, AIR's `_expire`)
2. reporting CANCELED as SUCCESS (Nav2 default)
3. a whole-team reset on one heartbeat loss
4. rewriting released or executing work
5. silently coalescing conflicting duplicates
6. trusting third-party schemas without validation
7. "stop if anything is ahead" without a reservation model
8. unbounded command queues to a robot
9. undefined disconnect behavior
