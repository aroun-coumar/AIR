"""Attestations, PROV-JSON, OTLP, StrictDoc, recordings, run logs and repro locks."""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agentic_robotics
from agentic_robotics.agents.providers import Location, ModelRouter, ScriptedChat, call, say
from agentic_robotics.cli import main
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.provenance import Project, otel
from agentic_robotics.provenance.attest import (
    SLSA_PROVENANCE_V1,
    TEST_RESULT_V01,
    check_statement,
    dsse_sign,
    dsse_verify,
    evidence_statement,
    pae,
    serialize_statement,
    work_statement,
)
from agentic_robotics.provenance.export_prov import to_prov_json
from agentic_robotics.provenance.export_sdoc import to_sdoc
from agentic_robotics.provenance.recording import (
    MCAP_MAGIC,
    air_metadata,
    read_mcap_summary,
    recordings,
    register_recording,
    verify_recording,
    write_mcap,
)
from agentic_robotics.provenance.repro import check, lock_activity
from agentic_robotics.provenance.runlog import (
    FieldDef,
    RunLogWriter,
    UnsupportedRunLog,
    read_runlog,
    register_runlog,
    summarize,
)
from agentic_robotics.provenance.signing import HmacKey
from agentic_robotics.provenance.traceability import (
    TestEvidence,
    baseline_requirements,
    build_matrix,
)
from agentic_robotics.work import LocalProcessSandbox, SandboxPolicy, WorkAgent, WorkKind, WorkTask

from .ed25519_ref import RefSigner

pytestmark = pytest.mark.req("REQ10", "REQ02")
KEY = HmacKey("air.local/proj", b"k" * 32)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path / "proj", requirements=baseline_requirements())


async def _work_run(project: Project) -> str:
    (project.root / "scenario.json").write_text('{"delay_ms": 200}')
    r = ModelRouter()
    r.register(ScriptedChat([  # type: ignore[arg-type]
        call("write_file", path="sim.py", content="print(0.97)\n"),
        call("run", argv=["python", "sim.py"]),
        call("propose", kind="experiment_result", title="Handoff", summary="0.97",
             files=["sim.py"]),
        say("done")], location=Location.LOCAL_HOST))

    def sandbox(policy: SandboxPolicy) -> LocalProcessSandbox:
        return LocalProcessSandbox(policy)

    task = WorkTask(task_id="sim-1", kind=WorkKind.SIMULATION, goal="secret goal text",
                    requirements=("REQ06",), inputs=("scenario.json",))
    result = await WorkAgent(project, r, sandbox_factory=sandbox).run(task)
    assert result.activity_id
    return result.activity_id


# -- in-toto / DSSE --------------------------------------------------------------------------
def test_dsse_pae_matches_the_spec_test_vector() -> None:
    assert pae("http://example.com/HelloWorld", b"hello world") == \
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"


@pytest.mark.req("REQ10", "F01", "G15")
async def test_work_run_becomes_a_slsa_provenance_statement(project: Project) -> None:
    aid = await _work_run(project)
    doc = work_statement(project, aid)
    assert doc["_type"] == "https://in-toto.io/Statement/v1"
    assert doc["predicateType"] == SLSA_PROVENANCE_V1 and not check_statement(doc)
    (subject,) = doc["subject"]
    assert subject["name"].endswith("sim.py") and len(subject["digest"]["sha256"]) == 64
    deps = doc["predicate"]["buildDefinition"]["resolvedDependencies"]
    assert [d["name"] for d in deps] == ["scenario.json"] and deps[0]["digest"]["sha256"]
    run = doc["predicate"]["runDetails"]
    assert run["metadata"]["invocationId"] == aid
    assert run["byproducts"][0]["annotations"]["tool_calls"] == 3
    assert doc["predicate"]["buildDefinition"]["internalParameters"]["sandbox"]["isolation"]

    payload = serialize_statement(doc)
    env = dsse_sign(payload, [KEY])
    assert env["payloadType"] == "application/vnd.in-toto+json"
    assert dsse_verify(env, [KEY]) == payload
    bad = {**env, "payload": base64.b64encode(payload.replace(b"sim.py", b"sim.pz")).decode()}
    with pytest.raises(SchemaError, match="no DSSE signature"):
        dsse_verify(bad, [KEY])
    ed = RefSigner("air.local/proj", b"\x05" * 32)
    assert dsse_verify(dsse_sign(payload, [ed]), [ed.public]) == payload


