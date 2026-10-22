"""Tamper-evident, append-only evidence ledger (baseline REQ10 and section 4).

Each entry's hash covers its content and the previous entry's hash, so any edit,
deletion inside the log, or reordering is detected by :meth:`Ledger.verify`. Tail
truncation and wholesale rewrites need an external anchor of the head (checkpoints).

Entry formats:

* **v2** (all new entries): hash = SHA-256 over the RFC 8785 canonical JSON of
  ``{v, seq, ts_ns, kind, subject, data, trace_id, span_id, prev}``. Integer
  nanoseconds, plain-JSON data (normalized before storing), so any implementation in any
  language can recompute it.
* **v1** (entries written before 2026-10-22): Python ``json.dumps(sort_keys=True)``
  over the original fields. Still verified, never written.

Writers are serialized with an OS file lock (``<ledger>.lock``), and a writer that
finds the file grew since it last looked reloads before appending, so concurrent
processes extend one chain instead of forking it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ..core.errors import SchemaError
from . import tracing
from .canonical import canonical_bytes, to_json_value

__requirements__ = ("REQ10", "REQ02", "REQ15", "REQ16")

GENESIS = "0" * 64
CURRENT_VERSION = 2
_V1_FIELDS = ("seq", "ts", "kind", "subject", "data", "trace_id", "span_id", "prev")


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
    v: int = 1
    ts_ns: int | None = None

    def body(self) -> dict[str, Any]:
        """The hashed content, in the shape the entry's format version defines."""
        if self.v == 1:
            return {k: getattr(self, k) for k in _V1_FIELDS}
        return {"v": self.v, "seq": self.seq, "ts_ns": self.ts_ns, "kind": self.kind,
                "subject": self.subject, "data": self.data, "trace_id": self.trace_id,
                "span_id": self.span_id, "prev": self.prev}

    def compute_hash(self) -> str:
        body = self.body()
        if self.v == 1:
            text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.v == 2:
            return hashlib.sha256(canonical_bytes(body)).hexdigest()
        raise SchemaError("unknown ledger entry version", v=self.v, seq=self.seq)

    def to_dict(self) -> dict[str, Any]:
        if self.v == 1:
            return {**self.body(), "hash": self.hash}
        return {**self.body(), "ts": self.ts, "hash": self.hash}


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    head: str
    first_bad_seq: int | None = None
    problem: str = ""


class LedgerLockTimeout(SchemaError):
    code = "ledger_locked"


@contextlib.contextmanager
def _file_lock(path: Path, timeout_s: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh: IO[bytes] = open(path, "a+b")  # noqa: SIM115 - held for the lock's lifetime
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LedgerLockTimeout("ledger is locked by another writer",
                                            lock=str(path), timeout_s=timeout_s) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


class Ledger:
    """Hash-chained JSON-lines ledger. ``path=None`` keeps it in memory."""

    def __init__(self, path: str | Path | None = None, *,
                 wall_clock_ns: Callable[[], int] = time.time_ns, fsync: bool = False,
                 lock_timeout_s: float = 10.0) -> None:
        self.path = Path(path) if path else None
        self.wall_clock_ns = wall_clock_ns
        self.fsync = fsync
        self.lock_timeout_s = lock_timeout_s
        self._entries: list[LedgerEntry] = []
        self._listeners: list[Callable[[LedgerEntry], None]] = []
        self._synced_size = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._reload()

    @property
    def lock_path(self) -> Path | None:
        return self.path.with_name(self.path.name + ".lock") if self.path else None

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
        ctx = tracing.current()
        if trace_id is None and ctx is not None:
            trace_id, span_id = ctx.trace_id, span_id or ctx.span_id
        payload = to_json_value(data or {})
        assert isinstance(payload, dict)
        lock = (_file_lock(self.lock_path, self.lock_timeout_s) if self.lock_path
                else contextlib.nullcontext())
        with lock:
            if self.path and self.path.exists() and \
                    self.path.stat().st_size != self._synced_size:
                self._reload()
            ts_ns = int(self.wall_clock_ns())
            draft = LedgerEntry(seq=len(self._entries), ts=ts_ns / 1e9, kind=kind,
                                subject=subject, data=payload, trace_id=trace_id,
                                span_id=span_id, prev=self.head, v=CURRENT_VERSION,
                                ts_ns=ts_ns)
            entry = LedgerEntry(**{**draft.__dict__, "hash": draft.compute_hash()})
            if self.path:
                with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(entry.to_dict(), sort_keys=True,
                                        ensure_ascii=False) + "\n")
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
            try:
                ok = e.compute_hash() == e.hash
            except SchemaError as exc:
                return VerifyReport(False, len(entries), prev, e.seq, exc.message)
            if not ok:
                return VerifyReport(False, len(entries), prev, e.seq, "content hash mismatch")
            if e.v >= 2 and e.ts_ns is None:
                return VerifyReport(False, len(entries), prev, e.seq, "v2 entry without ts_ns")
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
