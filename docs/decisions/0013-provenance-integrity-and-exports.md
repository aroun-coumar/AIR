# 0013. Provenance integrity (Merkle checkpoints) and interoperable exports

Status: Accepted
Date: 2026-10-22

## Context
The ledger's hash chain (ADR 0005, ledger v2 in ADR 0007) detects edits, deletions and
reordering inside the log. It cannot detect a truncated tail or a wholesale, internally
consistent rewrite, and proving one entry needs the whole file (finding D4, research
source 03). AIR evidence was also only readable by AIR: supply-chain tools (in-toto,
SLSA, GUAC), provenance tools (PROV), observability backends (OpenTelemetry, MLflow)
and requirements tools (StrictDoc, ReqIF) could not consume it. Recordings had no
integrity or ledger link, and there was no way to ask "has anything an activity
depended on changed?". This ADR covers oss-adoption-plan W2.7, the P1/P2 provenance
items and catalog-backlog W2.7 and W5.2.

Everything here is stdlib-only. Exports are lossy *views*; the ledger stays the source
of truth, and no export or anchor is on the append path.

## Decisions
1. **RFC 9162 Merkle tree plus C2SP checkpoints** (`provenance/merkle.py`).
   - Leaves are the ledger entries' own SHA-256 hashes; leaf hash `SHA-256(0x00 || d)`,
     node hash `SHA-256(0x01 || l || r)`. Inclusion (`PATH`) and consistency (`PROOF`)
     proofs and their verification follow RFC 9162 sections 2.1.3 and 2.1.4. Roots match
     the RFC 6962 / certificate-transparency reference vectors (tested).
   - Checkpoints are C2SP `tlog-checkpoint` notes (origin, decimal size, base64 root),
     signed as C2SP `signed-note`s. Three choices: **unsigned** (the tools say so: git
     history is the only anchor), **HMAC-SHA256** (stdlib; private-use signature type
     `0xff` + `air-hmac-sha256`; proves "holder of the project key"), or **Ed25519**
     (type `0x01`). Ed25519 *verification* is a small pure-Python RFC 8032 verifier
     (no secrets, so constant time is not needed) and passes the RFC 8032 vector and the
     C2SP signed-note example; Ed25519 *signing* needs the new extra
     `agentic-robotics[signing]` (`cryptography`) and fails with a clear message without
     it. Keys are never written into the project (`air ledger keygen` warns).
   - `create_checkpoint` refuses to write a checkpoint that is not a consistent
     extension of the latest stored one (C2SP: never sign two inconsistent
     checkpoints), and notes it in the ledger (`ledger.checkpointed`).
   - Checkpoints live in `.air/checkpoints/<size>.checkpoint` and are committed: **git is
     the anchor**. `merkle.Anchor` is the interface for external witnesses; `GitAnchor`
     reports whether the file is committed (it never commits); `RekorAnchor` is a
     placeholder that refuses (owner decision: no network anchoring).
   - CLI: `air ledger checkpoint`, `air ledger prove <seq>`,
     `air ledger verify --since <file|latest>`, `air ledger keygen`.
2. **in-toto attestations** (`provenance/attest.py`).
   - Work runs: Statement v1 with the **SLSA Provenance v1** predicate. Materials are
     the activity's *used* entities (`resolvedDependencies`), products with a digest are
     the `subject`; sandbox capabilities, model-turn and tool-call counts go in
     `internalParameters`/`byproducts`. If an activity produced no file, the subject is
     its `activity.finished` ledger record. **No SLSA level is claimed.**
   - Tests: the **test-result v0.1** predicate. passed -> `passedTests`, failed/error ->
     `failedTests`; skipped tests make the result `WARNED` (the predicate has no skipped
     list, and a partly skipped run must not look like a full pass). The subject is the
     git commit; configuration is the pytest config file digest and the digest of the
     requirement fingerprints. `pytest --air-evidence ... --air-attest PATH` writes it.
   - Optional **DSSE** envelope (PAE per DSSE v1.0.2, payload type
     `application/vnd.in-toto+json`) with any signer; statements are serialized with
     RFC 8785. Written to `.air/attestations/`, digest recorded (`attestation.created`).
   - CLI: `air attest work <activity_id>`, `air attest tests`, `air attest verify`.
