# 0005. Provenance spine: PROV model, hash-chained ledger, evidence-backed claims

Status: Accepted
Date: 2026-10-22

## Context
The owner requires complete traceability, maintenance records and provenance for every
research, development, simulation and maintenance activity in every project built on the
framework, plus debugging and a context feeder, established before the repo grows.
Baseline: REQ02, REQ10, REQ15, REQ16 and foundations F01, F05.

## Decision
- One per-project store, `.air/`, holding a hash-chained JSON-lines ledger (single
  writer, safe reload on external appends) and a content-addressed artifact store.
- The W3C PROV shape: activities use and generate entities, associated with actors,
  linked to requirement ids, with the environment captured automatically.
- Causal trace context (trace/span ids via contextvars) on every runtime event,
  propagated by `Command.trace_id` and `TaskPlan.trace_id`. Trace sinks copy significant
  events into the ledger.
- Requirement claims are checked against evidence (code tags, pytest markers, recorded
  activities). The check fails on unsupported claims, failing tests, or unknown ids.
- A blockchain or consensus layer is not used. Cross-organization trust can be added
  behind the same ledger interface, for example by anchoring the head hash.

Amended by [ADR 0007](0007-wave1-integrity-and-formats.md): the ledger now uses a v2
canonical encoding (RFC 8785) and serializes writers with an OS file lock, and trace ids
use the W3C/OTel format.

## Consequences
Every new feature carries a small tagging and evidence cost. The ledger is local and
single-writer; concurrent writers on one file are not supported, beyond reloading
appends made by another writer before writing.
