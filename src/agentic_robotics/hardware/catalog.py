"""Hardware catalog: robot classes, device types and protocols AIR can describe.

A :class:`DeviceType` says what a kind of component is (``actuator.motor.bldc``), which
properties a datasheet or manifest states (with units), which interfaces it speaks,
which capabilities it provides, which telemetry is worth watching (with typical
limits), what wears out, how it fails and whether it is safety relevant.
A :class:`RobotClass` (``robot.legged.quadruped``) lists the component slots such a
robot is expected to have. A :class:`Protocol` (``fieldbus.ethercat``) describes an
interface, including its real-time class and security properties.

The built-in catalog ships as JSON next to this module. Projects extend it with
their own files in ``.air/hardware/*.json``; ids must not collide, so a project can add
types but never silently redefine one.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..contracts.manifest import Sensitivity
from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from ..core.units import known_units

__requirements__ = ("REQ03", "REQ01", "REQ13", "REQ16")

DATA_DIR = Path(__file__).with_name("data")
DOMAINS = tuple(f"D{n:02d}" for n in range(1, 20))
TYPE_PREFIXES = ("actuator", "drive", "transmission", "sensor", "power", "compute", "thermal",
                 "comms", "iot", "infra", "end_effector", "mechanical", "hmi", "safety")


class PropertyKind(StrEnum):
    QUANTITY = "quantity"
    NUMBER = "number"
    INTEGER = "integer"
    STRING = "string"
    ENUM = "enum"
    BOOL = "bool"


class MaintenanceBasis(StrEnum):
    HOURS = "hours"
    CYCLES = "cycles"
    DISTANCE_KM = "distance_km"
    CALENDAR_DAYS = "calendar_days"
    CONDITION = "condition"


@dataclass(frozen=True)
class Reference:
    name: str
    url: str | None = None


@dataclass(frozen=True)
class PropertySpec:
    name: str
    kind: PropertyKind = PropertyKind.STRING
    unit: str | None = None
    values: tuple[str, ...] = ()
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class SignalSpec:
    """A telemetry signal and, where a typical value exists, its warn/critical limits."""

    name: str
    unit: str | None = None
    warn_above: float | None = None
    critical_above: float | None = None
    warn_below: float | None = None
    critical_below: float | None = None
    description: str = ""


@dataclass(frozen=True)
class MaintenanceItem:
    item: str
    basis: MaintenanceBasis = MaintenanceBasis.CONDITION
    interval: float | None = None
    action: str = ""


@dataclass(frozen=True)
class DeviceType:
    type_id: str
    name: str
    category: str
    description: str = ""
    domains: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    properties: tuple[PropertySpec, ...] = ()
    interfaces: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    telemetry: tuple[SignalSpec, ...] = ()
    maintenance: tuple[MaintenanceItem, ...] = ()
    failure_modes: tuple[str, ...] = ()
    safety_relevant: bool = False
    typical_parents: tuple[str, ...] = ()
    references: tuple[Reference, ...] = ()
    #: A more general type this one specializes (a vendor motor extends a generic BLDC).
    extends: str | None = None
    #: Shared-vocabulary classes and equivalents (CURIEs or IRIs), see ``semantics``.
    semantic: tuple[str, ...] = ()
    #: Who may see this type's details; vendor NDA types are ``restricted``.
    sensitivity: Sensitivity = Sensitivity.PUBLIC

    def property(self, name: str) -> PropertySpec | None:
        return next((p for p in self.properties if p.name == name), None)

    def signal(self, name: str) -> SignalSpec | None:
        return next((s for s in self.telemetry if s.name == name), None)


@dataclass(frozen=True)
class Slot:
    slot: str
    types: tuple[str, ...]
    min: int = 0
    max: int | None = None
    description: str = ""


@dataclass(frozen=True)
class RobotClass:
    class_id: str
    name: str
    description: str = ""
    domains: tuple[str, ...] = ()
    standards: tuple[str, ...] = ()
    slots: tuple[Slot, ...] = ()
    capabilities: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    safety_relevant: bool = False
    references: tuple[Reference, ...] = ()


@dataclass(frozen=True)
class Protocol:
    protocol_id: str
    name: str
    layer: str = ""
    medium: str = ""
    realtime: str = "none"
    typical_rate: str = ""
    topology: str = ""
    security: str = ""
    domains: tuple[str, ...] = ()
    standards: tuple[str, ...] = ()
    description: str = ""
    references: tuple[Reference, ...] = ()


@contract("device_catalog", version="1.0")
class DeviceCatalog(Contract):
    name: str = "catalog"
    device_types: tuple[DeviceType, ...] = ()
    robot_classes: tuple[RobotClass, ...] = ()
    protocols: tuple[Protocol, ...] = ()

    def __post_init__(self) -> None:
        for label, ids in (("device type", [t.type_id for t in self.device_types]),
                           ("robot class", [c.class_id for c in self.robot_classes]),
                           ("protocol", [p.protocol_id for p in self.protocols])):
            dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
            if dupes:
                raise SchemaError(f"duplicate {label} ids in catalog", ids=dupes[:20])
        declared = {t.type_id: t for t in self.device_types}
        object.__setattr__(self, "_declared", declared)
        object.__setattr__(self, "_types", _resolve_inheritance(declared))
        object.__setattr__(self, "_classes", {c.class_id: c for c in self.robot_classes})
        object.__setattr__(self, "_protocols", {p.protocol_id: p for p in self.protocols})

    # -- lookup -------------------------------------------------------------------------
    def type(self, type_id: str) -> DeviceType | None:
        """The effective type: its own declaration merged over everything it extends."""
        found: DeviceType | None = self._types.get(type_id)  # type: ignore[attr-defined]
        return found

    def declared(self, type_id: str) -> DeviceType | None:
        """The type exactly as written, without inherited content."""
        found: DeviceType | None = self._declared.get(type_id)  # type: ignore[attr-defined]
        return found

    def lineage(self, type_id: str) -> list[str]:
        """``[type_id, parent, grandparent, ...]``."""
        chain: list[str] = []
        current = self.declared(type_id)
        while current is not None and current.type_id not in chain:
            chain.append(current.type_id)
            current = self.declared(current.extends) if current.extends else None
        return chain

    def effective_types(self) -> list[DeviceType]:
        types: dict[str, DeviceType] = self._types  # type: ignore[attr-defined]
        return list(types.values())

    def robot_class(self, class_id: str) -> RobotClass | None:
        found: RobotClass | None = self._classes.get(class_id)  # type: ignore[attr-defined]
        return found

    def protocol(self, protocol_id: str) -> Protocol | None:
        found: Protocol | None = self._protocols.get(protocol_id)  # type: ignore[attr-defined]
        return found

    def match(self, pattern: str) -> list[DeviceType]:
        """Device types whose id matches a glob (``sensor.lidar.*``) or equals it."""
        return [t for t in self.effective_types() if matches(t.type_id, pattern)]

    def match_classes(self, pattern: str) -> list[RobotClass]:
        """Robot classes matching a pattern: a slot may hold a whole robot (an arm on an AMR)."""
        return [c for c in self.robot_classes if matches(c.class_id, pattern)]

    def search(self, text: str = "", *, category: str | None = None,
               domain: str | None = None, interface: str | None = None,
               provides: str | None = None, safety: bool | None = None) -> list[DeviceType]:
        needle = text.lower()
        out = []
        for t in self.effective_types():
            if category and t.category != category:
                continue
            if domain and domain not in t.domains:
                continue
            if interface and interface not in t.interfaces:
                continue
            if provides and provides not in t.provides:
                continue
            if safety is not None and t.safety_relevant is not safety:
                continue
            if needle and needle not in " ".join(
                    (t.type_id, t.name, t.description, *t.aliases)).lower():
                continue
            out.append(t)
        return sorted(out, key=lambda t: t.type_id)

    def merged(self, other: DeviceCatalog, *, name: str | None = None) -> DeviceCatalog:
        """Union of two catalogs. Colliding ids are an error, never an override."""
        return DeviceCatalog(name=name or self.name,
                             device_types=self.device_types + other.device_types,
                             robot_classes=self.robot_classes + other.robot_classes,
                             protocols=self.protocols + other.protocols)

    # -- consistency ----------------------------------------------------------------------
    def problems(self) -> list[str]:
        """Internal consistency: ids, prefixes, units, domains, interface and slot references."""
        units = set(known_units())
        out: list[str] = []

        def domains(owner: str, ds: Iterable[str]) -> None:
            ds = tuple(ds)
            if not ds:
                out.append(f"{owner}: no domains")
            out.extend(f"{owner}: unknown domain {d}" for d in ds if d not in DOMAINS)

        for t in self.device_types:
            head = t.type_id.split(".", 1)[0]
            if not _valid_id(t.type_id):
                out.append(f"{t.type_id}: invalid id")
            if head not in TYPE_PREFIXES:
                out.append(f"{t.type_id}: unknown prefix {head}")
            if t.category != head:
                out.append(f"{t.type_id}: category {t.category} does not match prefix")
            domains(t.type_id, t.domains)
            names = [p.name for p in t.properties]
            if len(names) != len(set(names)):
                out.append(f"{t.type_id}: duplicate property names")
            for p in t.properties:
                if p.kind is PropertyKind.QUANTITY and (p.unit is None or p.unit not in units):
                    out.append(f"{t.type_id}.{p.name}: quantity needs a known unit "
                               f"(got {p.unit!r})")
                if p.kind is PropertyKind.ENUM and not p.values:
                    out.append(f"{t.type_id}.{p.name}: enum without values")
            for s in t.telemetry:
                if s.unit is not None and s.unit not in units:
                    out.append(f"{t.type_id}.{s.name}: unknown telemetry unit {s.unit!r}")
                if s.warn_above is not None and s.critical_above is not None \
                        and s.critical_above < s.warn_above:
                    out.append(f"{t.type_id}.{s.name}: critical_above below warn_above")
                if s.warn_below is not None and s.critical_below is not None \
                        and s.critical_below > s.warn_below:
                    out.append(f"{t.type_id}.{s.name}: critical_below above warn_below")
            for i in t.interfaces:
                if self.protocol(i) is None:
                    out.append(f"{t.type_id}: unknown interface {i}")
            out.extend(self._inheritance_problems(t))
        for c in self.robot_classes:
            if not c.class_id.startswith("robot.") or not _valid_id(c.class_id):
                out.append(f"{c.class_id}: robot classes use the robot.* prefix")
            domains(c.class_id, c.domains)
            for slot in c.slots:
                if slot.max is not None and slot.max < slot.min:
                    out.append(f"{c.class_id}.{slot.slot}: max below min")
                for pattern in slot.types:
                    if not (self.match(pattern) or self.match_classes(pattern)):
                        out.append(f"{c.class_id}.{slot.slot}: nothing matches {pattern}")
            for i in c.interfaces:
                if self.protocol(i) is None:
                    out.append(f"{c.class_id}: unknown interface {i}")
        for proto in self.protocols:
            if not _valid_id(proto.protocol_id):
                out.append(f"{proto.protocol_id}: invalid id")
            domains(proto.protocol_id, proto.domains)
        return out

    def _inheritance_problems(self, t: DeviceType) -> list[str]:
        """A specialization may tighten its parent, never weaken safety."""
        if t.extends is None:
            return []
        parent = self.type(t.extends)
        if parent is None:
            return [f"{t.type_id}: extends unknown type {t.extends}"]
        out = []
        if parent.category != t.category:
            out.append(f"{t.type_id}: category {t.category} differs from parent "
                       f"{parent.type_id} ({parent.category})")
        if parent.safety_relevant and not t.safety_relevant:
            out.append(f"{t.type_id}: cannot clear safety_relevant inherited from "
                       f"{parent.type_id}")
        if parent.safety_relevant:
            for own in t.telemetry:
                base = parent.signal(own.name)
                if base is not None and _loosens(base, own):
                    out.append(f"{t.type_id}.{own.name}: loosens a limit of safety-relevant "
                               f"{parent.type_id}")
        return out

    def stats(self) -> dict[str, Any]:
        by_cat: dict[str, int] = {}
        for t in self.device_types:
            by_cat[t.category] = by_cat.get(t.category, 0) + 1
        layers: dict[str, int] = {}
        for p in self.protocols:
            layers[p.layer] = layers.get(p.layer, 0) + 1
        return {"device_types": len(self.device_types), "robot_classes": len(self.robot_classes),
                "protocols": len(self.protocols),
                "safety_relevant_types": sum(t.safety_relevant for t in self.device_types),
                "by_category": dict(sorted(by_cat.items())),
                "protocols_by_layer": dict(sorted(layers.items()))}


_ORDER = (Sensitivity.PUBLIC, Sensitivity.INTERNAL, Sensitivity.RESTRICTED)


def most_sensitive(*levels: Sensitivity) -> Sensitivity:
    return max(levels, key=_ORDER.index)


def _loosens(base: SignalSpec, own: SignalSpec) -> bool:
    """True if ``own`` widens or drops any limit ``base`` sets."""
    def wider(a: float | None, b: float | None, upper: bool) -> bool:
        if a is None:
            return False
        if b is None:
            return True
        return b > a if upper else b < a
    return (wider(base.warn_above, own.warn_above, True)
            or wider(base.critical_above, own.critical_above, True)
            or wider(base.warn_below, own.warn_below, False)
            or wider(base.critical_below, own.critical_below, False))


def _merge_named(parent: tuple[Any, ...], own: tuple[Any, ...], key: str) -> tuple[Any, ...]:
    """Parent entries, replaced or extended by the child's entries with the same key."""
    mine = {getattr(x, key): x for x in own}
    merged = [mine.pop(getattr(x, key), x) for x in parent]
    return tuple(merged) + tuple(mine.values())


