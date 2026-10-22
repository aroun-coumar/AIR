# 0003. Separate reported, verified and unresolved outcomes

Status: Accepted
Date: 2026-10-22

## Context
Baseline sections 6 and 11: acceptance, start, reported completion, verified
postconditions and resource release are separate events. Exactly-once physical effects
cannot be assumed when an action starts and its outcome is lost.

## Decision
The command state machine distinguishes REPORTED_COMPLETE, VERIFIED (only from
observations acquired after start) and UNRESOLVED. That covers a deadline exceeded after
start, lost contact, or inconclusive observations. UNRESOLVED is settled only by
`Runtime.reconcile()` (observation), and plan steps that end UNRESOLVED keep their
reservations.

## Consequences
Callers must handle UNRESOLVED explicitly. Blind retries are not offered.
