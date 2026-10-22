"""Self-describing run logs: a small, stdlib, JSON-lines format for robot and simulation
runs, after the PX4 ULog pattern.

    {"air_runlog": 1, "formats": {...}, "parameters": {...}, "required_features": [...],
     "optional_features": [...], "clock": {"domain": "host.monotonic", "unit": "ns"},
     "info": {"robot_id": ..., "activity_id": ..., "trace_id": ...}}
    {"t": 1000, "m": "imu", "v": [0.01, -0.02, 9.81]}
    {"t": 2000, "sync": 1}
    {"t": 2500, "dropout": 400000, "m": "imu"}
    {"t": 3000, "param": {"gain": 0.5}}

Rules that make it safe to read years later:

* **Definitions before data.** The first line declares every message format (field
  names, types, units) and all parameters; a data line is only positional values, and a
  reader checks each against its format.
* **Feature flags.** ``required_features`` lists what a reader must understand (like
  ULog's incompatible flags): a reader that does not know one rejects the whole file
  instead of misreading it. ``optional_features`` may be skipped: their records are
  ``{"t": ..., "x": "<feature>", ...}`` and readers that do not know the feature skip
  exactly those.
* **Sync and dropout markers.** ``sync`` lines carry a sequence number for aligning
  logs from several recorders; ``dropout`` lines say data *was lost* for a duration, so
  a gap is never mistaken for "nothing happened".
* Timestamps are integer nanoseconds in the declared clock domain and never go back.

Integrity is not in the file: register it with :func:`register_runlog` (sha256 entity in
the ledger), optionally embedding the latest ledger checkpoint in ``info``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:
    from .model import EntityRef
    from .project import Project

__requirements__ = ("REQ02", "F05", "F07")

FORMAT_VERSION = 1
FEATURES = frozenset({"sync_markers", "dropout_markers", "param_updates"})
FIELD_TYPES = frozenset({"f64", "i64", "bool", "str"})


class UnsupportedRunLog(SchemaError):
    code = "unsupported_runlog"


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str = "f64"
    unit: str | None = None

    def check(self, value: Any) -> None:
        ok = {"f64": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
              and math.isfinite(v),
              "i64": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "bool": lambda v: isinstance(v, bool),
              "str": lambda v: isinstance(v, str)}[self.type](value)
        if not ok:
            raise SchemaError("run log value does not match its field type", field=self.name,
                              type=self.type, value=repr(value)[:40])


@dataclass(frozen=True)
class RunLogHeader:
    formats: dict[str, tuple[FieldDef, ...]]
    parameters: dict[str, Any] = field(default_factory=dict)
    required_features: frozenset[str] = frozenset()
    optional_features: frozenset[str] = frozenset()
    clock_domain: str = "host.monotonic"
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"air_runlog": FORMAT_VERSION,
                "formats": {n: {"fields": [[f.name, f.type, f.unit] for f in fs]}
                            for n, fs in self.formats.items()},
                "parameters": self.parameters,
                "required_features": sorted(self.required_features),
                "optional_features": sorted(self.optional_features),
                "clock": {"domain": self.clock_domain, "unit": "ns"}, "info": self.info}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], known_features: frozenset[str] = FEATURES
                  ) -> RunLogHeader:
        if d.get("air_runlog") != FORMAT_VERSION:
            raise UnsupportedRunLog("unsupported run log version", version=d.get("air_runlog"))
        required = frozenset(d.get("required_features", []))
        unknown = sorted(required - known_features)
        if unknown:
            raise UnsupportedRunLog("run log needs features this reader does not "
                                    "understand; refusing to guess", features=unknown)
        clock = d.get("clock") or {}
        if clock.get("unit", "ns") != "ns":
            raise UnsupportedRunLog("run log clock unit must be ns", unit=clock.get("unit"))
        formats: dict[str, tuple[FieldDef, ...]] = {}
        for name, spec in (d.get("formats") or {}).items():
            fields = []
            for item in spec.get("fields", []):
                fname, ftype, unit = [*list(item), None, None][:3]
                if ftype not in FIELD_TYPES:
                    raise UnsupportedRunLog("unknown field type", format=name, type=ftype)
                fields.append(FieldDef(str(fname), str(ftype), unit))
            if len({f.name for f in fields}) != len(fields):
                raise SchemaError("duplicate field names in run log format", format=name)
            formats[str(name)] = tuple(fields)
        return cls(formats, dict(d.get("parameters") or {}), required,
                   frozenset(d.get("optional_features", [])),
                   str(clock.get("domain", "host.monotonic")), dict(d.get("info") or {}))


@dataclass(frozen=True)
class Record:
    kind: str  # data | sync | dropout | param
    t: int
    format: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


class RunLogWriter:
    """Write a run log; definitions go first, so declare every format and feature up
    front. Using an undeclared format or feature raises."""

    def __init__(self, path: str | Path, formats: Mapping[str, Sequence[FieldDef]], *,
                 parameters: Mapping[str, Any] | None = None, features: Sequence[str] = (),
                 optional_features: Sequence[str] = (), clock_domain: str = "host.monotonic",
                 info: Mapping[str, Any] | None = None) -> None:
        unknown = sorted(set(features) - FEATURES)
        if unknown:
            raise SchemaError("unknown run log features", features=unknown)
        for fs in formats.values():
            for f in fs:
                if f.type not in FIELD_TYPES:
                    raise SchemaError("unknown field type", field=f.name, type=f.type)
        self.header = RunLogHeader({k: tuple(v) for k, v in formats.items()},
                                   dict(parameters or {}), frozenset(features),
                                   frozenset(optional_features), clock_domain, dict(info or {}))
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = self.path.open("w", encoding="utf-8", newline="\n")
        self._last_t: int | None = None
        self._line(self.header.to_dict())

    def _line(self, obj: Mapping[str, Any]) -> None:
        if self._fh is None:
            raise SchemaError("run log is closed")
        self._fh.write(json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n")

    def _time(self, t_ns: int) -> int:
        if not isinstance(t_ns, int) or isinstance(t_ns, bool) or t_ns < 0:
            raise SchemaError("run log timestamps are non-negative integer ns", t=t_ns)
        if self._last_t is not None and t_ns < self._last_t:
            raise SchemaError("run log time went backwards", t=t_ns, last=self._last_t)
        self._last_t = t_ns
        return t_ns

    def _need(self, feature: str) -> None:
        if feature not in self.header.required_features:
            raise SchemaError("declare the feature in the header first", feature=feature)

    def write(self, t_ns: int, fmt: str, values: Sequence[Any]) -> None:
        fields = self.header.formats.get(fmt)
        if fields is None:
            raise SchemaError("undeclared run log format", format=fmt)
        if len(values) != len(fields):
            raise SchemaError("wrong number of values", format=fmt, expected=len(fields),
                              got=len(values))
        for f, v in zip(fields, values, strict=True):
            f.check(v)
        self._line({"t": self._time(t_ns), "m": fmt, "v": list(values)})

    def sync(self, t_ns: int, seq: int) -> None:
        self._need("sync_markers")
        self._line({"t": self._time(t_ns), "sync": int(seq)})

    def dropout(self, t_ns: int, duration_ns: int, fmt: str | None = None) -> None:
        self._need("dropout_markers")
        if fmt is not None and fmt not in self.header.formats:
            raise SchemaError("undeclared run log format", format=fmt)
        rec: dict[str, Any] = {"t": self._time(t_ns), "dropout": int(duration_ns)}
        if fmt:
            rec["m"] = fmt
        self._line(rec)

    def param(self, t_ns: int, name: str, value: Any) -> None:
        self._need("param_updates")
        self._line({"t": self._time(t_ns), "param": {name: value}})

    def extension(self, t_ns: int, feature: str, payload: Mapping[str, Any]) -> None:
        if feature not in self.header.optional_features:
            raise SchemaError("declare the optional feature in the header first",
                              feature=feature)
        self._line({**payload, "t": self._time(t_ns), "x": feature})

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> RunLogWriter:
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def is_runlog(path: str | Path) -> bool:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            first = fh.readline()
        return '"air_runlog"' in first and isinstance(json.loads(first), dict)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def read_header(path: str | Path, known_features: frozenset[str] = FEATURES) -> RunLogHeader:
    with Path(path).open(encoding="utf-8") as fh:
        first = fh.readline()
    try:
        return RunLogHeader.from_dict(json.loads(first), known_features)
    except json.JSONDecodeError as exc:
        raise UnsupportedRunLog("run log header is not JSON") from exc


def read_runlog(path: str | Path, known_features: frozenset[str] = FEATURES
                ) -> tuple[RunLogHeader, Iterator[Record]]:
    header = read_header(path, known_features)

    def records() -> Iterator[Record]:
        last = None
        with Path(path).open(encoding="utf-8") as fh:
            fh.readline()
            for n, line in enumerate(fh, 2):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SchemaError("corrupt run log line", line=n) from exc
                t = obj.get("t")
                if not isinstance(t, int) or isinstance(t, bool) or t < 0:
                    raise SchemaError("run log line without integer time", line=n)
                if last is not None and t < last:
                    raise SchemaError("run log time went backwards", line=n)
                last = t
                if "x" in obj:
                    if obj["x"] in header.optional_features:
                        continue  # an optional feature: skipping it is always safe
                    raise UnsupportedRunLog("record of an undeclared feature", line=n,
                                            feature=obj["x"])
                if "m" in obj and "v" in obj:
                    fields = header.formats.get(obj["m"])
                    if fields is None:
                        raise SchemaError("data for an undeclared format", line=n,
                                          format=obj["m"])
                    if len(obj["v"]) != len(fields):
                        raise SchemaError("wrong number of values", line=n, format=obj["m"])
                    for f, v in zip(fields, obj["v"], strict=True):
                        f.check(v)
                    yield Record("data", t, obj["m"],
                                 {f.name: v for f, v in zip(fields, obj["v"], strict=True)})
                elif "sync" in obj and "sync_markers" in header.required_features:
                    yield Record("sync", t, None, {"seq": obj["sync"]})
                elif "dropout" in obj and "dropout_markers" in header.required_features:
                    yield Record("dropout", t, obj.get("m"), {"duration_ns": obj["dropout"]})
                elif "param" in obj and "param_updates" in header.required_features:
                    yield Record("param", t, None, dict(obj["param"]))
                else:
                    raise UnsupportedRunLog("record this reader cannot interpret", line=n,
                                            keys=sorted(obj))

    return header, records()


def summarize(path: str | Path) -> dict[str, Any]:
    header, recs = read_runlog(path)
    counts: dict[str, int] = {}
    first = last = None
    lost_ns = 0
    syncs = 0
    for r in recs:
        first = r.t if first is None else first
        last = r.t
        if r.kind == "data":
            counts[str(r.format)] = counts.get(str(r.format), 0) + 1
        elif r.kind == "dropout":
            lost_ns += int(r.values["duration_ns"])
        elif r.kind == "sync":
            syncs += 1
    return {"formats": sorted(header.formats), "messages": counts, "start_ns": first,
            "end_ns": last, "dropout_ns": lost_ns, "sync_markers": syncs,
            "clock_domain": header.clock_domain,
            "required_features": sorted(header.required_features), "info": header.info}


def register_runlog(project: Project, path: str | Path, **kw: Any) -> EntityRef:
    """Check the whole log, then register it as a ``recording`` entity."""
    from .recording import register_recording

    info = summarize(path)
    attributes = {"runlog_summary": {k: info[k] for k in ("messages", "dropout_ns",
                                                          "sync_markers", "clock_domain")}}
    kw.setdefault("start_ns", info["start_ns"])
    kw.setdefault("end_ns", info["end_ns"])
    kw.setdefault("media_type", "application/x-air-runlog+jsonl")
    return register_recording(project, path, attributes=attributes, **kw)