3. **PROV-JSON export** (`provenance/export_prov.py`, `air export prov`): entity,
   activity (`prov:startTime/endTime`), agent, `used`, `wasGeneratedBy`,
   `wasDerivedFrom`, `wasAssociatedWith`, plus `wasInvalidatedBy` for replaced parts and
   `wasInformedBy` for parent activities. Identifiers are qualified names under declared
   prefixes; unsafe characters are percent-encoded. Parameters and notes are not
   exported. The `prov` package parses the output (tested where installed).
4. **OpenTelemetry without the SDK** (`provenance/otel.py`, `air export otel`). OTLP/JSON
   `ExportTraceServiceRequest` (hex ids, integer enums, string int64) from activities,
   work model turns and tool calls, and runtime events (ledger `runtime.*` or a
   `*.trace.jsonl`). To a JSON-lines file (OTLP file-exporter shape) or an OTLP/HTTP
   endpoint through an injectable poster. GenAI semantic-convention names
   (`invoke_agent`, `chat`, `execute_tool`, `plan`, `gen_ai.*`) are in one table,
   `GENAI_SEMCONV`, pinned to `semantic-conventions-genai@9ffc7e00` (no release tag yet;
   Development status), and `--no-genai` drops them. `unresolved` commands are not
   marked as errors (rule 3: unknown is not failed). No `[otel]` extra was needed.
   Goals, prompts, tool arguments and outputs are never exported.
5. **Recording entities** (`provenance/recording.py`, `air recording register|show|verify`).
   Any file becomes a `recording` entity whose identity is its sha256, with activity,
   trace, robot and time range; linked to the activity (`recording.linked`,
   `activity.generated`). A stdlib MCAP reader (Header, Metadata, Attachment, Channel,
   Message, Chunk header, Statistics) and a minimal writer (with an `air` metadata
   record: project, ledger size/head, latest checkpoint, git commit, activity, trace).
   MCAP CRCs are written as 0 and never called integrity. The `mcap` package reads the
   writer's output (tested where installed).
6. **Reproducibility lock** (`provenance/repro.py`): `air repro lock [--activity]` writes
   `.air/repro.lock` (format `air.repro_lock/1`): params, input and output digests,
   Python, platform, every installed distribution version, git commit and dirty state.
   `air repro --check` lists each change and exits 1 if any.
7. **Self-describing run log** (`provenance/runlog.py`, format `air_runlog: 1`, JSON
   lines): header with formats (field, type, unit), parameters, clock domain and info
   before any data; `required_features` that a reader must understand or reject the
   file (ULog incompat-flag pattern); `optional_features` whose records (`"x": feature`)
   are skipped; `sync` and `dropout` markers; parameter updates; integer-ns time that
   never goes back. `register_runlog` validates the whole log and registers it.
8. **StrictDoc export, not ReqIF** (`provenance/export_sdoc.py`, `air export sdoc`).
   ReqIF interpretation differs between tools and writing one's own ReqIF is an
   anti-pattern (source 03); StrictDoc's SDoc grammar is public and its own parser
   checks it, and StrictDoc converts SDoc to ReqIF. The document carries its own
   `[GRAMMAR]` (UID, STATUS = claimed status, TITLE, STATEMENT, AIR_KIND, AIR_OWNER,
   AIR_VERIFICATION, AIR_EVIDENCE = computed matrix result) and `Parent` relations from
   `traces_to`. StrictDoc 0.30.4's `SDReader` parses the export of the baseline
   requirement set (tested where installed).

## Verification done
- Merkle roots against the RFC 6962 reference vectors; exhaustive proof checks for trees
  of 1 to 33 leaves, including tampering.
