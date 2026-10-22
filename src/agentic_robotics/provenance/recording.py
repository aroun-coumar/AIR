"""Recordings (MCAP, rosbag2, run logs, video, any file) as ledger entities.

A recording is registered as an entity of kind ``recording`` whose identity is its
**SHA-256** digest, with metadata: the activity and trace it belongs to, the robot, the
time range, the media type. MCAP's own CRC32 fields are *not* integrity (they detect
accidental corruption, not tampering); the digest in the hash-chained, checkpointed
ledger is.

For MCAP files a small stdlib reader (:func:`read_mcap_summary`) reads the Header,
Metadata and Attachment records, message counts and the log-time range, without the
``mcap`` package. It walks top-level records only; compressed chunks are skipped (their
time range still comes from the chunk header). :func:`write_mcap` writes a minimal valid
file (Header, Metadata, Attachments, Data End, Footer) so AIR can put an ``air``
metadata record (project, activity, trace, ledger checkpoint) into a container.

Checked against the MCAP format specification (foxglove/mcap ``website/docs/spec``,
major version 0): little-endian integers, ``uint32``-prefixed strings and maps,
records ``<opcode:u8><length:u64><content>``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:
    from .model import EntityRef
    from .project import Project

__requirements__ = ("REQ02", "F05", "F07")

MCAP_MAGIC = b"\x89MCAP0\r\n"
OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE, OP_CHUNK = 0x01, 0x02, 0x03, 0x04, \
    0x05, 0x06
OP_MESSAGE_INDEX, OP_CHUNK_INDEX, OP_ATTACHMENT, OP_ATTACHMENT_INDEX = 0x07, 0x08, 0x09, 0x0A
OP_STATISTICS, OP_METADATA, OP_METADATA_INDEX, OP_SUMMARY_OFFSET, OP_DATA_END = 0x0B, 0x0C, \
    0x0D, 0x0E, 0x0F
AIR_METADATA_NAME = "air"


@dataclass
class McapSummary:
    profile: str = ""
    library: str = ""
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    channels: dict[int, str] = field(default_factory=dict)
    message_count: int = 0
    start_ns: int | None = None
    end_ns: int | None = None
    chunks: int = 0
    compressed_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "library": self.library, "metadata": self.metadata,
                "attachments": self.attachments,
                "topics": sorted(set(self.channels.values())),
                "message_count": self.message_count, "start_ns": self.start_ns,
                "end_ns": self.end_ns, "chunks": self.chunks,
                "compressed_chunks": self.compressed_chunks}


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise SchemaError("truncated MCAP record")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u16(self) -> int:
        return int(struct.unpack("<H", self.take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.take(8))[0])

    def string(self) -> str:
        return self.take(self.u32()).decode("utf-8")

    def str_map(self) -> dict[str, str]:
        end = self.u32() + self.pos
        out = {}
        while self.pos < end:
            k = self.string()
            out[k] = self.string()
        return out


def is_mcap(path: str | Path) -> bool:
    with Path(path).open("rb") as fh:
        return fh.read(8) == MCAP_MAGIC


def _records(data: bytes, start: int, end: int) -> Iterable[tuple[int, bytes]]:
    pos = start
    while pos < end:
        if pos + 9 > end:
            raise SchemaError("truncated MCAP record header", offset=pos)
        op = data[pos]
        (length,) = struct.unpack("<Q", data[pos + 1:pos + 9])
        body = data[pos + 9:pos + 9 + length]
        if len(body) != length:
            raise SchemaError("truncated MCAP record", offset=pos, opcode=op)
        yield op, body
        pos += 9 + length
        if op == OP_FOOTER:
            return


def _time(summary: McapSummary, t: int) -> None:
    summary.start_ns = t if summary.start_ns is None else min(summary.start_ns, t)
    summary.end_ns = t if summary.end_ns is None else max(summary.end_ns, t)


def read_mcap_summary(path: str | Path) -> McapSummary:
    data = Path(path).read_bytes()
    if not data.startswith(MCAP_MAGIC):
        raise SchemaError("not an MCAP file (bad magic)", path=str(path))
    if not data.endswith(MCAP_MAGIC):
        raise SchemaError("MCAP file has no trailing magic (incomplete recording?)",
                          path=str(path))
    s = McapSummary()
    stats_seen = False
    first = True
    for op, body in _records(data, 8, len(data) - 8):
        c = _Cursor(body)
        if first:
            if op != OP_HEADER:
                raise SchemaError("MCAP file does not start with a Header record")
            s.profile, s.library = c.string(), c.string()
            first = False
        elif op == OP_METADATA:
            name = c.string()
            s.metadata[name] = c.str_map()
        elif op == OP_ATTACHMENT:
            log_time, create_time = c.u64(), c.u64()
            name, media = c.string(), c.string()
            size = c.u64()
            s.attachments.append({"name": name, "media_type": media, "size": size,
                                  "log_time_ns": log_time, "create_time_ns": create_time})
        elif op == OP_CHANNEL:
            cid, _schema = c.u16(), c.u16()
            s.channels[cid] = c.string()
        elif op == OP_MESSAGE and not stats_seen:
            c.u16()
            c.u32()
            s.message_count += 1
            _time(s, c.u64())
        elif op == OP_CHUNK:
            s.chunks += 1
            start, end = c.u64(), c.u64()
            c.u64()
            c.u32()
            compression = c.string()
            records = c.take(c.u64())
            if start or end:
                _time(s, start)
                _time(s, end)
            if compression:
                s.compressed_chunks += 1
            elif not stats_seen:
                for iop, ibody in _records(records, 0, len(records)):
                    ic = _Cursor(ibody)
                    if iop == OP_CHANNEL:
                        cid, _ = ic.u16(), ic.u16()
                        s.channels[cid] = ic.string()
                    elif iop == OP_MESSAGE:
                        ic.u16()
                        ic.u32()
                        s.message_count += 1
                        _time(s, ic.u64())
        elif op == OP_STATISTICS:
            stats_seen = True
            s.message_count = c.u64()
            c.u16()
            c.u32()
            c.u32()
            c.u32()
            c.u32()
            start, end = c.u64(), c.u64()
            if s.message_count:
                s.start_ns, s.end_ns = start, end
    if first:
        raise SchemaError("MCAP file has no records")
    return s


# -- minimal writer -------------------------------------------------------------------------
def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def _map(m: Mapping[str, str]) -> bytes:
    body = b"".join(_str(k) + _str(v) for k, v in m.items())
    return struct.pack("<I", len(body)) + body


def _record(op: int, body: bytes) -> bytes:
    return bytes([op]) + struct.pack("<Q", len(body)) + body


def write_mcap(path: str | Path, *, metadata: Mapping[str, Mapping[str, str]] | None = None,
               attachments: Iterable[tuple[str, str, bytes, int]] = (),
               profile: str = "", library: str = "agentic_robotics") -> Path:
    """Write a minimal MCAP file: Header, Metadata records, Attachments
    ``(name, media_type, data, log_time_ns)``, Data End, Footer. CRC fields are 0
    ("not available"), which the spec allows; integrity is the ledger digest."""
    out = bytearray(MCAP_MAGIC)
    out += _record(OP_HEADER, _str(profile) + _str(library))
    for name, values in (metadata or {}).items():
        out += _record(OP_METADATA, _str(name) + _map(dict(values)))
    for name, media, data, log_time in attachments:
        body = struct.pack("<QQ", log_time, 0) + _str(name) + _str(media) + \
            struct.pack("<Q", len(data)) + data + struct.pack("<I", 0)
        out += _record(OP_ATTACHMENT, body)
    out += _record(OP_DATA_END, struct.pack("<I", 0))
    out += _record(OP_FOOTER, struct.pack("<QQI", 0, 0, 0))
    out += MCAP_MAGIC
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(out))
    return p


def air_metadata(project: Project | None = None, *, activity_id: str | None = None,
                 trace_id: str | None = None, robot_id: str | None = None,
                 **extra: str) -> dict[str, str]:
    """The ``air`` MCAP metadata record linking a recording back to the ledger."""
    md: dict[str, str] = {}
    if project is not None:
        from .merkle import latest_checkpoint

        md["project"] = project.name
        md["ledger_size"] = str(len(project.ledger))
        md["ledger_head"] = project.ledger.head
        latest = latest_checkpoint(project)
        if latest is not None:
            md["ledger_checkpoint"] = f"{latest[1].size}:{latest[1].root.hex()}"
        from .project import capture_environment

        git = capture_environment(project.root).get("git") or {}
        if git.get("commit"):
            md["git_commit"] = str(git["commit"])
    for k, v in (("activity_id", activity_id), ("trace_id", trace_id), ("robot_id", robot_id)):
        if v:
            md[k] = v
    md.update(extra)
    return md


# -- registration ---------------------------------------------------------------------------
_MEDIA = {".mcap": "application/x-mcap", ".db3": "application/x-sqlite3",
          ".jsonl": "application/x-ndjson", ".airlog": "application/x-air-runlog+jsonl",
          ".bag": "application/x-rosbag", ".mp4": "video/mp4", ".csv": "text/csv"}


def register_recording(project: Project, path: str | Path, *, activity_id: str | None = None,
                       trace_id: str | None = None, robot_id: str | None = None,
                       start_ns: int | None = None, end_ns: int | None = None,
                       media_type: str | None = None, store: bool = False,
                       attributes: Mapping[str, Any] | None = None) -> EntityRef:
    """Register a recording file as a ``recording`` entity (sha256 is its identity).

    MCAP files are read for profile, topics, message count, time range and their ``air``
    metadata record; explicit arguments win over what the file says. With
    ``activity_id`` the activity is linked to the recording (``activity.generated``)."""
    p = Path(path)
    if not p.is_file():
        raise SchemaError("recording file not found", path=str(path))
    attrs: dict[str, Any] = {}
    fmt = "opaque"
    if is_mcap(p):
        fmt = "mcap"
        summary = read_mcap_summary(p)
        info = summary.to_dict()
        attrs["mcap"] = {k: info[k] for k in ("profile", "library", "topics",
                                              "message_count", "chunks",
                                              "compressed_chunks")}
        attrs["mcap"]["metadata_names"] = sorted(summary.metadata)
        attrs["mcap"]["attachments"] = [a["name"] for a in summary.attachments]
        embedded = summary.metadata.get(AIR_METADATA_NAME, {})
        activity_id = activity_id or embedded.get("activity_id")
        trace_id = trace_id or embedded.get("trace_id")
        robot_id = robot_id or embedded.get("robot_id")
        start_ns = start_ns if start_ns is not None else summary.start_ns
        end_ns = end_ns if end_ns is not None else summary.end_ns
    else:
        from .runlog import is_runlog, read_header

        if is_runlog(p):
            fmt = "air-runlog"
            header = read_header(p)
            attrs["runlog"] = {"formats": sorted(header.formats),
                               "required_features": sorted(header.required_features),
                               "parameters": len(header.parameters)}
            activity_id = activity_id or header.info.get("activity_id")
            trace_id = trace_id or header.info.get("trace_id")
            robot_id = robot_id or header.info.get("robot_id")
    if start_ns is not None and end_ns is not None and end_ns < start_ns:
        raise SchemaError("recording ends before it starts", start_ns=start_ns,
                          end_ns=end_ns)
    attrs.update({"format": fmt,
                  "media_type": media_type or _MEDIA.get(p.suffix.lower(),
                                                         "application/octet-stream")})
    for k, v in (("activity_id", activity_id), ("trace_id", trace_id),
                 ("robot_id", robot_id), ("start_ns", start_ns), ("end_ns", end_ns)):
        if v is not None:
            attrs[k] = v
    attrs.update(attributes or {})
    ref = project.entity(p, kind="recording", store=store, attributes=attrs)
    if activity_id and not project.ledger.query(kind="recording.linked",
                                                subject=ref.entity_id):
        project.ledger.append("recording.linked", ref.entity_id,
                              {"activity_id": activity_id, "trace_id": trace_id,
                               "robot_id": robot_id, "digest": ref.digest},
                              trace_id=trace_id if trace_id and len(trace_id) == 32 else None)
        project.ledger.append("activity.generated", activity_id,
                              {"activity_id": activity_id, "entity_id": ref.entity_id,
                               "derived_from": []})
    return ref


def recordings(project: Project, *, activity_id: str | None = None) -> list[EntityRef]:
    out = [e for e in project.entities().values() if e.kind == "recording"]
    if activity_id:
        out = [e for e in out if e.attributes.get("activity_id") == activity_id]
    return out


def verify_recording(project: Project, ref: EntityRef, path: str | Path) -> bool:
    """Does this file still match the digest the ledger recorded?"""
    from .project import file_digest

    return ref.digest == file_digest(Path(path))
