"""Structured execution trace: the evidence log for decisions, commands and outcomes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.clock import Clock
from ..core.schema import _dump


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    t: float
    clock_domain: str
    kind: str
    subject: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["data"] = _dump(self.data)
        return d


class Trace:
    """Append-only, in-memory trace with optional JSON-lines persistence."""

    def __init__(self, clock: Clock, path: str | Path | None = None) -> None:
        self.clock = clock
        self._events: list[TraceEvent] = []
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, subject: str, **data: Any) -> TraceEvent:
        ev = TraceEvent(len(self._events), self.clock.now(), self.clock.domain, kind,
                        subject, data)
        self._events.append(ev)
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict(), default=str) + "\n")
        return ev

    def __iter__(self) -> Iterator[TraceEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def for_subject(self, subject: str) -> list[TraceEvent]:
        return [e for e in self._events if e.subject == subject]

    def kinds(self, subject: str | None = None) -> list[str]:
        return [e.kind for e in self._events if subject is None or e.subject == subject]

    def to_jsonl(self) -> str:
        return "".join(json.dumps(e.to_dict(), default=str) + "\n" for e in self._events)
