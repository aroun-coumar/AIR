# Research source 03: provenance, lineage, attestation, traceability, recording

Raw findings from the 2026-10-22 survey (researcher report, condensed). **[U]** marks
claims that were not verified. The synthesis is in
[../oss-adoption-plan.md](../oss-adoption-plan.md).

## Findings about AIR's current code (confirmed against the source)

- **Trace ids are 64-bit.** `core/ids.py:new_id` gives `trc_<16 hex>`. W3C/OTel trace ids
  are 128-bit (32 hex). Span ids are 64-bit and already match.
- **The ledger hash is not portable.** `ledger._hash` uses Python `json.dumps(sort_keys,
  default=str)` over a body containing a float `ts`. Other implementations cannot be
  guaranteed to reproduce it, which blocks independent verifiers and signed checkpoints.
- **A linear chain cannot detect tail truncation or a full rewrite.** `verify()`
  recomputes from GENESIS. ADR 0005 mentions anchoring the head hash, but nothing
  implements it.
- **Single-writer is not enforced.** There is no file lock, and reloading when the file
  size changes leaves a race window.
- **No inclusion proofs.** Verification is O(n), and proving one entry needs the whole file.

## Sources

| Project / spec | License | Mechanism |
|---|---|---|
| W3C Trace Context; OTel trace API | Apache-2.0 | `traceparent` is `00-<32hex trace>-<16hex span>-<flags>`, with all-zero ids invalid. SpanContext has a 16-byte trace id, 8-byte span id, flags, TraceState and IsRemote. It also defines span links, kinds and status. The OTLP file exporter (JSONL) is still in Development status. |
| OTel GenAI semconv (`semantic-conventions-genai`) | Apache-2.0 | Spans `create_agent`, `invoke_agent` (CLIENT or INTERNAL), `invoke_workflow`, `plan`. Attributes `gen_ai.agent.*` and `gen_ai.conversation.id` (do not fall back to the trace id). Tool definitions are opt-in because they may be sensitive. All of it is **Development status**. |
| OpenLineage | Apache-2.0 | RunEvent START/RUNNING/COMPLETE/ABORT/FAIL. `runId` must be a UUID. Job and datasets. Every facet carries `_producer` and `_schemaURL`. 39 facets, including Parent, ProcessingEngine, SourceCodeLocation, DatasetVersion and TestRun. |
| W3C PROV-JSON; `prov` | MIT (`prov`) | PROV-JSON is a Member Submission, not a Recommendation. `prov` reads and writes PROV-N/JSON/JSONLD/XML/RDF, converts to networkx and renders via Graphviz. |
| MLflow | Apache-2.0 | Tracing is OTel-compatible and ingests OTLP at `/v1/traces`, so **an AIR OTel exporter also reaches MLflow**. |
| DVC | Apache-2.0 | Now stewarded by lakeFS (Nov 2025). `dvc.yaml` stages have cmd, deps, outs and params. `dvc.lock` records resolved hashes and params, so you can ask "has anything changed?". Uses MD5 by default. |
| in-toto attestation | Apache-2.0 [U] | Statement v1: `_type`, `subject[{name, digest}]`, `predicateType`, `predicate`. The **test-result v0.1** predicate (`result`, `configuration`, `passedTests`/`failedTests`) maps almost directly onto AIR's evidence file. |
| SLSA v1.1 provenance | - | `buildDefinition` + `runDetails{builder.id,...}`. The level depends on the builder, so a laptop build is not L3. |
| RFC 9162 Merkle trees; C2SP tlog-checkpoint; Rekor v2 (rekor-tiles) | Apache-2.0 (Rekor) | Leaf hash `H(0x00‖d)` and node hash `H(0x01‖L‖R)`, with inclusion and consistency proofs. Checkpoints are signed notes (`origin`, `size`, `root`) with Ed25519 signatures and witness cosignatures. **A signer must never sign two inconsistent checkpoints.** Rekor v2 is tile-based and sharded. |
| StrictDoc | Apache-2.0 | ReqIF import/export (experimental; varies between tools), `@relation(REQ, scope=...)` source markers, JUnit ingestion. |
| Sphinx-Needs | MIT | `needs.json` schema. |
| Doorstop | LGPLv3 | Items are YAML files. A `reviewed` fingerprint covers each item, and **each link stores the parent's fingerprint**, so a changed parent marks the child as suspect. |
| OpenFastTrace | GPL-3.0 | Ids are `type~name~revision` (the revision is part of the id). Code tags look like `[impl->dsn~x~1]`, and `>>impl,test` declares the needed coverage. Copy the idea; do not vendor the code. |
| MCAP | MIT | Records Header, Schema, Channel, Message, Chunk, Index, **Attachment**, **Metadata**, Statistics, Summary. Integrity uses **CRC32 only (not tamper-evident)**. |
| rosbag2 | Apache-2.0 | **The default storage plugin is MCAP.** A bag is a directory with `metadata.yaml`. |
| ROS diagnostics | BSD-3 | updater, aggregator, `self_test`, `/diagnostics` (levels OK/WARN/ERROR/STALE [U]) |
| SwanLab (CN) | Apache-2.0 [U] | lightweight self-hostable experiment tracker |
| openEuler IB_Robot (CN) | no license stated | episodic recorder, Rerun sidecar, `bag_to_lerobot`, model-bundle identity checks. Evidence that the field is converging on rosbag/MCAP-to-LeRobot pipelines. |

