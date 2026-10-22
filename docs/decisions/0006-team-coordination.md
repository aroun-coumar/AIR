# 0006. Team coordination: transactional coordinator, revisions, occupancy-aware release

Status: Accepted
Date: 2026-10-22

## Context
Stage 3 needs allocation, a typed task protocol, reservations, selective
acknowledgements and reconciliation across robots (baseline section 11, REQ06, REQ07,
REQ09, REQ10; gates G06 and G07). It has to stay correct under contention, duplicated
and delayed messages, disconnection and uncertain outcomes.

## Decision
- One **transactional coordinator** per authority domain (the baseline default). No
  consensus protocol. Commitments, outcomes, reallocations and releases are appended to
  the hash-chained ledger.
- Each robot keeps a **robot-local runtime**. Committed work is executed through it, so
  local admission (authority, capability, freshness, preconditions, fencing) still
  applies on the robot.
- Every message is bound to a **task revision**. Reallocation bumps the revision, and
  replies for a superseded revision are ignored and traced.
- **Selective messaging**: proposals go only to participants that advertise the skill
  (or to the pinned or colocated participant). Readiness, commit and query go only to
  the assignee.
- **Fenced reservations** are acquired all-or-nothing in sorted order with a bounded
  wait, which prevents circular wait. A reservation is released only when no unfinished
  task of the holder needs it **and** the holder does not physically occupy the resource
  (`occupancy.<resource>` observations reported with results).
- **Deterministic command ids** (`cmd-<mission>-<task>-r<revision>`) let participants
  deduplicate retransmitted commitments, so nothing executes twice.
- **Uncertain outcomes**: a lost result or acknowledgement, or an UNRESOLVED robot
  result, is settled by QUERY with `reconcile=true`. The robot observes; nothing is
  re-dispatched. Tasks still unresolved keep their reservations, dependents wait, and
  `Team.resume()` continues after reconnection.

## Consequences
The coordinator is a single point of coordination (not of local safety). Robots keep
their local responses during a partition. Replication and partition policy for the
coordinator remain open (baseline section 21). Team-level cancellation and priority
preemption are not yet implemented.