def _union(*groups: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(dict.fromkeys(x for g in groups for x in g))


def _resolve_inheritance(declared: dict[str, DeviceType]) -> dict[str, DeviceType]:
    """Effective types: each child merged over its ancestors. A cycle is an error; a
    missing parent leaves the child as declared (``problems`` reports it)."""
    resolved: dict[str, DeviceType] = {}

    def resolve(tid: str, seen: tuple[str, ...]) -> DeviceType:
        if tid in resolved:
            return resolved[tid]
        t = declared[tid]
        if t.extends is None or t.extends not in declared:
            resolved[tid] = t
            return t
        if t.extends in seen or t.extends == tid:
            raise SchemaError("device type inheritance cycle", chain=[*seen, tid, t.extends])
        p = resolve(t.extends, (*seen, tid))
        merged = dataclasses.replace(
            t,
            description=t.description or p.description,
            domains=_union(p.domains, t.domains),
            aliases=_union(p.aliases, t.aliases),
            properties=_merge_named(p.properties, t.properties, "name"),
            interfaces=_union(p.interfaces, t.interfaces),
            provides=_union(p.provides, t.provides),
            telemetry=_merge_named(p.telemetry, t.telemetry, "name"),
            maintenance=_merge_named(p.maintenance, t.maintenance, "item"),
            failure_modes=_union(p.failure_modes, t.failure_modes),
            safety_relevant=t.safety_relevant or p.safety_relevant,
            typical_parents=_union(p.typical_parents, t.typical_parents),
            references=_union(p.references, t.references),
            semantic=_union(p.semantic, t.semantic),
            sensitivity=most_sensitive(p.sensitivity, t.sensitivity),
        )
        resolved[tid] = merged
        return merged

    for tid in declared:
        resolve(tid, ())
    return resolved


def matches(type_id: str, pattern: str) -> bool:
    """``sensor.imu`` matches itself and its children; globs (``sensor.lidar.*``) as usual."""
    return type_id == pattern or type_id.startswith(pattern + ".") or \
        fnmatch.fnmatchcase(type_id, pattern) or \
        (pattern.endswith(".*") and type_id == pattern[:-2])


def _valid_id(value: str) -> bool:
    parts = value.split(".")
    return len(parts) >= 2 and all(p and all(ch.islower() or ch.isdigit() or ch == "_"
                                             for ch in p) for p in parts)


def load_files(paths: Iterable[str | Path], *, name: str = "catalog") -> DeviceCatalog:
    """Load catalog fragments (``device_types`` / ``robot_classes`` / ``protocols``)."""
    data: dict[str, list[Any]] = {"device_types": [], "robot_classes": [], "protocols": []}
    for path in paths:
        doc = json.loads(Path(path).read_text("utf-8"))
        unknown = set(doc) - set(data) - {"$schema", "name", "meta"}
        if unknown:
            raise SchemaError("unknown keys in catalog file", path=str(path),
                              keys=sorted(unknown))
        for key in data:
            data[key].extend(doc.get(key, []))
    return DeviceCatalog.from_dict({"$schema": "ar.device_catalog/1.0", "name": name, **data})


_BUILTIN: DeviceCatalog | None = None


def builtin_catalog() -> DeviceCatalog:
    """The catalog that ships with AIR (cached)."""
    global _BUILTIN
    if _BUILTIN is None:
        _BUILTIN = load_files(sorted(DATA_DIR.glob("*.json")), name="air-builtin")
    return _BUILTIN


def project_catalog(root: str | Path = ".") -> DeviceCatalog:
    """Built-in catalog plus a project's ``.air/hardware/*.json`` extensions."""
    extra = sorted((Path(root) / ".air" / "hardware").glob("*.json"))
    base = builtin_catalog()
    return base.merged(load_files(extra, name="project"), name="project") if extra else base
