"""Check robot manifests against the hardware catalog, and derive what to watch and maintain.

``check_manifest`` reports what a manifest gets wrong or leaves unknown.

AIR never requires particular hardware. The catalog *describes* robots; it does not
prescribe them. So there are two kinds of finding:

- **Data errors** (always errors): unknown type or robot-class ids, missing or invalid
  values for a type the manifest itself chose, wrong units or dimensions, and attached
  documents that changed. The manifest says something false or unusable.
- **Composition advice** (warnings; errors only with ``strict=True``): a robot class's
  typical slots under- or over-filled, domain-pack rules ("a lithium pack usually has a
  BMS"), untyped components, interfaces a type is not known to speak. A robot built
  differently is still a valid robot.

``telemetry_limits`` and ``maintenance_plan`` turn the catalog into per-component
monitoring thresholds and maintenance items, so health monitoring and maintenance
planning start from the hardware the robot actually has. A component's own limits
(from its datasheet) override the catalog's typical values, and every limit says where
it came from.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..contracts.manifest import ComponentManifest, ComponentSpec, LimitOverride
from ..core.errors import UnitError
from ..core.units import dimension_of
from .catalog import (
    DeviceCatalog,
    DeviceType,
    MaintenanceItem,
    PropertyKind,
    PropertySpec,
    SignalSpec,
    matches,
)

__requirements__ = ("REQ03", "REQ13", "REQ16", "D14")


class Level(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    level: Level
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.value}: {self.where}: {self.message}"


@dataclass(frozen=True)
class ComponentLimit:
    component_id: str
    type_id: str
    signal: SignalSpec
    #: ``catalog:<type_id>`` or ``manifest:<component_id>`` (with the override's reason).
    source: str = ""


@dataclass(frozen=True)
class ComponentMaintenance:
    component_id: str
    type_id: str
    item: MaintenanceItem
    safety_relevant: bool


def check_manifest(manifest: ComponentManifest, catalog: DeviceCatalog, *,
                   strict: bool = False, root: str | Path | None = None,
                   domains: Any = None) -> list[Finding]:
    """Findings for a manifest. ``strict`` turns untyped components into errors.
    With ``root``, attached documents are checked against their pinned digests. With
    ``domains`` (a :class:`~agentic_robotics.hardware.domains.DomainLibrary`), domain
    rules are applied too."""
    out: list[Finding] = []
    for c in manifest.components:
        out.extend(_check_component(c, catalog, strict))
        out.extend(_check_limits(c, catalog))
        if root is not None:
            out.extend(_check_documents(c, Path(root)))
    out.extend(_check_class(manifest, catalog, strict))
    if domains is not None:
        out.extend(domains.check(manifest, catalog, strict=strict))
    return out


def _check_limits(c: ComponentSpec, catalog: DeviceCatalog) -> list[Finding]:
    t = component_type(c, catalog)
    if t is None:
        return []
    out = []
    for o in c.limits:
        base = t.signal(o.signal)
        if base is None:
            out.append(Finding(Level.WARNING, c.component_id,
                               f"limit override for {o.signal}, which {t.type_id} does not "
                               f"define"))
            continue
        if not o.reason:
            out.append(Finding(Level.WARNING, c.component_id,
                               f"limit override for {o.signal} has no reason (cite the "
                               f"datasheet or test)"))
        if t.safety_relevant and _widens(base, o):
            out.append(Finding(Level.WARNING, c.component_id,
                               f"{o.signal}: override is looser than the typical limit of a "
                               f"safety-relevant {t.type_id}; confirm it against the "
                               f"datasheet"))
    return out


def _widens(base: SignalSpec, o: LimitOverride) -> bool:
    def wider(a: float | None, b: float | None, upper: bool) -> bool:
        return a is not None and b is not None and (b > a if upper else b < a)
    return (wider(base.warn_above, o.warn_above, True)
            or wider(base.critical_above, o.critical_above, True)
            or wider(base.warn_below, o.warn_below, False)
            or wider(base.critical_below, o.critical_below, False))


def _check_documents(c: ComponentSpec, root: Path) -> list[Finding]:
    from ..provenance.project import file_digest

    out = []
    for d in c.documents:
        path = root / d.path
        if not path.is_file():
            out.append(Finding(Level.ERROR, c.component_id, f"document missing: {d.path}"))
        elif file_digest(path) != d.digest:
            out.append(Finding(Level.ERROR, c.component_id,
                               f"document changed since it was attached: {d.path}"))
    return out


def _check_component(c: ComponentSpec, catalog: DeviceCatalog, strict: bool) -> list[Finding]:
    where = c.component_id
    if c.type_id is None:
        level = Level.ERROR if strict else Level.WARNING
        return [Finding(level, where, f"no catalog type_id (kind {c.kind!r} is free text)")]
    t = catalog.type(c.type_id)
    if t is None:
        if catalog.robot_class(c.type_id) is not None:
            return []                       # a sub-robot (e.g. the arm of a mobile manipulator)
        return [Finding(Level.ERROR, where, f"unknown device type {c.type_id}")]
    out: list[Finding] = []
    for spec in t.properties:
        if spec.required and spec.name not in c.properties:
            out.append(Finding(Level.ERROR, where, f"missing required property {spec.name}"
                                                   + (f" [{spec.unit}]" if spec.unit else "")))
    for name, value in c.properties.items():
        declared = t.property(name)
        if declared is None:
            out.append(Finding(Level.WARNING, where,
                               f"property {name} is not defined for {t.type_id}"))
            continue
        problem = _value_problem(declared, value)
        if problem:
            out.append(Finding(Level.ERROR, where, f"{name}: {problem}"))
    for i in c.interfaces:
        if catalog.protocol(i.kind) is None:
            out.append(Finding(Level.WARNING, where,
                               f"interface {i.name} kind {i.kind!r} is not a catalog protocol"))
        elif t.interfaces and i.kind not in t.interfaces:
            out.append(Finding(Level.WARNING, where,
                               f"{t.type_id} is not known to speak {i.kind}"))
    return out


def _value_problem(spec: PropertySpec, value: Any) -> str | None:
    kind = spec.kind
    if kind is PropertyKind.QUANTITY:
        if isinstance(value, bool):
            return "expected a number or {value, unit}"
        if isinstance(value, int | float):
            return None                                   # stated in the spec's unit
        if isinstance(value, dict) and set(value) == {"value", "unit"} \
                and isinstance(value["value"], int | float):
            try:
                if spec.unit and dimension_of(value["unit"]) != dimension_of(spec.unit):
                    return f"unit {value['unit']} is not a {dimension_of(spec.unit)}"
            except UnitError:
                return f"unknown unit {value['unit']!r}"
            return None
        return "expected a number or {value, unit}"
    if kind is PropertyKind.NUMBER:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return None if ok else "expected a number"
    if kind is PropertyKind.INTEGER:
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else "expected an integer"
    if kind is PropertyKind.BOOL:
        return None if isinstance(value, bool) else "expected true or false"
    if kind is PropertyKind.ENUM:
        return None if value in spec.values else f"expected one of {list(spec.values)}"
    return None if isinstance(value, str) else "expected a string"


def _check_class(manifest: ComponentManifest, catalog: DeviceCatalog,
                 strict: bool = False) -> list[Finding]:
    """Slot counts are the class's *typical* composition: advice, not a requirement."""
    advice = Level.ERROR if strict else Level.WARNING
    if manifest.robot_class is None:
        return []
    rc = catalog.robot_class(manifest.robot_class)
    if rc is None:
        return [Finding(Level.ERROR, manifest.robot_id,
                        f"unknown robot class {manifest.robot_class}")]
    out: list[Finding] = []
    typed = [c for c in manifest.components if c.type_id]
    for slot in rc.slots:
        n = sum(any(matches(c.type_id or "", p) for p in slot.types) for c in typed)
        if n < slot.min:
            out.append(Finding(advice, f"{manifest.robot_id}.{slot.slot}",
                               f"a typical {rc.class_id} has at least {slot.min} of "
                               f"{list(slot.types)}, found {n}"))
        if slot.max is not None and n > slot.max:
            out.append(Finding(Level.WARNING, f"{manifest.robot_id}.{slot.slot}",
                               f"a typical {rc.class_id} has at most {slot.max}, found {n}"))
    return out


