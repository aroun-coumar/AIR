"""Structured execution trace: the evidence log for decisions, commands and outcomes.

Each event carries the causal trace context (``trace_id``/``span_id``/``parent_id``)
active when it was recorded. Sinks receive every event as it happens; use
``Project.runtime_sink()`` to copy significant events into a tamper-evident ledger.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.clock import Clock
from ..core.schema import _dump
from ..provenance import tracing

__requirements__ = ("REQ02", "REQ15")

TraceSink = Callable[["TraceEvent"], None]


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    t: float
    clock_domain: str
    kind: str
    subject: str
    data: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["data"] = _dump(self.data)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraceEvent:
        return cls(seq=int(d["seq"]), t=float(d["t"]), clock_domain=str(d["clock_domain"]),
                   kind=str(d["kind"]), subject=str(d["subject"]),
                   data=dict(d.get("data") or {}), trace_id=d.get("trace_id"),
                   span_id=d.get("span_id"), parent_id=d.get("parent_id"))


class Trace:
    """Append-only, in-memory trace with optional JSON-lines persistence and sinks."""

    def __init__(self, clock: Clock, path: str | Path | None = None,
                 sinks: list[TraceSink] | None = None) -> None:
        self.clock = clock
        self._events: list[TraceEvent] = []
        self._path = Path(path) if path else None
        self._sinks: list[TraceSink] = list(sinks or [])
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_sink(self, sink: TraceSink) -> None:
        self._sinks.append(sink)

    def record(self, kind: str, subject: str, **data: Any) -> TraceEvent:
        ctx = tracing.current()
        ev = TraceEvent(len(self._events), self.clock.now(), self.clock.domain, kind,
                        subject, data,
                        trace_id=ctx.trace_id if ctx else None,
                        span_id=ctx.span_id if ctx else None,
                        parent_id=ctx.parent_id if ctx else None)
        self._events.append(ev)
        if self._path:
            with self._path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(ev.to_dict(), default=str) + "\n")
        for sink in list(self._sinks):
            sink(ev)
        return ev

    @staticmethod
    def load(path: str | Path) -> list[TraceEvent]:
        out = []
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(TraceEvent.from_dict(json.loads(line)))
        return out

    def __iter__(self) -> Iterator[TraceEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def for_subject(self, subject: str) -> list[TraceEvent]:
        return [e for e in self._events if e.subject == subject]

    def for_trace(self, trace_id: str) -> list[TraceEvent]:
        return [e for e in self._events if e.trace_id == trace_id]

    def kinds(self, subject: str | None = None) -> list[str]:
        return [e.kind for e in self._events if subject is None or e.subject == subject]

    def to_jsonl(self) -> str:
        return "".join(json.dumps(e.to_dict(), default=str) + "\n" for e in self._events)
