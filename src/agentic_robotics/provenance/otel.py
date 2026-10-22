"""OpenTelemetry trace export without the OpenTelemetry SDK.

Builds OTLP/JSON (``ExportTraceServiceRequest`` in the protobuf JSON mapping: lowerCamel
field names, hex trace/span ids, integer enums, 64-bit integers as decimal strings)
from what AIR already records:

* **activities** in the project ledger: one span each, using the activity's own W3C
  trace and span ids;
* **work runs**: model turns (``work.model_turn``) as GenAI inference spans and tool
  calls (``work.tool_call``) as ``execute_tool`` spans, children of the activity span;
* **runtime events** copied into the ledger (``runtime.*``) or read from a runtime
  ``*.trace.jsonl``: grouped by span id, agent decisions as ``plan`` spans, commands as
  ``air.command`` spans, each event a span event.

Output goes to a file (OTLP file-exporter JSON lines: one request per line) or to any
OTLP/HTTP endpoint (``/v1/traces``, ``application/json``) through an injectable poster,
so MLflow, Jaeger, Tempo or a Collector can ingest it. Exports are lossy views; the
ledger stays the source of truth, and an export failure never blocks recording.

**GenAI semantic conventions are Development status.** Every GenAI name AIR emits is in
:data:`GENAI_SEMCONV`, pinned to the commit of ``open-telemetry/semantic-conventions-genai``
it was checked against, and ``genai=False`` turns them all off (only ``air.*``
attributes remain). Prompts, goals, tool arguments and outputs are never exported.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:
    from .project import Project

__requirements__ = ("REQ15", "REQ18", "REQ02")

# The one place that knows GenAI semantic-convention names. Checked 2026-10-22 against
# open-telemetry/semantic-conventions-genai docs/gen-ai/{client-inference,gen-ai-spans,
# gen-ai-agent-spans}.md at commit 9ffc7e00c639be7712e544e3e54331e885dccf84 (no release
# tag existed yet; all names are Development status). Update deliberately, with a test.
GENAI_SEMCONV: dict[str, Any] = {
    "pin": "semantic-conventions-genai@9ffc7e00c639be7712e544e3e54331e885dccf84",
    "status": "development",
    "attributes": {
        "operation": "gen_ai.operation.name",
        "provider": "gen_ai.provider.name",
        "request_model": "gen_ai.request.model",
        "response_model": "gen_ai.response.model",
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
        "finish_reasons": "gen_ai.response.finish_reasons",
        "agent_name": "gen_ai.agent.name",
        "tool_name": "gen_ai.tool.name",
        "tool_call_id": "gen_ai.tool.call.id",
    },
    "operations": {"inference": "chat", "tool": "execute_tool", "plan": "plan",
                   "agent": "invoke_agent"},
    # span name formats from the conventions
    "names": {"inference": "{operation} {model}", "tool": "execute_tool {tool}",
              "plan": "plan {agent}", "agent": "invoke_agent {agent}"},
}

SPAN_KIND = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2
ERROR_KINDS = frozenset({"failed", "rejected", "adapter_error", "deadline_exceeded"})
TERMINAL_KINDS = ("verified", "failed", "unresolved", "cancelled", "rejected")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


@dataclass
class Span:
    trace_id: str
    span_id: str
    name: str
    start_ns: int
    end_ns: int
    parent_span_id: str | None = None
    kind: str = "internal"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)
    status_code: int = STATUS_UNSET
    status_message: str = ""


def _trace_id(value: str | None) -> str | None:
    if not value:
        return None
    if _HEX32.match(value) and value != "0" * 32:
        return value
    m = re.fullmatch(r"trc_([0-9a-f]{16})", value)
    return m.group(1).rjust(32, "0") if m else None


def _span_id(value: str | None) -> str | None:
    if not value:
        return None
    if _HEX16.match(value) and value != "0" * 16:
        return value
    m = re.fullmatch(r"spn_([0-9a-f]{16})", value)
    return m.group(1) if m else None


def derived_span_id(*parts: object) -> str:
    """Deterministic span id for records that have none of their own."""
    h = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return h if h != "0" * 16 else "0" * 15 + "1"


def _genai(attrs: dict[str, Any], key: str, value: Any, enabled: bool) -> None:
    if enabled and value is not None:
        attrs[GENAI_SEMCONV["attributes"][key]] = value


# -- spans from a project ledger -------------------------------------------------------------
def spans_from_project(project: Project, *, genai: bool = True,
                       include_runtime: bool = True) -> list[Span]:
    spans: list[Span] = []
    entries = list(project.ledger)
    started = {e.subject: e for e in entries if e.kind == "activity.started"}
    work_runs = {e.subject for e in entries if e.kind == "work.sandbox"}
    acts = {a.activity_id: a for a in project.activities()}
    span_of: dict[str, tuple[str, str]] = {}
    for aid, st in started.items():
        tid, sid = _trace_id(st.trace_id), _span_id(st.span_id)
        if tid and sid:
            span_of[aid] = (tid, sid)
    for aid, st in started.items():
        if aid not in span_of:
            continue
        tid, sid = span_of[aid]
        act = acts.get(aid)
        end_ts = act.ended_at if act and act.ended_at else st.ts
        attrs: dict[str, Any] = {"air.activity.id": aid, "air.activity.kind": st.data["kind"],
                                 "air.activity.status": act.status.value if act else "running"}
        if st.data.get("requirements"):
            attrs["air.requirements"] = list(st.data["requirements"])
        actors = list(st.data.get("actors", []))
        if aid in work_runs:
            name = GENAI_SEMCONV["names"]["agent"].format(agent=actors[0] if actors
                                                          else "agent") if genai \
                else f"air.work {st.data['kind']}"
            _genai(attrs, "operation", GENAI_SEMCONV["operations"]["agent"], genai)
            _genai(attrs, "agent_name", actors[0] if actors else None, genai)
        else:
            name = f"air.activity {st.data['kind']}"
        parent = act.parent_id if act else st.data.get("parent_id")
        parent_span = span_of[parent][1] if parent in span_of and \
            span_of[parent][0] == tid else None
        span = Span(tid, sid, name, st.ts_ns or int(st.ts * 1e9), int(end_ts * 1e9),
                    parent_span_id=parent_span, attributes=attrs)
        if act is not None and act.status.value == "failed":
            span.status_code, span.status_message = STATUS_ERROR, (act.error or "")[:200]
        spans.append(span)

    for e in entries:
        if e.kind not in ("work.model_turn", "work.tool_call") or e.subject not in span_of:
            continue
        tid, parent_sid = span_of[e.subject]
        end_ns = e.ts_ns or int(e.ts * 1e9)
        attrs = {"air.ledger.seq": e.seq}
        if e.kind == "work.model_turn":
            profile = e.data.get("model") or {}
            op = GENAI_SEMCONV["operations"]["inference"]
            model = profile.get("model") or e.data.get("served_by") or "model"
            name = GENAI_SEMCONV["names"]["inference"].format(operation=op, model=model) \
                if genai else f"air.model_turn {model}"
            _genai(attrs, "operation", op, genai)
            _genai(attrs, "provider", profile.get("provider"), genai)
            _genai(attrs, "request_model", profile.get("model"), genai)
            _genai(attrs, "response_model", e.data.get("served_by"), genai)
            usage = e.data.get("usage") or {}
            _genai(attrs, "input_tokens", usage.get("input_tokens"), genai)
            _genai(attrs, "output_tokens", usage.get("output_tokens"), genai)
            _genai(attrs, "finish_reasons", [e.data["stop"]] if e.data.get("stop") else None,
                   genai)
            attrs["air.model.location"] = profile.get("location")
            attrs["air.timing"] = "end_only"  # the ledger records when the turn returned
            kind = "client" if profile.get("data_leaves_device") else "internal"
            span = Span(tid, derived_span_id(tid, e.seq), name, end_ns, end_ns,
                        parent_span_id=parent_sid, kind=kind, attributes=attrs)
        else:
            tool = str(e.data.get("tool"))
            name = GENAI_SEMCONV["names"]["tool"].format(tool=tool) if genai \
                else f"air.tool {tool}"
            _genai(attrs, "operation", GENAI_SEMCONV["operations"]["tool"], genai)
            _genai(attrs, "tool_name", tool, genai)
            _genai(attrs, "tool_call_id", e.data.get("call_id"), genai)
            attrs["air.tool.side_effect"] = e.data.get("side_effect")
            duration_ns = int(float(e.data.get("duration_s") or 0) * 1e9)
            span = Span(tid, derived_span_id(tid, e.seq), name, end_ns - duration_ns, end_ns,
                        parent_span_id=parent_sid, attributes=attrs)
            if not e.data.get("ok", True):
                span.status_code = STATUS_ERROR
        span.attributes = {k: v for k, v in span.attributes.items() if v is not None}
        spans.append(span)

    if include_runtime:
        rows = [(e.kind.removeprefix("runtime."), e.subject, e.ts_ns or int(e.ts * 1e9),
                 e.trace_id, e.span_id, None, e.seq, e.data)
                for e in entries if e.kind.startswith("runtime.")]
        by_id = {(s.trace_id, s.span_id): s for s in spans}
        for rs in _runtime_spans(rows, genai):
            host = by_id.get((rs.trace_id, rs.span_id))
            if host is None:  # runtime events inside an activity span join that span
                spans.append(rs)
                by_id[(rs.trace_id, rs.span_id)] = rs
            else:
                host.events.extend(rs.events)
                if rs.status_code == STATUS_ERROR and host.status_code == STATUS_UNSET:
                    host.status_code, host.status_message = rs.status_code, rs.status_message
    return spans


# -- spans from runtime trace events ---------------------------------------------------------
def spans_from_trace(events: Iterable[Any], *, epoch_offset_ns: int = 0,
                     genai: bool = True) -> list[Span]:
    """Runtime :class:`~agentic_robotics.runtime.trace.TraceEvent` objects (or dicts).
    Their ``t`` is in the clock's own domain; ``epoch_offset_ns`` maps it to Unix time
    (``air.clock_domain`` records which domain it came from)."""
    rows = []
    for ev in events:
        d = ev if isinstance(ev, dict) else ev.to_dict()
        rows.append((d["kind"], d["subject"], int(float(d["t"]) * 1e9) + epoch_offset_ns,
                     d.get("trace_id"), d.get("span_id"), d.get("parent_id"), d["seq"],
                     {**(d.get("data") or {}), "clock_domain": d.get("clock_domain")}))
    return _runtime_spans(rows, genai)


def _runtime_spans(rows: Sequence[tuple[str, str, int, str | None, str | None, str | None,
                                        int, dict[str, Any]]], genai: bool) -> list[Span]:
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        tid, sid = _trace_id(row[3]), _span_id(row[4])
        if tid and sid:
            groups.setdefault((tid, sid), []).append(row)
    spans = []
    for (tid, sid), items in groups.items():
        items.sort(key=lambda r: (r[2], r[6]))
        kinds = [r[0] for r in items]
        attrs: dict[str, Any] = {}
        decision = next((r for r in items if r[0] == "agent_decision"), None)
        if decision is not None:
            agent = decision[1]
            name = GENAI_SEMCONV["names"]["plan"].format(agent=agent) if genai \
                else f"air.decision {agent}"
            _genai(attrs, "operation", GENAI_SEMCONV["operations"]["plan"], genai)
            _genai(attrs, "agent_name", agent, genai)
            outcome = decision[7].get("outcome")
            if outcome:
                attrs["air.decision.outcome"] = str(outcome)
        else:
            subject = next((r[1] for r in items if r[0] == "submitted"), items[0][1])
            name = f"air.command {subject}" if "submitted" in kinds else f"air.{kinds[0]}"
            attrs["air.subject"] = subject
        terminal = [k for k in kinds if k in TERMINAL_KINDS]
        if terminal:
            attrs["air.outcome"] = terminal[-1]
        domain = items[0][7].get("clock_domain")
        if domain:
            attrs["air.clock_domain"] = str(domain)
        parent = next((_span_id(r[5]) for r in items if _span_id(r[5])), None)
        span = Span(tid, sid, name, items[0][2], items[-1][2], parent_span_id=parent,
                    attributes=attrs)
        span.events = [(r[0], r[2], {"air.subject": r[1], "air.seq": r[6]}) for r in items]
        errors = [k for k in kinds if k in ERROR_KINDS]
        if errors:
            span.status_code, span.status_message = STATUS_ERROR, errors[-1]
        spans.append(span)
    return spans


# -- OTLP/JSON encoding ----------------------------------------------------------------------
def any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [any_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": any_value(v)} for k, v in sorted(d.items()) if v is not None]


def to_otlp(spans: Sequence[Span], *, service_name: str = "air",
            resource: dict[str, Any] | None = None) -> dict[str, Any]:
    """One ``ExportTraceServiceRequest`` in OTLP/JSON."""
    from .. import __version__

    out = []
    for s in sorted(spans, key=lambda s: (s.start_ns, s.span_id)):
        span: dict[str, Any] = {
            "traceId": s.trace_id, "spanId": s.span_id, "name": s.name,
            "kind": SPAN_KIND[s.kind], "startTimeUnixNano": str(s.start_ns),
            "endTimeUnixNano": str(max(s.end_ns, s.start_ns)), "attributes": _attrs(
                s.attributes)}
        if s.parent_span_id:
            span["parentSpanId"] = s.parent_span_id
        if s.events:
            span["events"] = [{"timeUnixNano": str(t), "name": n, "attributes": _attrs(a)}
                              for n, t, a in s.events]
        if s.status_code != STATUS_UNSET:
            span["status"] = {"code": s.status_code, "message": s.status_message}
        out.append(span)
    res = {"service.name": service_name, **(resource or {})}
    return {"resourceSpans": [{
        "resource": {"attributes": _attrs(res)},
        "scopeSpans": [{
            "scope": {"name": "agentic_robotics", "version": __version__,
                      "attributes": _attrs({"air.semconv.genai": GENAI_SEMCONV["pin"]})},
            "spans": out}]}]}


def write_otlp_file(path: str | Path, request: dict[str, Any], append: bool = False) -> Path:
    """OTLP file-exporter format: one JSON request per line."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(request, separators=(",", ":")) + "\n")
    return p


Poster = Callable[[str, bytes, dict[str, str]], int]


def urllib_poster(url: str, body: bytes, headers: dict[str, str]) -> int:
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return int(resp.status)


def post_otlp(endpoint: str, request: dict[str, Any], *, poster: Poster = urllib_poster,
              headers: dict[str, str] | None = None) -> int:
    """POST to an OTLP/HTTP collector. ``endpoint`` is the base URL or the full
    ``.../v1/traces`` URL."""
    if not endpoint.startswith(("http://", "https://")):
        raise SchemaError("OTLP endpoint must be an http(s) URL", endpoint=endpoint)
    url = endpoint if endpoint.rstrip("/").endswith("/v1/traces") else \
        endpoint.rstrip("/") + "/v1/traces"
    status = poster(url, json.dumps(request).encode(),
                    {"Content-Type": "application/json", **(headers or {})})
    if not 200 <= status < 300:
        raise SchemaError("OTLP endpoint rejected the export", url=url, status=status)
    return status