def component_type(c: ComponentSpec, catalog: DeviceCatalog) -> DeviceType | None:
    return catalog.type(c.type_id) if c.type_id else None


def telemetry_limits(manifest: ComponentManifest, catalog: DeviceCatalog
                     ) -> list[ComponentLimit]:
    """Signals to monitor per component: the component's own limits where it states them,
    otherwise the catalog's typical limits."""
    out: list[ComponentLimit] = []
    for c in manifest.components:
        t = component_type(c, catalog)
        if t is None:
            continue
        overrides = {o.signal: o for o in c.limits}
        for s in t.telemetry:
            o = overrides.get(s.name)
            if o is None:
                out.append(ComponentLimit(c.component_id, t.type_id, s, f"catalog:{t.type_id}"))
                continue
            merged = dataclasses.replace(
                s, **{k: getattr(o, k) for k in ("warn_above", "critical_above", "warn_below",
                                                  "critical_below") if getattr(o, k) is not None})
            source = f"manifest:{c.component_id}" + (f" ({o.reason})" if o.reason else "")
            out.append(ComponentLimit(c.component_id, t.type_id, merged, source))
    return out


def limit_for(manifest: ComponentManifest, catalog: DeviceCatalog, component_id: str,
              signal: str) -> ComponentLimit | None:
    return next((lim for lim in telemetry_limits(manifest, catalog)
                 if lim.component_id == component_id and lim.signal.name == signal), None)


def maintenance_plan(manifest: ComponentManifest, catalog: DeviceCatalog
                     ) -> list[ComponentMaintenance]:
    """Maintenance items per component; safety-relevant components first."""
    out: list[ComponentMaintenance] = []
    for c in manifest.components:
        t = component_type(c, catalog)
        if t is not None:
            out.extend(ComponentMaintenance(c.component_id, t.type_id, m, t.safety_relevant)
                       for m in t.maintenance)
    return sorted(out, key=lambda m: (not m.safety_relevant, m.component_id, m.item.item))


def evaluate(signal: SignalSpec, value: float) -> str:
    """``ok``, ``warn`` or ``critical`` for a reading against a signal's limits."""
    if (signal.critical_above is not None and value > signal.critical_above) or \
            (signal.critical_below is not None and value < signal.critical_below):
        return "critical"
    if (signal.warn_above is not None and value > signal.warn_above) or \
            (signal.warn_below is not None and value < signal.warn_below):
        return "warn"
    return "ok"
