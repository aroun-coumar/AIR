"""in-toto attestations of AIR evidence, optionally wrapped in signed DSSE envelopes.

* **Work runs** (agent sandbox activities, :mod:`agentic_robotics.work`) become an
  in-toto Statement v1 with a SLSA Provenance v1 predicate: the activity's *used*
  entities are ``resolvedDependencies`` (materials), its *generated* entities with a
  digest are the ``subject`` (products). Supply-chain tools that read SLSA (GUAC,
  slsa-verifier style policies) can consume it. No SLSA *level* is claimed: a laptop or
  a sandbox run is not a hardened builder.
* **Test runs** (the pytest evidence file) become a Statement with the in-toto
  ``test-result/v0.1`` predicate: ``result``, ``configuration``, ``passedTests``,
  ``warnedTests``, ``failedTests``. The subject is the git commit tested.

Both are plain JSON first. :func:`dsse_sign` wraps a statement in a DSSE envelope
(payload type ``application/vnd.in-toto+json``), signed with any :mod:`.signing` signer.

Verified against: in-toto attestation spec v1 (statement.md, resource_descriptor.md,
predicates/test-result.md) and DSSE protocol v1.0.2 (PAE).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from .canonical import canonical_bytes
from .signing import Signer, Verifier

if TYPE_CHECKING:
    from .project import Project

__requirements__ = ("REQ10", "F01")

STATEMENT_V1 = "https://in-toto.io/Statement/v1"
TEST_RESULT_V01 = "https://in-toto.io/attestation/test-result/v0.1"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
WORK_BUILD_TYPE = "urn:agentic-robotics:work-run:v1"
WORK_BUILDER_ID = "urn:agentic-robotics:work-agent"
INTOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
ATTESTATION_DIR = "attestations"


def _digest_map(digest: str | None) -> dict[str, str] | None:
    """``sha256:<hex>`` -> ``{"sha256": "<hex>"}`` (in-toto DigestSet)."""
    if not digest:
        return None
    algo, _, value = digest.partition(":")
    if not value:
        return None
    return {algo: value}


def statement(subjects: Sequence[dict[str, Any]], predicate_type: str,
              predicate: dict[str, Any]) -> dict[str, Any]:
    if not subjects:
        raise SchemaError("an in-toto statement needs at least one subject")
    for s in subjects:
        if not s.get("digest"):
            raise SchemaError("every in-toto subject needs a digest", subject=s)
    return {"_type": STATEMENT_V1, "subject": list(subjects), "predicateType": predicate_type,
            "predicate": predicate}


def check_statement(doc: dict[str, Any]) -> list[str]:
    """Structural checks a consumer would apply. Empty list means acceptable."""
    problems = []
    if doc.get("_type") != STATEMENT_V1:
        problems.append("_type is not https://in-toto.io/Statement/v1")
    subjects = doc.get("subject")
    if not isinstance(subjects, list) or not subjects:
        problems.append("subject must be a non-empty list")
    else:
        for s in subjects:
            if not isinstance(s, dict) or not isinstance(s.get("digest"), dict) \
                    or not s["digest"]:
                problems.append(f"subject without digest: {s!r}")
    if not isinstance(doc.get("predicateType"), str):
        problems.append("predicateType missing")
    if doc.get("predicateType") == TEST_RESULT_V01:
        pred = doc.get("predicate") or {}
        if pred.get("result") not in ("PASSED", "WARNED", "FAILED"):
            problems.append("test-result predicate needs result PASSED|WARNED|FAILED")
        if not isinstance(pred.get("configuration"), list):
            problems.append("test-result predicate needs a configuration list")
    return problems


# -- work runs ----------------------------------------------------------------------------
def _descriptor(entity: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": entity.name}
    digest = _digest_map(entity.digest)
    if digest:
        out["digest"] = digest
    if entity.uri:
        out["uri"] = entity.uri
    out["annotations"] = {"air.entity_id": entity.entity_id, "air.kind": entity.kind}
    return out


def _rfc3339(ts: float | None) -> str | None:
    if ts is None:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).isoformat().replace(
        "+00:00", "Z")


def work_statement(project: Project, activity_id: str) -> dict[str, Any]:
    """SLSA Provenance v1 statement for one recorded activity (typically a work run)."""
    acts = {a.activity_id: a for a in project.activities()}
    act = acts.get(activity_id)
    if act is None:
        raise SchemaError("unknown activity", activity_id=activity_id)
    entities = project.entities()
    sandbox = [e.data for e in project.ledger.query(kind="work.sandbox", subject=activity_id)]
    finished = project.ledger.query(kind="activity.finished", subject=activity_id)
    materials = [_descriptor(entities[u]) for u in act.used if u in entities]
    products = [_descriptor(entities[g]) for g in act.generated
                if g in entities and entities[g].digest]
    subjects = [{k: v for k, v in p.items() if k != "uri"} for p in products]
    if not subjects:
        if not finished:
            raise SchemaError("activity has no digested products and has not finished",
                              activity_id=activity_id)
        # No file products: attest the activity's own ledger record instead.
        subjects = [{"name": f"air-ledger:{activity_id}",
                     "digest": {"sha256": finished[-1].hash},
                     "annotations": {"air.ledger_seq": finished[-1].seq}}]
    model_turns = project.ledger.query(kind="work.model_turn", subject=activity_id)
    tool_calls = project.ledger.query(kind="work.tool_call", subject=activity_id)
    predicate: dict[str, Any] = {
        "buildDefinition": {
            "buildType": WORK_BUILD_TYPE,
            "externalParameters": {"title": act.title, "kind": act.kind.value,
                                   "requirements": list(act.requirements),
                                   "params": act.params},
            "internalParameters": {"sandbox": sandbox[-1] if sandbox else None,
                                   "environment": act.environment},
            "resolvedDependencies": materials,
        },
        "runDetails": {
            "builder": {"id": WORK_BUILDER_ID + (f"/{act.actors[0]}" if act.actors else "")},
            "metadata": {k: v for k, v in {
                "invocationId": activity_id,
                "startedOn": _rfc3339(act.started_at),
                "finishedOn": _rfc3339(act.ended_at)}.items() if v is not None},
            "byproducts": [
                {"name": "air-ledger-record", "digest": {"sha256": finished[-1].hash},
                 "annotations": {"status": act.status.value, "outcome": act.outcome,
                                 "model_turns": len(model_turns),
                                 "tool_calls": len(tool_calls),
                                 "served_by": sorted({str(e.data.get("served_by"))
                                                      for e in model_turns}),
                                 "trace_id": act.trace_id}}] if finished else [],
        },
    }
    return statement(subjects, SLSA_PROVENANCE_V1, predicate)


# -- test runs ----------------------------------------------------------------------------
def evidence_statement(evidence: dict[str, Any], *, root: Path | None = None,
                          url: str | None = None) -> dict[str, Any]:
    """in-toto test-result/v0.1 statement from an AIR evidence document.

    ``passed`` -> passedTests; ``failed``/``error`` -> failedTests; ``skipped`` is not
    listed (the predicate has no such list) and makes the result WARNED, so a policy
    cannot mistake a partly skipped run for a complete pass."""
    tests = evidence.get("tests", [])
    run = evidence.get("run", {})
    passed = sorted(t["nodeid"] for t in tests if t.get("outcome") == "passed")
    failed = sorted(t["nodeid"] for t in tests if t.get("outcome") in ("failed", "error"))
    skipped = [t for t in tests if t.get("outcome") == "skipped"]
    broken_run = not tests and run.get("exitstatus", 0) not in (0, 5)
    result = "FAILED" if failed or broken_run else ("WARNED" if skipped else "PASSED")
    env = run.get("environment", {})
    subjects: list[dict[str, Any]] = []
    commit = (env.get("git") or {}).get("commit")
    if commit:
        subjects.append({"name": "source", "digest": {"gitCommit": commit},
                         "annotations": {"dirty": bool(env["git"].get("dirty"))}})
    else:
        subjects.append({"name": "test-evidence",
                         "digest": {"sha256": _sha256_hex(canonical_bytes(evidence))}})
    configuration: list[dict[str, Any]] = []
    if root is not None:
        for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"):
            p = root / name
            if p.is_file():
                configuration.append({"name": name,
                                      "digest": {"sha256": _sha256_hex(p.read_bytes())}})
                break
    fingerprints = run.get("requirement_fingerprints") or {}
    if fingerprints:
        configuration.append({
            "name": "air-requirement-fingerprints",
            "digest": {"sha256": _sha256_hex(canonical_bytes(fingerprints))},
            "annotations": {"requirements": sorted(fingerprints)}})
    if not configuration:
        configuration.append({"name": "pytest", "uri": "pkg:pypi/pytest"})
    predicate: dict[str, Any] = {"result": result, "configuration": configuration,
                                 "passedTests": passed, "warnedTests": [],
                                 "failedTests": failed}
    if url:
        predicate["url"] = url
    return statement(subjects, TEST_RESULT_V01, predicate)


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# -- DSSE ---------------------------------------------------------------------------------
def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE v1 Pre-Authentication Encoding."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def serialize_statement(doc: dict[str, Any]) -> bytes:
    return canonical_bytes(doc)


def dsse_sign(payload: bytes, signers: Iterable[Signer],
              payload_type: str = INTOTO_PAYLOAD_TYPE) -> dict[str, Any]:
    sigs = [{"keyid": f"{s.name}/{s.key_id.hex()}",
             "sig": base64.b64encode(s.sign(pae(payload_type, payload))).decode()}
            for s in signers]
    if not sigs:
        raise SchemaError("a DSSE envelope needs at least one signature")
    return {"payload": base64.b64encode(payload).decode(), "payloadType": payload_type,
            "signatures": sigs}


def dsse_verify(envelope: dict[str, Any], verifiers: Iterable[Verifier],
                payload_type: str = INTOTO_PAYLOAD_TYPE) -> bytes:
    """Return the verified payload bytes (never re-parse the envelope after this)."""
    if envelope.get("payloadType") != payload_type:
        raise SchemaError("unexpected DSSE payload type", got=envelope.get("payloadType"))
    raw = str(envelope.get("payload", ""))
    try:
        payload = base64.b64decode(raw, validate=True)
    except ValueError:
        payload = base64.urlsafe_b64decode(raw)
    message = pae(payload_type, payload)
    keys = list(verifiers)
    for sig in envelope.get("signatures", []):
        value = str(sig.get("sig", ""))
        try:
            signature = base64.b64decode(value, validate=True)
        except ValueError:
            signature = base64.urlsafe_b64decode(value)
        for k in keys:
            if k.verify(message, signature):
                return payload
    raise SchemaError("no DSSE signature verified with the given keys")


def write_attestation(project: Project, name: str, doc: dict[str, Any], *,
                      signers: Sequence[Signer] = (), out: Path | None = None,
                      subject: str = "") -> Path:
    """Write a statement (or, with signers, a DSSE envelope) and ledger its digest."""
    payload = serialize_statement(doc)
    if signers:
        body = json.dumps(dsse_sign(payload, signers), indent=2) + "\n"
        suffix = ".dsse.json"
    else:
        body = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        suffix = ".intoto.json"
    path = out or project.air_dir / ATTESTATION_DIR / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    project.ledger.append("attestation.created", subject or name, {
        "path": project._rel(path), "predicate_type": doc["predicateType"],
        "statement_sha256": _sha256_hex(payload), "signed": bool(signers),
        "subjects": [s.get("name", "") for s in doc["subject"]]})
    return path
