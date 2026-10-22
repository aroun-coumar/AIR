# 0007. Wave 1: fail-closed leases, verifiable registry, ledger v2, W3C ids, schemas

Status: Accepted
Date: 2026-10-22

## Context
The open-source survey ([oss-adoption-plan](../research/oss-adoption-plan.md)) found five
defects in AIR (D1–D5). It also recommended locking several formats before they spread:
trace ids, ledger hashing, schemas and deprecation.

## Decisions
1. **Leases fail closed on physical uncertainty (D1).**
   - A lease that expires while occupancy is OCCUPIED or UNKNOWN becomes
     `EXPIRED_OCCUPIED`. That blocks other holders, denies the expired holder new
     commands (`lease_expired`), and clears only when vacancy is observed.
   - Preemption requires known vacancy.
   - `ResourceReservation` goes to 1.1 (new enum value).
2. **Registry integrity comes from outside the asset file (D3).**
   - A hash-chained publish log records each publish's digest.
   - `fetch(..., digest=)`, `name@ver#digest` refs and an `air.lock` lockfile allow pinning.
   - SemVer/PEP 440 precedence; "latest" excludes prereleases unless asked.
3. **Ledger entry v2 (D4, part).**
   - SHA-256 over RFC 8785 canonical JSON, with an integer `ts_ns`, and data normalized
     to plain JSON before storing and hashing.
   - v1 entries keep verifying and are never written again.
   - Appends are serialized by an OS file lock (`msvcrt`/`fcntl`), and the writer
     reloads before appending, so concurrent processes extend one chain. This
     supersedes ADR 0005's unenforced "single writer".
   - Still open: tail-truncation and rewrite detection (Merkle checkpoints), planned
     as Wave 2 item W2.7.
4. **W3C Trace Context ids (D5).** A 128-bit trace id (32 hex) and 64-bit span id
   (16 hex), never all zeros. `traceparent` encode and decode; legacy `trc_` ids
   left-padded.
5. **Requirement fingerprints.**
   - A fingerprint covers what a requirement says (not its status or owner).
   - Test evidence and activities record the fingerprints they were produced against,
     and the matrix marks evidence STALE when the requirement has since changed.
6. **JSON Schema for every contract.**
   - Generated with the stdlib (Draft 2020-12) and published in
     `schemas/ar.<name>/<ver>.json`; never edited after publication.
   - `air schema check` in CI catches drift and non-superset minors.
   - `$schema` accepts the tag or its URI.
   - `load_any` now loads built-in and entry-point-registered contract modules on
     demand (fixes a latent import-order bug).
7. **Versioning and deprecation policy** ([docs/policy/versioning.md](../policy/versioning.md)):
   `AIRDeprecationWarning(FutureWarning)`, `@deprecated`, `@experimental`, and a test
   that fails on overdue removals.
8. **Repository hygiene**:
   - CI token `permissions: contents: read`; actions pinned by commit SHA; Dependabot.
   - `SECURITY.md` with private reporting and a 14-day acknowledgement.
   - CODEOWNERS, a PR template with an evidence checklist, issue forms.

## Consequences
- `jsonschema` is a dev-only dependency, used to prove that the schemas and the parser
  agree; the core remains stdlib-only.
- Private vulnerability reporting must be enabled in the GitHub repository settings by
  the owner.
- CODEOWNERS takes effect for merges only once branch protection requires code-owner
  review.