- Ed25519 verifier against RFC 8032 test 1 and the c2sp.org/signed-note example key and
  note; DSSE PAE against the DSSE spec vector.
- In a scratch environment (not the shared venv) with `cryptography` 50.0.2, `prov`
  3.2.3, `mcap` 1.6.0, `strictdoc` 0.30.4 and `opentelemetry-proto`: Ed25519Signer
  interoperates with the pure verifier, `prov` parses the PROV-JSON, `mcap` reads the
  writer's metadata and attachments, StrictDoc parses the SDoc, and the OTLP JSON parses
  into the official `ExportTraceServiceRequest` (after the documented hex-to-base64 id
  difference between OTLP/JSON and generic proto3 JSON). These tests skip when the
  packages are absent.

## Consequences
- New CLI: `air ledger checkpoint|prove|keygen`, `air ledger verify --since`,
  `air attest work|tests|verify`, `air export prov|otel|sdoc`,
  `air recording register|show|verify`, `air repro lock`, `air repro --check`;
  `pytest --air-attest`.
- New ledger kinds: `ledger.checkpointed`, `attestation.created`, `recording.linked`,
  `repro.locked`. New project files: `.air/checkpoints/` (commit), `.air/attestations/`
  (commit if wanted), `.air/repro.lock` (commit), `.air/exports/` (ignored).
- New extra `[signing]`. No new contracts; the checkpoint, lock and run-log formats
  carry their own format identifiers.
- A checkpoint protects only what was committed before the attack; an attacker who can
  rewrite git history as well needs an external witness (see below).

## Sources and licenses
- RFC 9162 (Certificate Transparency 2.0), RFC 6962 test vectors, RFC 8032 (Ed25519),
  RFC 4648 (base64): IETF specifications.
- C2SP `tlog-checkpoint` and `signed-note` (github.com/C2SP/C2SP; no license file at
  the repository root, per-spec terms not checked): formats implemented from the
  specification text; no code copied.
- in-toto attestation framework spec (Statement v1, ResourceDescriptor, test-result v0.1;
  Apache-2.0) and DSSE protocol v1.0.2 (Apache-2.0).
- SLSA Provenance v1 (slsa.dev, Community Specification License).
- W3C PROV-DM and PROV-JSON (W3C Member Submission); `prov` (MIT) used only to test.
- OpenTelemetry OTLP specification and `semantic-conventions-genai` (Apache-2.0);
  `opentelemetry-proto` used only to test.
- MCAP specification (foxglove/mcap, MIT); `mcap` package used only to test.
- PX4 ULog format description (pattern only: definitions before data, incompat flags,
  dropouts, sync); `pyulog` (BSD-3) not used.
- `dvc.lock` (DVC, Apache-2.0): pattern only.
- StrictDoc SDoc grammar (Apache-2.0): grammar read, parser used only to test.
- `cryptography` (Apache-2.0 or BSD-3): optional extra.

## Not done / needs the owner
- **Rekor (or any public transparency log) anchoring** is designed, not enabled; it is
  irreversible publication and an owner decision. Also open: witness cosignatures
  (C2SP `tlog-cosignature`) from a second party.
- **Signing key custody**: where Ed25519/HMAC keys live (OS keychain, CI secret, HSM),
  who holds them, rotation. The repository should get its first committed checkpoint
  after the merge (`air ledger checkpoint`, then commit `.air/checkpoints/`); this
  branch did not write to the shared ledger.
- Whether the repository's own CI should emit `--air-attest` and sign it.
- MCAP: reading compressed chunks (zstd/lz4 are not stdlib) and writing messages; the
  stdlib writer is metadata/attachments only. A `[mcap]` extra could add both.
- OpenLineage, Sphinx-Needs and MLflow exports (P2) were not built. ReqIF is reached
  through StrictDoc, not written directly.
- The GenAI semantic conventions have no release yet; the pin must be revisited when
  one is published.