No notable open-source requirements-traceability tool was found on Gitee or AtomGit.

## Adoption items

| # | Pattern | AIR module | Serves | P | Effort |
|---|---|---|---|---|---|
| 1 | **Canonical encoding v2** for ledger entries: JCS (RFC 8785)-style JSON, integer `ts_ns`, no `default=str`, `"v":2` (v1 entries still verify) | `ledger.py`, `schema._dump` | REQ10, F05 | P0 | S |
| 2 | **RFC 9162 Merkle tree plus C2SP signed checkpoints**: `air ledger checkpoint`, `prove <seq>`, `verify --since <checkpoint>`; keep the chain too | new `provenance/merkle.py` | REQ10, REQ16 | P0 | M |
| 3 | External anchoring: commit or tag the checkpoint in git; optionally submit to Rekor via sigstore | CLI, extra `[attest]` | REQ10 | P1 | M |
| 4 | **Enforce a single writer** with an OS file lock | `ledger.py` | REQ10 | P0 | S |
| 5 | **W3C/OTel ids**: 128-bit trace and 64-bit span ids in bare lowercase hex (prefix for display only); `to_traceparent`/`from_traceparent`; legacy 64-bit ids left-padded | `core/ids.py`, `tracing.py`, `Command.trace_id` | REQ15, REQ02 | P0 | S |
| 6 | Optional OTel exporter (`air[otel]`) mapping agent decisions to `invoke_agent`/`plan` and commands to tool or INTERNAL spans with `air.*` attributes; dependency-free OTLP-JSONL writer; semconv pinned in one module | new `provenance/otel.py` | REQ15, REQ18 | P1 | M |
| 7 | in-toto Statement v1 **test-result** attestation from `pytest --air-evidence`; ledger the attestation digest; optional DSSE signing | `pytest_plugin.py`, new `provenance/attest.py` | F01, REQ10 | P1 | S/M |
| 8 | SLSA build provenance from CI (GitHub attestation), linked to release activities | CI | REQ10, F07 | P2 | S |
| 9 | PROV-JSON / PROV-JSONLD export (`prov` package used only for validation, optional) | `export_prov.py` | REQ10, F07 | P1 | S |
| 10 | OpenLineage export (`runId = uuid5(activity_id)`, facets) | `export_openlineage.py` | F07 | P2 | S |
| 11 | **Suspect evidence on requirement change**: a fingerprint per requirement, recorded with the evidence; the matrix check reports "stale" when they differ | `traceability.py`, `RequirementSet`, plugin | F01, REQ16 | P0 | S |
| 12 | StrictDoc/ReqIF interop through StrictDoc's converter; recognize `@relation(...)` markers | `traceability.py` | F01, REQ18 | P2 | M |
| 13 | Sphinx-Needs `needs.json` export | `traceability.py` | F01 | P2 | S |
| 14 | **MCAP run recordings referenced from the ledger**: recording entity with sha256; MCAP Metadata `air` record (project, activity_id, trace_id, ledger seq, commit); scenario and config as attachments; CRC is not integrity | new `provenance/recording.py`, extra `[mcap]` | REQ02, F05, F07 | P1 | M |
| 15 | Reproducibility lock (dvc.lock pattern): resolved params, input digests and environment per stage; `air repro --check` | `project.py`, CLI | F05 | P1 | M |
| 16 | MLflow one-way export | extra `[mlflow]` | F05 | P2 | S |
| 17 | ROS diagnostics to AIR health and incident events | ROS adapter | REQ15, REQ16 | P2 | S |

## Anti-patterns

- non-standard ids on the wire
- exporters treated as the source of truth (they are lossy views; an exporter failure must
  never block an append)
- hard dependencies on otel, mlflow, sigstore, prov or mcap
- hard-coding Development-status semconv
- recording prompts or content by default (especially once anchored publicly, where it
  cannot be erased)
- calling CRC32 or MD5 integrity
- running a log server per project
- committing signing keys
- claiming SLSA levels for local builds
- writing your own ReqIF parser
- vendoring GPL-3.0 code
