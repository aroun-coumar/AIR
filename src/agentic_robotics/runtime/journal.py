"""Durable command intent journal (W2.1, fixes survey defect D2).

The runtime writes a command's intent to an append-only file *before* it dispatches the
command to an adapter, and records every outcome. After a restart, a runtime given the
same journal rebuilds its handles: deduplication survives, settled outcomes are kept,
and any command that was dispatched but has no recorded outcome becomes UNRESOLVED and
is reconciled by observation. It is never resent.

Format: JSON lines. Each record is ``{"seq", "kind", "command_id", ..., "h"}`` where
``h`` is a SHA-256 over the canonical record body, so a corrupted line is detected. A
torn *final* line (the process died mid-write) is ignored; corruption anywhere else
raises. Each append is a single ``write`` on an ``O_APPEND`` descriptor followed by
``fsync``. One process at a time may hold a journal open (an OS file lock).

Kinds: ``accepted`` (command admitted, carries the command), ``awaiting_approval``,
``dispatch`` (about to call the adapter; carries the applied arguments), ``outcome``
(a result, carries the state and the result document).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ..contracts.execution import Command, CommandState, ExecutionResult
from ..core.errors import SchemaError

__requirements__ = ("REQ07", "REQ14")

JOURNAL_VERSION = 1


class JournalCorrupt(SchemaError):
    code = "journal_corrupt"


class JournalLocked(SchemaError):
    code = "journal_locked"


def _digest(body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


@dataclass
class JournalEntry:
    """Everything the journal knows about one command, folded from its records."""

    command: Command
    accepted: bool = False
    awaiting_approval: bool = False
    dispatched: bool = False
    applied_args: dict[str, Any] | None = None
    result: ExecutionResult | None = None
    records: list[str] = field(default_factory=list)

    @property
    def state(self) -> CommandState | None:
        return self.result.state if self.result else None


class CommandJournal:
    """Append-only, fsynced intent journal. Pass one to ``Runtime(journal=...)``."""

    def __init__(self, path: str | Path, *, fsync: bool = True, lock: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh: IO[bytes] | None = None
        if lock:
            self._acquire_lock()
        self._seq = 0
        self.entries: dict[str, JournalEntry] = {}
        self.torn_tail = False
        self._load()
        self._fd: int | None = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o644)

    # -- lifecycle -----------------------------------------------------------------
    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _acquire_lock(self) -> None:
        fh: IO[bytes] = open(self.lock_path, "a+b")  # noqa: SIM115 - held while open
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise JournalLocked("command journal is open in another runtime",
                                path=str(self.path)) from None
        self._lock_fh = fh

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._lock_fh is not None:
            fh, self._lock_fh = self._lock_fh, None
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def __enter__(self) -> CommandJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading ---------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            last = i == len(lines) - 1  # no trailing newline: possibly torn
            try:
                record = json.loads(line.decode("utf-8"))
                body = {k: v for k, v in record.items() if k != "h"}
                if record.get("h") != _digest(body):
                    raise ValueError("digest mismatch")
            except (ValueError, UnicodeDecodeError) as exc:
                if last:
                    self.torn_tail = True
                    break
                raise JournalCorrupt("command journal record is corrupt", path=str(self.path),
                                     line=i + 1, error=str(exc)) from None
            self._apply(record)
        if self.torn_tail:
            # Drop the torn bytes so the next append starts on a clean line.
            keep = raw[:raw.rfind(b"\n") + 1] if b"\n" in raw else b""
            with self.path.open("wb") as fh:
                fh.write(keep)
                fh.flush()
                os.fsync(fh.fileno())

    def _apply(self, record: dict[str, Any]) -> None:
        self._seq = max(self._seq, int(record.get("seq", 0)))
        kind = record.get("kind")
        cid = str(record.get("command_id"))
        entry = self.entries.get(cid)
        if kind in ("accepted", "awaiting_approval"):
            if entry is None:
                entry = JournalEntry(Command.from_dict(record["command"]))
                self.entries[cid] = entry
            if kind == "accepted":
                entry.accepted = True
                entry.awaiting_approval = False
            else:
                entry.awaiting_approval = True
        elif entry is None:
            if kind == "outcome" and "command" in record:
                entry = JournalEntry(Command.from_dict(record["command"]))
                self.entries[cid] = entry
            else:
                raise JournalCorrupt("journal record for an unknown command",
                                     path=str(self.path), command_id=cid, kind=kind)
        if kind == "dispatch":
            entry.dispatched = True
            entry.applied_args = dict(record.get("applied_args") or {})
        elif kind == "outcome":
            entry.result = ExecutionResult.from_dict(record["result"])
        entry.records.append(str(kind))

    def __iter__(self) -> Iterator[JournalEntry]:
        return iter(list(self.entries.values()))

    def __len__(self) -> int:
        return len(self.entries)

    # -- writing ---------------------------------------------------------------------
    def _append(self, kind: str, command_id: str, **data: Any) -> None:
        if self._fd is None:
            raise SchemaError("command journal is closed", path=str(self.path))
        self._seq += 1
        body = {"v": JOURNAL_VERSION, "seq": self._seq, "kind": kind,
                "command_id": command_id, **data}
        record = {**body, "h": _digest(body)}
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           default=str) + "\n").encode("utf-8")
        written = os.write(self._fd, line)
        if written != len(line):
            raise SchemaError("short write to command journal", path=str(self.path))
        if self.fsync:
            os.fsync(self._fd)
        self._apply(json.loads(line))

    def record_awaiting_approval(self, command: Command) -> None:
        self._append("awaiting_approval", command.command_id, command=command.to_dict())

    def record_accepted(self, command: Command) -> None:
        self._append("accepted", command.command_id, command=command.to_dict())

    def record_dispatch(self, command_id: str, applied_args: dict[str, Any]) -> None:
        """Written, and fsynced, before the adapter is called."""
        self._append("dispatch", command_id, applied_args=applied_args)

    def record_outcome(self, command: Command, result: ExecutionResult) -> None:
        extra: dict[str, Any] = {}
        if command.command_id not in self.entries:
            extra["command"] = command.to_dict()  # e.g. a rejection at submission
        self._append("outcome", command.command_id, state=result.state.value,
                     result=result.to_dict(), **extra)


__all__ = ["CommandJournal", "JournalCorrupt", "JournalEntry", "JournalLocked"]
