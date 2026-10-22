"""Tamper-evident, append-only evidence ledger (baseline REQ10 and section 4).

Each entry's hash covers its content and the previous entry's hash, so any edit,
deletion or reordering is detected by :meth:`Ledger.verify`. This is a single-writer,
local ledger: the "blockchain-like" accountability the baseline asks for without
consensus round trips. Multi-organization trust can be added behind the same
interface (e.g. by anchoring the head hash elsewhere).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import _dump
from . import tracing

__requirements__ = ("REQ10", "REQ02", "REQ15", "REQ16")

GENESIS = "0" * 64


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    ts: float
    kind: str
    subject: str
    data: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None
    prev: str = GENESIS
    hash: str = ""

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("hash")
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    head: str
    first_bad_seq: int | None = None
    problem: str = ""


class Ledger:
    """Hash-chained JSON-lines ledger. ``path=None`` keeps it in memory."""

    def __init__(self, path: str | Path | None = None, *,
                 wall_clock: Callable[[], float] = time.time, fsync: bool = False) -> None:
        self.path = Path(path) if path else None
        self.wall_clock = wall_clock
        self.fsync = fsync
        self._entries: list[LedgerEntry] = []
        self._listeners: list[Callable[[LedgerEntry], None]] = []
        self._synced_size = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._reload()

    def _reload(self) -> None:
        """Pick up entries appended by another process since we last looked."""
        assert self.path is not None
        if self.path.exists():
            self._entries = list(self._read(self.path))
            self._synced_size = self.path.stat().st_size

    @staticmethod
    def _read(path: Path) -> Iterator[LedgerEntry]:
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    yield LedgerEntry(**json.loads(line))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise SchemaError("corrupt ledger line", path=str(path), line=n,
                                      error=str(exc)) from exc

    @property
    def head(self) -> str:
        return self._entries[-1].hash if self._entries else GENESIS

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[LedgerEntry]:
        return iter(self._entries)

    def on_append(self, listener: Callable[[LedgerEntry], None]) -> None:
        self._listeners.append(listener)

    def append(self, kind: str, subject: str, data: dict[str, Any] | None = None, *,
               trace_id: str | None = None, span_id: str | None = None) -> LedgerEntry:
        if self.path and self.path.exists() and self.path.stat().st_size != self._synced_size:
            self._reload()
        ctx = tracing.current()
        if trace_id is None and ctx is not None:
            trace_id, span_id = ctx.trace_id, span_id or ctx.span_id
        payload = _dump(data or {})
        assert isinstance(payload, dict)
        draft = LedgerEntry(seq=len(self._entries), ts=self.wall_clock(), kind=kind,
                            subject=subject, data=payload, trace_id=trace_id,
                            span_id=span_id, prev=self.head)
        entry = LedgerEntry(**{**draft.body(), "hash": _hash(draft.body())})
        if self.path:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True, default=str) + "\n")
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            self._synced_size = self.path.stat().st_size
        self._entries.append(entry)
        for listener in list(self._listeners):
            listener(entry)
        return entry

    def verify(self) -> VerifyReport:
        entries = list(self._read(self.path)) if self.path and self.path.exists() \
            else self._entries
        prev = GENESIS
        for i, e in enumerate(entries):
            if e.seq != i:
                return VerifyReport(False, len(entries), prev, e.seq, "sequence gap or reorder")
            if e.prev != prev:
                return VerifyReport(False, len(entries), prev, e.seq, "broken chain link")
            if _hash(e.body()) != e.hash:
                return VerifyReport(False, len(entries), prev, e.seq, "content hash mismatch")
            prev = e.hash
        return VerifyReport(True, len(entries), prev)

    def query(self, *, kind: str | None = None, subject: str | None = None,
              trace_id: str | None = None, prefix: str | None = None) -> list[LedgerEntry]:
        out = []
        for e in self._entries:
            if kind is not None and e.kind != kind:
                continue
            if prefix is not None and not e.kind.startswith(prefix):
                continue
            if subject is not None and e.subject != subject:
                continue
            if trace_id is not None and e.trace_id != trace_id:
                continue
            out.append(e)
        return out