def test_test_evidence_becomes_an_in_toto_test_result(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    evidence = {"run": {"exitstatus": 1, "environment": {"git": {"commit": "a" * 40,
                                                                "dirty": False}},
                        "requirement_fingerprints": {"REQ01": "sha256:00"}},
                "tests": [{"nodeid": "t::a", "outcome": "passed"},
                          {"nodeid": "t::b", "outcome": "failed"},
                          {"nodeid": "t::c", "outcome": "skipped"}]}
    doc = evidence_statement(evidence, root=tmp_path, url="https://ci.example/run/1")
    assert doc["predicateType"] == TEST_RESULT_V01 and not check_statement(doc)
    pred = doc["predicate"]
    assert pred["result"] == "FAILED" and pred["failedTests"] == ["t::b"]
    assert pred["passedTests"] == ["t::a"] and pred["url"] == "https://ci.example/run/1"
    assert doc["subject"][0]["digest"] == {"gitCommit": "a" * 40}
    assert pred["configuration"][0]["name"] == "pyproject.toml"
    evidence["tests"] = [t for t in evidence["tests"] if t["outcome"] != "failed"]
    assert evidence_statement(evidence)["predicate"]["result"] == "WARNED"
    evidence["tests"] = evidence["tests"][:1]
    assert evidence_statement(evidence)["predicate"]["result"] == "PASSED"


@pytest.mark.req("REQ10", "REQ18")
def test_pytest_plugin_writes_the_attestation(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_x.py").write_text(
        "import pytest\n\n@pytest.mark.req('REQ01')\ndef test_ok():\n    assert True\n")
    src = str(Path(agentic_robotics.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suite),
         "--rootdir", str(suite), "--air-evidence", str(tmp_path / "ev.json"),
         "--air-attest", str(tmp_path / "run.intoto.json")],
        capture_output=True, text=True, env=env, cwd=suite, timeout=120, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    doc = json.loads((tmp_path / "run.intoto.json").read_text())
    assert doc["predicate"]["passedTests"] == ["test_x.py::test_ok"]
    assert doc["predicate"]["result"] == "PASSED" and not check_statement(doc)


# -- PROV-JSON -------------------------------------------------------------------------------
@pytest.mark.req("REQ10", "F07")
def test_prov_json_export_maps_activities_entities_and_relations(project: Project) -> None:
    (project.root / "in.txt").write_text("x")
    (project.root / "out.txt").write_text("y")
    with project.activity("simulation", "run", actors=("alice",)) as act:
        src = act.use("in.txt")
        act.generate("out.txt", derived_from=[src])
    doc = to_prov_json(project)
    assert set(doc["prefix"]) >= {"air", "ent", "act", "agt"}
    aid = f"act:{act.activity_id}"
    assert doc["activity"][aid]["prov:startTime"]["type"] == "xsd:dateTime"
    assert any(e["prov:label"] == "in.txt" for e in doc["entity"].values())
    assert [r["prov:activity"] for r in doc["used"].values()] == [aid]
    assert [r["prov:activity"] for r in doc["wasGeneratedBy"].values()] == [aid]
    (derived,) = doc["wasDerivedFrom"].values()
    assert derived["prov:usedEntity"].startswith("ent:input:in.txt@")
    assert doc["agent"]["agt:alice"] and next(iter(doc["wasAssociatedWith"].values()))[
        "prov:agent"] == "agt:alice"
    assert "params" not in json.dumps(doc["activity"])



@pytest.mark.req("REQ10", "F07")
def test_prov_json_is_read_by_the_prov_package(project: Project) -> None:
    prov = pytest.importorskip("prov.model")
    (project.root / "in.txt").write_text("x")
    with project.activity("simulation", "run", actors=("alice",)) as act:
        act.generate(b"o", kind="result", name="out", derived_from=[act.use("in.txt")])
    parsed = prov.ProvDocument.deserialize(content=json.dumps(to_prov_json(project)),
                                           format="json")
    assert len(parsed.get_records()) >= 6
    assert "wasDerivedFrom" in parsed.get_provn()


# -- OpenTelemetry ---------------------------------------------------------------------------
@pytest.mark.req("REQ15", "REQ18", "REQ02")
async def test_otlp_export_of_a_work_run_uses_w3c_ids_and_pinned_genai_names(
        project: Project) -> None:
    aid = await _work_run(project)
    spans = otel.spans_from_project(project)
    by_name = {s.name: s for s in spans}
    root = next(s for s in spans if s.attributes.get("air.activity.id") == aid)
    assert root.name.startswith("invoke_agent ") and len(root.trace_id) == 32
    chats = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat"]
    tools = [s for s in spans if s.name.startswith("execute_tool ")]
    assert len(chats) == 4 and len(tools) == 3
    assert all(s.parent_span_id == root.span_id and s.trace_id == root.trace_id
               for s in chats + tools)
    assert "gen_ai.request.model" in chats[0].attributes
    assert by_name["execute_tool run"].attributes["gen_ai.tool.name"] == "run"
    request = otel.to_otlp(spans, service_name="proj")
    text = json.dumps(request)
    assert "secret goal text" not in text and "print(0.97)" not in text
    span0 = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert isinstance(span0["startTimeUnixNano"], str) and span0["kind"] in (1, 3)
    plain = otel.to_otlp(otel.spans_from_project(project, genai=False))
    assert "gen_ai." not in json.dumps(plain)



@pytest.mark.req("REQ15", "REQ18")
async def test_otlp_json_parses_into_the_official_protobuf(project: Project) -> None:
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from google.protobuf import json_format
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    await _work_run(project)
    spans = otel.spans_from_project(project)
    # OTLP/JSON deliberately encodes trace/span ids as hex, not proto3-JSON base64; the
    # generic protobuf parser expects base64, so convert just those before parsing.
    request = otel.to_otlp(spans)
    for span in request["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        for key in ("traceId", "spanId", "parentSpanId"):
            if key in span:
                span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    msg = json_format.Parse(json.dumps(request), trace_service_pb2.ExportTraceServiceRequest())
    parsed = msg.resource_spans[0].scope_spans[0].spans
    assert len(parsed) == len(spans)
    assert {bytes(p.trace_id).hex() for p in parsed} == {s.trace_id for s in spans}


@pytest.mark.req("REQ15", "REQ02")
def test_runtime_trace_spans_keep_unresolved_distinct_from_failed(tmp_path: Path) -> None:
    t, s1, s2, s3 = "ab" * 16, "1" * 16, "2" * 16, "3" * 16
    events = [
        {"seq": 0, "t": 1.0, "clock_domain": "sim", "kind": "agent_decision",
         "subject": "planner", "data": {"outcome": "accepted"}, "trace_id": t,
         "span_id": s1},
        {"seq": 1, "t": 1.5, "clock_domain": "sim", "kind": "submitted", "subject": "cmd-1",
         "trace_id": t, "span_id": s2, "parent_id": s1},
        {"seq": 2, "t": 2.0, "clock_domain": "sim", "kind": "unresolved",
         "subject": "cmd-1", "trace_id": t, "span_id": s2, "parent_id": s1},
        {"seq": 3, "t": 2.5, "clock_domain": "sim", "kind": "submitted", "subject": "cmd-2",
         "trace_id": t, "span_id": s3},
        {"seq": 4, "t": 3.0, "clock_domain": "sim", "kind": "failed", "subject": "cmd-2",
         "trace_id": t, "span_id": s3},
        {"seq": 5, "t": 3.0, "clock_domain": "sim", "kind": "progress", "subject": "x"},
    ]
    spans = {s.span_id: s for s in otel.spans_from_trace(events, epoch_offset_ns=10**18)}
    assert spans[s1].name == "plan planner" and spans[s1].attributes[
        "gen_ai.operation.name"] == "plan"
    assert spans[s2].parent_span_id == s1 and spans[s2].attributes["air.outcome"] == \
        "unresolved"
    assert spans[s2].status_code == otel.STATUS_UNSET
    assert spans[s3].status_code == otel.STATUS_ERROR
    assert spans[s2].start_ns == 10**18 + 1_500_000_000
    assert len(spans) == 3

    posted: list[tuple[str, bytes, dict[str, str]]] = []

    def poster(url: str, body: bytes, headers: dict[str, str]) -> int:
        posted.append((url, body, headers))
        return 200

    req = otel.to_otlp(list(spans.values()))
    assert otel.post_otlp("http://collector:4318", req, poster=poster) == 200
    assert posted[0][0] == "http://collector:4318/v1/traces"
    assert posted[0][2]["Content-Type"] == "application/json"
    with pytest.raises(SchemaError, match="rejected"):
        otel.post_otlp("http://collector:4318/v1/traces", req, poster=lambda *a: 503)
    path = otel.write_otlp_file(tmp_path / "t.jsonl", req)
    otel.write_otlp_file(path, req, append=True)
    assert len(path.read_text().splitlines()) == 2


def test_genai_names_live_in_one_pinned_table() -> None:
    assert otel.GENAI_SEMCONV["status"] == "development"
    assert "semantic-conventions-genai@" in otel.GENAI_SEMCONV["pin"]
    assert set(otel.GENAI_SEMCONV["attributes"].values()) >= {
        "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.usage.input_tokens"}


# -- StrictDoc -------------------------------------------------------------------------------
@pytest.mark.req("F01", "REQ18")
def test_strictdoc_export_of_the_matrix(project: Project) -> None:
    reqs = baseline_requirements()
    rows = build_matrix(reqs, {"REQ01": ["m"]}, TestEvidence(
        tests=[{"nodeid": "t::a", "outcome": "passed", "requirements": ["REQ01"]}]))
    text = to_sdoc(reqs, rows)
    assert text.startswith("[DOCUMENT]\nTITLE: ")
    assert "[GRAMMAR]\nELEMENTS:\n- TAG: REQUIREMENT\n" in text
    assert "UID: REQ01\nSTATUS: " in text and "AIR_EVIDENCE: evidenced; 1 passing" in text
    traced = next(r for r in reqs.requirements if r.traces_to)
    block = text.split(f"UID: {traced.req_id}\n", 1)[1].split("[REQUIREMENT]", 1)[0]
    assert f"RELATIONS:\n- TYPE: Parent\n  VALUE: {traced.traces_to[0]}\n" in block
    assert main(["export", "sdoc", "--project", str(project.root), "--out",
                 str(project.root / "reqs.sdoc")]) == 0



@pytest.mark.req("F01", "REQ18")
def test_strictdoc_parses_the_export() -> None:
    reader = pytest.importorskip("strictdoc.backend.sdoc.reader")
    reqs = baseline_requirements()
    doc = reader.SDReader().read(to_sdoc(reqs), file_path="reqs.sdoc")
    nodes = list(doc.section_contents)
    assert len(nodes) == len(reqs.requirements)
    traced = [n for n in nodes if getattr(n, "relations", None)]
    assert traced and traced[0].relations[0].ref_type == "Parent"


# -- recordings ------------------------------------------------------------------------------
def _record(op: int, body: bytes) -> bytes:
    return bytes([op]) + struct.pack("<Q", len(body)) + body


def _s(text: str) -> bytes:
    return struct.pack("<I", len(text)) + text.encode()


@pytest.mark.req("REQ02", "F05", "F07")
def test_mcap_recording_is_read_and_registered_with_its_digest(project: Project) -> None:
    with project.activity("execution", "drive") as act:
        pass
    md = air_metadata(project, activity_id=act.activity_id, trace_id="cd" * 16,
                      robot_id="carrier-1")
    path = write_mcap(project.root / "run.mcap", metadata={"air": md},
                      attachments=[("scenario.json", "application/json", b"{}", 5)])
    summary = read_mcap_summary(path)
    assert summary.metadata["air"]["activity_id"] == act.activity_id
    assert summary.attachments[0]["name"] == "scenario.json" and summary.library
    ref = register_recording(project, path)
    assert ref.kind == "recording" and ref.digest and ref.digest.startswith("sha256:")
    assert ref.attributes["activity_id"] == act.activity_id
    assert ref.attributes["robot_id"] == "carrier-1" and ref.attributes["format"] == "mcap"
    assert recordings(project, activity_id=act.activity_id) == [ref]
    assert project.ledger.query(kind="recording.linked")[0].data["digest"] == ref.digest
    assert project.lineage(ref.entity_id)["generated_by"][0]["activity"] == act.activity_id
    assert verify_recording(project, ref, path)
    path.write_bytes(path.read_bytes().replace(b"carrier-1", b"carrier-2"))
    assert not verify_recording(project, ref, path)



@pytest.mark.req("REQ02", "F07")
def test_mcap_package_reads_the_minimal_writer_output(tmp_path: Path) -> None:
    pytest.importorskip("mcap.reader")
    from mcap.reader import make_reader

    path = write_mcap(tmp_path / "x.mcap", metadata={"air": {"activity_id": "act_1"}},
                      attachments=[("cfg.json", "application/json", b"{}", 7)])
    with path.open("rb") as fh:
        reader = make_reader(fh)
        metas = [(m.name, dict(m.metadata)) for m in reader.iter_metadata()]
    with path.open("rb") as fh:
        atts = [(a.name, a.media_type, a.data) for a in make_reader(fh).iter_attachments()]
    assert metas == [("air", {"activity_id": "act_1"})]
    assert atts == [("cfg.json", "application/json", b"{}")]


def test_mcap_reader_counts_messages_and_rejects_truncated_files(tmp_path: Path) -> None:
    body = bytearray(MCAP_MAGIC)
    body += _record(0x01, _s("ros2") + _s("test"))
    body += _record(0x04, struct.pack("<HH", 1, 0) + _s("/imu") + _s("json") +
                    struct.pack("<I", 0))
    for t in (100, 300, 200):
        body += _record(0x05, struct.pack("<HIQQ", 1, 0, t, t) + b"{}")
    body += _record(0x0F, struct.pack("<I", 0))
    body += _record(0x02, struct.pack("<QQI", 0, 0, 0)) + MCAP_MAGIC
    path = tmp_path / "m.mcap"
    path.write_bytes(bytes(body))
    s = read_mcap_summary(path)
    assert (s.profile, s.message_count, s.start_ns, s.end_ns) == ("ros2", 3, 100, 300)
    assert s.to_dict()["topics"] == ["/imu"]
    path.write_bytes(bytes(body[:-20]))
    with pytest.raises(SchemaError, match="trailing magic"):
        read_mcap_summary(path)
    plain = tmp_path / "video.mp4"
    plain.write_bytes(b"\x00" * 10)
    project = Project.init(tmp_path / "p")
    ref = register_recording(project, plain, robot_id="r1", start_ns=5, end_ns=9)
    assert ref.attributes["format"] == "opaque" and ref.attributes["media_type"] == "video/mp4"
    with pytest.raises(SchemaError, match="ends before"):
        register_recording(project, plain, start_ns=9, end_ns=5)


# -- run logs --------------------------------------------------------------------------------
@pytest.mark.req("REQ02", "F05")
def test_run_log_is_self_describing_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "run.airlog"
    fmt = {"imu": (FieldDef("ax", "f64", "m/s^2"), FieldDef("ok", "bool"))}
    with RunLogWriter(path, fmt, parameters={"rate_hz": 100},
                      features=("sync_markers", "dropout_markers", "param_updates"),
                      optional_features=("vendor_blob",),
                      info={"robot_id": "r1", "activity_id": "act_x"}) as w:
        w.write(10, "imu", [0.5, True])
        w.sync(20, 1)
        w.dropout(30, 1000, "imu")
        w.param(40, "rate_hz", 50)
        w.extension(45, "vendor_blob", {"raw": "xx"})
        w.write(50, "imu", [0.6, False])
        with pytest.raises(SchemaError, match="backwards"):
            w.write(1, "imu", [0.1, True])
        with pytest.raises(SchemaError, match="field type"):
            w.write(60, "imu", ["x", True])
        with pytest.raises(SchemaError, match="undeclared"):
            w.write(60, "gps", [1.0])
    header, recs = read_runlog(path)
    kinds = [r.kind for r in recs]
    assert kinds == ["data", "sync", "dropout", "param", "data"]
    assert header.formats["imu"][0].unit == "m/s^2" and header.parameters == {"rate_hz": 100}
    info = summarize(path)
    assert info["messages"] == {"imu": 2} and info["dropout_ns"] == 1000

    w2 = RunLogWriter(tmp_path / "n.airlog", fmt)
    with pytest.raises(SchemaError, match="declare the feature"):
        w2.sync(1, 1)
    w2.close()

    future = tmp_path / "future.airlog"
    lines = path.read_text().splitlines()
    head = json.loads(lines[0])
    head["required_features"].append("compressed_frames")
    future.write_text("\n".join([json.dumps(head), *lines[1:]]) + "\n")
    with pytest.raises(UnsupportedRunLog, match="refusing to guess"):
        read_runlog(future)
    unknown_opt = tmp_path / "x.airlog"
    head2 = json.loads(lines[0])
    head2["optional_features"] = []
    unknown_opt.write_text("\n".join([json.dumps(head2), *lines[1:]]) + "\n")
    with pytest.raises(UnsupportedRunLog, match="undeclared feature"):
        list(read_runlog(unknown_opt)[1])

    project = Project.init(tmp_path / "p")
    ref = register_runlog(project, path)
    assert ref.attributes["format"] == "air-runlog" and ref.attributes["start_ns"] == 10
    assert ref.attributes["activity_id"] == "act_x"
    assert ref.attributes["runlog_summary"]["dropout_ns"] == 1000


# -- reproducibility lock --------------------------------------------------------------------
@pytest.mark.req("F05", "REQ02")
def test_repro_lock_reports_what_changed(project: Project,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    (project.root / "params.yaml").write_text("gain: 1\n")
    with project.activity("simulation", "tune") as act:
        act.use("params.yaml")
        act.generate(b"result", kind="result", name="res")
    env: dict[str, Any] = {"python": "3.12.0", "implementation": "CPython",
                           "platform": "p", "machine": "m", "framework_version": "0.1",
                           "git": {"commit": "a" * 40, "dirty": False},
                           "packages": {"numpy": "2.0.0", "pytest": "8.0"}}
    entry = lock_activity(project, act.activity_id, env=env)
    assert next(iter(entry["inputs"].values()))["uri"] == "params.yaml"
    assert check(project, env=env).ok
    (project.root / "params.yaml").write_text("gain: 2\n")
    changed = dict(env, python="3.13.1", packages={"numpy": "2.1.0", "torch": "2.5"},
                   git={"commit": "b" * 40, "dirty": True})
    report = check(project, env=changed)
    text = "\n".join(str(c) for c in report.changes)
    for needle in ("input changed: params.yaml", "python: 3.12.0 -> 3.13.1",
                   "numpy: 2.0.0 -> 2.1.0", "torch: absent -> 2.5", "pytest: 8.0 -> absent",
                   "git commit", "uncommitted"):
        assert needle in text
    assert project.ledger.query(kind="repro.locked")
    root = str(project.root)
    (project.root / "params.yaml").write_text("gain: 1\n")  # what the activity used
    assert main(["repro", "lock", "--project", root]) == 0
    assert main(["repro", "--check", "--project", root]) == 0
    assert "nothing changed" in capsys.readouterr().out
    (project.root / "params.yaml").write_text("gain: 3\n")
    assert main(["repro", "--check", "--project", root]) == 1


# -- CLI -------------------------------------------------------------------------------------
@pytest.mark.req("REQ18", "REQ10")
async def test_cli_attest_export_and_recording(project: Project, tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    aid = await _work_run(project)
    root = str(project.root)
    key = tmp_path / "k.hmac"
    key.write_text("ab" * 32)
    assert main(["attest", "work", aid, "--project", root, "--hmac-key", str(key)]) == 0
    envelope = next((project.air_dir / "attestations").glob("*.dsse.json"))
    assert main(["attest", "verify", str(envelope), "--project", root,
                 "--hmac-key", str(key)]) == 0
    assert "DSSE signature verified" in capsys.readouterr().out
    assert project.ledger.query(kind="attestation.created")[0].data["signed"] is True

    (project.air_dir / "test-evidence.json").write_text(json.dumps(
        {"run": {"exitstatus": 0, "environment": {}},
         "tests": [{"nodeid": "a", "outcome": "passed", "requirements": []}]}))
    assert main(["attest", "tests", "--project", root]) == 0
    stmt = project.air_dir / "attestations" / "test-result.intoto.json"
    assert main(["attest", "verify", str(stmt), "--project", root]) == 0

    assert main(["export", "prov", "--project", root, "--out", str(tmp_path / "p.json")]) == 0
    assert "activity" in json.loads((tmp_path / "p.json").read_text())
    assert main(["export", "otel", "--project", root]) == 0
    otlp = project.air_dir / "exports" / "traces.otlp.jsonl"
    assert json.loads(otlp.read_text().splitlines()[0])["resourceSpans"]

    log = project.root / "drive.airlog"
    with RunLogWriter(log, {"odom": (FieldDef("x"),)}) as w:
        w.write(1, "odom", [0.0])
    assert main(["recording", "register", str(log), "--project", root, "--robot", "r1",
                 "--activity", aid]) == 0
    assert main(["recording", "show", str(log)]) == 0
    assert main(["recording", "verify", str(log), "--project", root]) == 0
    capsys.readouterr()
    log.write_text(log.read_text() + '{"t":2,"m":"odom","v":[1.0]}\n')
    assert main(["recording", "verify", str(log), "--project", root]) == 1
    assert "CHANGED" in capsys.readouterr().out
