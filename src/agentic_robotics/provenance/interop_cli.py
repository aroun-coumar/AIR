"""``air`` commands for ledger integrity (checkpoints, proofs), attestations, exports,
recordings, run logs and reproducibility locks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .project import Project

__requirements__ = ("REQ10", "REQ16", "REQ18", "F05")

ProjFn = Callable[[argparse.ArgumentParser], argparse.ArgumentParser]


def _project(args: argparse.Namespace) -> Project:
    return Project.open(args.project)


def _key_args(p: argparse.ArgumentParser, verify: bool = False) -> None:
    p.add_argument("--hmac-key", metavar="FILE",
                   help="shared-secret key file (hex or raw bytes, >= 16 bytes)")
    if verify:
        p.add_argument("--vkey", action="append",
                       help="Ed25519 verifier key name+keyid+base64 (repeatable)")
    else:
        p.add_argument("--ed25519-key", metavar="FILE",
                       help="Ed25519 seed (64 hex digits); needs agentic-robotics[signing]")
    p.add_argument("--key-name", help="key name (default: the checkpoint origin)")


def _signers(args: argparse.Namespace, name: str) -> list[Any]:
    from .signing import Ed25519Signer, HmacKey

    out: list[Any] = []
    key_name = args.key_name or name
    if getattr(args, "hmac_key", None):
        out.append(HmacKey.from_file(key_name, args.hmac_key))
    if getattr(args, "ed25519_key", None):
        out.append(Ed25519Signer.from_file(key_name, args.ed25519_key))
    return out


def _verifiers(args: argparse.Namespace, name: str) -> list[Any]:
    from .signing import Ed25519PublicKey, HmacKey

    out: list[Any] = [Ed25519PublicKey.from_vkey(v) for v in getattr(args, "vkey", None) or []]
    if getattr(args, "hmac_key", None):
        out.append(HmacKey.from_file(args.key_name or name, args.hmac_key))
    return out


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")


# -- ledger --------------------------------------------------------------------------------
def cmd_checkpoint(args: argparse.Namespace) -> int:
    from .merkle import GitAnchor, create_checkpoint, default_origin

    p = _project(args)
    origin = args.origin or default_origin(p)
    signers = _signers(args, origin)
    path, ckpt = create_checkpoint(p, origin=origin, signers=signers,
                                   record=not args.no_record)
    print(f"checkpoint {path.relative_to(p.root).as_posix()}: size {ckpt.size}, "
          f"root {ckpt.root.hex()[:16]}")
    if not signers:
        print("UNSIGNED: git history is the only anchor; commit .air/checkpoints/")
    else:
        print("signed by " + ", ".join(f"{s.name} ({s.algorithm}, key id {s.key_id.hex()})"
                                      for s in signers))
    print("anchor: " + GitAnchor(p.root).anchor(path, ckpt).detail)
    return 0


def cmd_prove(args: argparse.Namespace) -> int:
    from .merkle import prove_entry

    doc = prove_entry(_project(args), args.seq, args.size)
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(f"entry {doc['seq']} is in the tree of {doc['tree_size']} entries "
              f"(root {doc['root'][:16]}); proof of {len(doc['inclusion_proof'])} hashes"
              f"{'' if doc['entry_recomputed_ok'] else ' -- ENTRY CONTENT DOES NOT HASH'}")
    return 0 if doc["entry_recomputed_ok"] else 1


def _verify_since(args: argparse.Namespace) -> int:
    from .merkle import Checkpoint, latest_checkpoint, verify_against

    p = _project(args)
    if args.since == "latest":
        latest = latest_checkpoint(p)
        if latest is None:
            print("no stored checkpoint (run `air ledger checkpoint`)", file=sys.stderr)
            return 2
        path, ckpt = latest
    else:
        path = Path(args.since)
        ckpt = Checkpoint.parse(path.read_text("utf-8"))
    report = verify_against(p, ckpt, _verifiers(args, ckpt.origin))
    if args.json:
        print(json.dumps({"checkpoint": str(path), **report.to_dict()}, indent=2))
    else:
        state = "OK" if report.ok else "BROKEN"
        print(f"ledger {state} against checkpoint {path.name} (size {ckpt.size}): "
              f"{report.entries} entries now")
        print(f"  {report.signature}")
        for n in report.notes:
            print(f"  note: {n}")
        for prob in report.problems:
            print(f"  PROBLEM: {prob}")
    return 0 if report.ok else 1


def cmd_keygen(args: argparse.Namespace) -> int:
    import secrets

    out = Path(args.out)
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 2
    try:
        Path(args.project).resolve()
        inside = out.resolve().is_relative_to(Path(args.project).resolve())
    except OSError:
        inside = False
    if args.type == "hmac":
        out.write_text(secrets.token_hex(32) + "\n", encoding="ascii")
        print(f"wrote HMAC key {out}")
    else:
        from .signing import Ed25519Signer

        seed = Ed25519Signer.generate_seed()
        signer = Ed25519Signer(args.name, seed)  # fails early without [signing]
        out.write_text(seed.hex() + "\n", encoding="ascii")
        print(f"wrote Ed25519 key {out}")
        print(f"verifier key: {signer.public.vkey()}")
    if inside:
        print("WARNING: the key is inside the project; never commit signing keys",
              file=sys.stderr)
    return 0


def add_ledger_commands(lgs: argparse._SubParsersAction[argparse.ArgumentParser],
                        verify: argparse.ArgumentParser, proj: ProjFn,
                        plain_verify: Callable[[argparse.Namespace], int]) -> None:
    verify.add_argument("--since", metavar="CHECKPOINT",
                        help="also check the ledger is an append-only extension of this "
                             "checkpoint file ('latest' = newest in .air/checkpoints)")
    verify.add_argument("--json", action="store_true")
    _key_args(verify, verify=True)
    verify.set_defaults(fn=lambda a: _verify_since(a) if a.since else plain_verify(a))

    c = proj(lgs.add_parser("checkpoint", help="write a signed RFC 9162 / C2SP checkpoint "
                                               "of the ledger to .air/checkpoints/"))
    c.add_argument("--origin", help="log origin line (default air.local/<project>)")
    c.add_argument("--no-record", action="store_true",
                   help="do not note the checkpoint in the ledger")
    _key_args(c)
    c.set_defaults(fn=cmd_checkpoint)

    pr = proj(lgs.add_parser("prove", help="inclusion proof for one ledger entry"))
    pr.add_argument("seq", type=int)
    pr.add_argument("--size", type=int, help="tree size (default: current)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(fn=cmd_prove)

    k = proj(lgs.add_parser("keygen", help="create a checkpoint/attestation signing key "
                                           "(store it OUTSIDE the repository)"))
    k.add_argument("--type", choices=["ed25519", "hmac"], default="ed25519")
    k.add_argument("--name", default="air.local/project", help="key name (Ed25519)")
    k.add_argument("--out", required=True)
    k.set_defaults(fn=cmd_keygen)


# -- attestations --------------------------------------------------------------------------
def _attest_name(p: Project) -> str:
    from .merkle import default_origin
    return default_origin(p)


def cmd_attest_work(args: argparse.Namespace) -> int:
    from .attest import work_statement, write_attestation

    p = _project(args)
    doc = work_statement(p, args.activity_id)
    path = write_attestation(p, args.activity_id, doc, signers=_signers(args, _attest_name(p)),
                             out=Path(args.out) if args.out else None,
                             subject=args.activity_id)
    print(f"wrote {path} ({len(doc['subject'])} subject(s), "
          f"{len(doc['predicate']['buildDefinition']['resolvedDependencies'])} material(s))")
    return 0


def cmd_attest_tests(args: argparse.Namespace) -> int:
    from .attest import evidence_statement, write_attestation

    p = _project(args)
    ev_path = Path(args.evidence) if args.evidence else p.air_dir / "test-evidence.json"
    if not ev_path.exists():
        print(f"no evidence at {ev_path} (run pytest --air-evidence ...)", file=sys.stderr)
        return 2
    doc = evidence_statement(json.loads(ev_path.read_text("utf-8")), root=p.root,
                             url=args.url)
    path = write_attestation(p, "test-result", doc, signers=_signers(args, _attest_name(p)),
                             out=Path(args.out) if args.out else None, subject="test-run")
    pred = doc["predicate"]
    print(f"wrote {path}: {pred['result']} ({len(pred['passedTests'])} passed, "
          f"{len(pred['failedTests'])} failed)")
    return 0


def cmd_attest_verify(args: argparse.Namespace) -> int:
    from .attest import check_statement, dsse_verify

    p = _project(args)
    doc = json.loads(Path(args.file).read_text("utf-8"))
    if "payloadType" in doc:
        verifiers = _verifiers(args, _attest_name(p))
        if not verifiers:
            print("DSSE envelope: give --vkey or --hmac-key to verify its signature",
                  file=sys.stderr)
            return 2
        statement = json.loads(dsse_verify(doc, verifiers))
        print("DSSE signature verified")
    else:
        statement = doc
        print("unsigned statement (no DSSE envelope)")
    problems = check_statement(statement)
    for prob in problems:
        print(f"PROBLEM: {prob}")
    print(f"{statement.get('predicateType')}: {len(statement.get('subject', []))} subject(s)")
    return 1 if problems else 0


# -- exports -------------------------------------------------------------------------------
def cmd_export_prov(args: argparse.Namespace) -> int:
    from .export_prov import to_prov_json

    _emit(json.dumps(to_prov_json(_project(args)), indent=2) + "\n", args.out)
    return 0


def cmd_export_otel(args: argparse.Namespace) -> int:
    from . import otel

    p = _project(args)
    genai = not args.no_genai
    if args.trace_file:
        rows = [json.loads(ln) for ln in Path(args.trace_file).read_text("utf-8").splitlines()
                if ln.strip()]
        spans = otel.spans_from_trace(rows, epoch_offset_ns=int(args.epoch_offset_s * 1e9),
                                      genai=genai)
    else:
        spans = otel.spans_from_project(p, genai=genai)
    request = otel.to_otlp(spans, service_name=args.service or p.name)
    if args.endpoint:
        status = otel.post_otlp(args.endpoint, request)
        print(f"posted {len(spans)} spans to {args.endpoint} (HTTP {status})")
    else:
        out = args.out or str(p.air_dir / "exports" / "traces.otlp.jsonl")
        otel.write_otlp_file(out, request)
        print(f"wrote {len(spans)} spans to {out}")
    return 0


def cmd_export_sdoc(args: argparse.Namespace) -> int:
    from .export_sdoc import to_sdoc
    from .traceability import TestEvidence, build_matrix, scan_code

    p = _project(args)
    reqs = p.requirements
    if reqs is None:
        print("project has no requirements.json", file=sys.stderr)
        return 2
    roots = [Path(s) for s in (args.source or ["src"])]
    code = scan_code([r if r.is_absolute() else p.root / r for r in roots], p.root)
    evidence = TestEvidence.load(Path(args.evidence) if args.evidence
                                 else p.air_dir / "test-evidence.json")
    counts: dict[str, int] = {}
    for a in p.activities():
        for r in a.requirements:
            counts[r] = counts.get(r, 0) + 1
    _emit(to_sdoc(reqs, build_matrix(reqs, code, evidence, counts), title=args.title),
          args.out)
    return 0


# -- recordings and run logs -----------------------------------------------------------------
def cmd_recording_register(args: argparse.Namespace) -> int:
    from .recording import register_recording

    ref = register_recording(_project(args), args.file, activity_id=args.activity,
                             trace_id=args.trace, robot_id=args.robot,
                             start_ns=args.start_ns, end_ns=args.end_ns,
                             media_type=args.media_type, store=args.store)
    print(f"registered {ref.entity_id} ({ref.attributes.get('format')}, {ref.digest})")
    return 0


def cmd_recording_show(args: argparse.Namespace) -> int:
    from .recording import is_mcap, read_mcap_summary
    from .runlog import is_runlog, summarize

    if is_mcap(args.file):
        print(json.dumps(read_mcap_summary(args.file).to_dict(), indent=2))
    elif is_runlog(args.file):
        print(json.dumps(summarize(args.file), indent=2))
    else:
        print("opaque file (neither MCAP nor an AIR run log)")
    return 0


def cmd_recording_verify(args: argparse.Namespace) -> int:
    from .project import file_digest

    p = _project(args)
    digest = file_digest(Path(args.file))
    known = [e for e in p.entities().values() if e.kind == "recording"]
    match = [e for e in known if e.digest == digest]
    if match:
        print(f"OK: matches {', '.join(e.entity_id for e in match)}")
        return 0
    rel = p._rel(Path(args.file))
    same_name = [e for e in known if e.uri == rel]
    if same_name:
        print(f"CHANGED: {rel} no longer matches the recorded digest "
              f"{same_name[-1].digest}")
    else:
        print(f"UNKNOWN: no recording with this digest is registered ({digest})")
    return 1


def cmd_repro(args: argparse.Namespace) -> int:
    from .repro import check, lock_activity

    p = _project(args)
    if args.action == "lock":
        entry = lock_activity(p, args.activity)
        print(f"locked {entry['activity_id']}: {len(entry['inputs'])} input(s), "
              f"{len(entry['outputs'])} output(s), "
              f"{len(entry['environment']['packages'])} packages -> .air/repro.lock")
        return 0
    if not args.check:
        print("use `air repro lock` or `air repro --check`", file=sys.stderr)
        return 2
    report = check(p, activity_id=args.activity)
    for c in report.changes:
        print(f"changed  {c}")
    print(f"{report.locked} locked activit{'y' if report.locked == 1 else 'ies'}: "
          + ("nothing changed" if report.ok else f"{len(report.changes)} change(s)"))
    return 0 if report.ok else 1


def add_interop_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser],
                         proj: ProjFn) -> None:
    at = sub.add_parser("attest", help="in-toto attestations of work runs and test runs")
    ats = at.add_subparsers(dest="action", required=True)
    aw = proj(ats.add_parser("work", help="SLSA provenance statement for an activity"))
    aw.add_argument("activity_id")
    aw.add_argument("--out")
    _key_args(aw)
    aw.set_defaults(fn=cmd_attest_work)
    att = proj(ats.add_parser("tests", help="in-toto test-result statement from evidence"))
    att.add_argument("--evidence", help="default .air/test-evidence.json")
    att.add_argument("--url", help="link to the CI run")
    att.add_argument("--out")
    _key_args(att)
    att.set_defaults(fn=cmd_attest_tests)
    av = proj(ats.add_parser("verify", help="check a statement or DSSE envelope"))
    av.add_argument("file")
    _key_args(av, verify=True)
    av.set_defaults(fn=cmd_attest_verify)

    ex = sub.add_parser("export", help="interoperable exports (PROV-JSON, OTLP, StrictDoc)")
    exs = ex.add_subparsers(dest="action", required=True)
    ep = proj(exs.add_parser("prov", help="W3C PROV-JSON of the project ledger"))
    ep.add_argument("--out")
    ep.set_defaults(fn=cmd_export_prov)
    eo = proj(exs.add_parser("otel", help="OTLP/JSON traces (file or OTLP/HTTP endpoint)"))
    eo.add_argument("--out", help="OTLP JSON-lines file (default .air/exports/)")
    eo.add_argument("--endpoint", help="OTLP/HTTP base URL, e.g. http://localhost:4318")
    eo.add_argument("--trace-file", help="runtime *.trace.jsonl instead of the ledger")
    eo.add_argument("--epoch-offset-s", type=float, default=0.0,
                    help="add to trace-file times to get Unix time")
    eo.add_argument("--no-genai", action="store_true",
                    help="omit GenAI semantic-convention names (Development status)")
    eo.add_argument("--service")
    eo.set_defaults(fn=cmd_export_otel)
    es = proj(exs.add_parser("sdoc", help="StrictDoc .sdoc of the requirement matrix"))
    es.add_argument("--out")
    es.add_argument("--title")
    es.add_argument("--evidence")
    es.add_argument("--source", action="append")
    es.set_defaults(fn=cmd_export_sdoc)

    rc = sub.add_parser("recording", help="recordings (MCAP, run logs, any file) as "
                                          "ledger entities")
    rcs = rc.add_subparsers(dest="action", required=True)
    rr = proj(rcs.add_parser("register"))
    rr.add_argument("file")
    rr.add_argument("--activity")
    rr.add_argument("--trace")
    rr.add_argument("--robot")
    rr.add_argument("--start-ns", type=int)
    rr.add_argument("--end-ns", type=int)
    rr.add_argument("--media-type")
    rr.add_argument("--store", action="store_true", help="copy into the artifact store")
    rr.set_defaults(fn=cmd_recording_register)
    rsh = rcs.add_parser("show", help="summarize an MCAP file or AIR run log")
    rsh.add_argument("file")
    rsh.set_defaults(fn=cmd_recording_show)
    rv = proj(rcs.add_parser("verify", help="does a file match its registered digest?"))
    rv.add_argument("file")
    rv.set_defaults(fn=cmd_recording_verify)

    rp = proj(sub.add_parser("repro", help="reproducibility lock: `lock`, or `--check`"))
    rp.add_argument("--check", action="store_true", help="report what changed since lock")
    rp.add_argument("--activity")
    rps = rp.add_subparsers(dest="action")
    rl = proj(rps.add_parser("lock", help="lock inputs and environment of an activity"))
    rl.add_argument("--activity")
    rl.set_defaults(fn=cmd_repro)
    rp.set_defaults(fn=cmd_repro)
