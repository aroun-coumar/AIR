"""Health contracts: levels, expected items, capability gates and reports (ADR 0015).

Patterns adopted (re-implemented, not depended on):

- **Expected-component manifest, STALE level, worst-of rollup, "other" bucket,
  critical items published immediately**: ROS ``diagnostic_aggregator``
  (``generic_analyzer.cpp``, BSD-3). Levels use its numbering (OK 0, WARN 1, ERROR 2,
  STALE 3), so the rollup is the plain maximum and an item that stops reporting can
  never look OK.
- **Report vs gate**: Microduck's fall verdict reports, it does not command recovery.
  AIR turns a bad health level into a RESTRICTED or UNAVAILABLE capability profile the
  runtime admits against; recovery the robot owns is never commanded by AIR.

The expected items are *derived* from a :class:`ComponentManifest` and the hardware
catalog's telemetry limits (:func:`derive_manifest`), so nothing is configured twice.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts.capability import ProfileStatus
from ..contracts.manifest import ComponentManifest
from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ13", "REQ14", "G10", "G11")


class HealthLevel(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    STALE = "stale"     # no fresh report: the state is unknown, which is never OK

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def bad(self) -> bool:
        """ERROR or STALE: the component cannot be trusted right now."""
        return self in (HealthLevel.ERROR, HealthLevel.STALE)

    @staticmethod
    def worst(levels: Iterable[HealthLevel]) -> HealthLevel:
        return max(levels, key=lambda lv: lv.rank, default=HealthLevel.OK)


_RANK = {HealthLevel.OK: 0, HealthLevel.WARN: 1, HealthLevel.ERROR: 2, HealthLevel.STALE: 3}


@dataclass(frozen=True)
class HealthStatus:
    """One reporter's (or one rollup group's) level at a time."""

    name: str                       # ``<component_id>.<signal>``, a monitor or a group path
    level: HealthLevel
    message: str = ""
    path: str = ""                  # position in the component tree: ``robot/base/battery``
    values: dict[str, float] = field(default_factory=dict)
    observed_at: float | None = None
    source: str = ""                # who reported it (adapter, monitor, probe, transport)
    critical: bool = False


@dataclass(frozen=True)
class ExpectedItem:
    """A reporter the robot must have. Missing is ERROR; silent for ``timeout_s`` is STALE."""

    name: str
    path: str
    component_id: str | None = None
    signal: str | None = None
    unit: str | None = None
    warn_above: float | None = None
    critical_above: float | None = None
    warn_below: float | None = None
    critical_below: float | None = None
    timeout_s: float = 5.0
    critical: bool = False          # safety relevant: published on the fast path
    limit_source: str = ""          # ``catalog:<type>`` or ``manifest:<component>``

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise SchemaError("timeout_s must be positive", item=self.name)


@dataclass(frozen=True)
class CapabilityGate:
    """How bad health on matching items restricts a capability profile.

    ``match`` are globs over item names *and* paths; ``capability`` ``*`` means every
    profile in the report. Gates only ever downgrade a profile, never upgrade it."""

    capability: str
    match: tuple[str, ...]
    on_warn: ProfileStatus = ProfileStatus.AVAILABLE
    on_error: ProfileStatus = ProfileStatus.UNAVAILABLE
    on_stale: ProfileStatus = ProfileStatus.UNAVAILABLE
    reason: str = ""

    def matches(self, status: HealthStatus) -> bool:
        return any(fnmatch.fnmatchcase(status.name, p) or
                   (status.path and fnmatch.fnmatchcase(status.path, p))
                   for p in self.match)

    def status_for(self, level: HealthLevel) -> ProfileStatus:
        return {HealthLevel.OK: ProfileStatus.AVAILABLE, HealthLevel.WARN: self.on_warn,
                HealthLevel.ERROR: self.on_error, HealthLevel.STALE: self.on_stale}[level]


@contract("health_manifest", version="1.0")
class HealthManifest(Contract):
    robot_id: str
    expected: tuple[ExpectedItem, ...] = ()
    gates: tuple[CapabilityGate, ...] = ()
    #: Skills that run recovery the robot owns (a fall recovery, self-righting). AIR
    #: never commands them; it waits until observations show the robot healthy again.
    robot_owned_recovery: tuple[str, ...] = ()
    default_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        names = [e.name for e in self.expected]
        if len(names) != len(set(names)):
            raise SchemaError("duplicate expected health items", robot_id=self.robot_id)

    def item(self, name: str) -> ExpectedItem | None:
        return next((e for e in self.expected if e.name == name), None)

    def for_component(self, component_id: str) -> list[ExpectedItem]:
        return [e for e in self.expected if e.component_id == component_id]


@contract("health_report", version="1.0")
class HealthReport(Contract):
    robot_id: str
    observed_at: float
    level: HealthLevel
    items: tuple[HealthStatus, ...] = ()      # expected reporters
    groups: tuple[HealthStatus, ...] = ()     # worst-of rollup by component tree
    other: tuple[HealthStatus, ...] = ()      # reporters nobody expected
    critical: tuple[str, ...] = ()            # critical items that are not OK

    def item(self, name: str) -> HealthStatus | None:
        return next((s for s in (*self.items, *self.other) if s.name == name), None)

    def group(self, path: str) -> HealthStatus | None:
        return next((g for g in self.groups if g.path == path), None)

    def bad(self) -> list[HealthStatus]:
        return [s for s in (*self.items, *self.other) if s.level.bad]

    def render(self) -> str:
        lines = [f"{self.robot_id}: {self.level.value.upper()}"]
        for g in sorted(self.groups, key=lambda g: g.path):
            if g.path == self.robot_id:
                continue
            depth = g.path.count("/")
            lines.append(f"{'  ' * depth}{g.path.rsplit('/', 1)[-1]}: {g.level.value}")
            for s in sorted(self.items, key=lambda s: s.name):
                if s.path.rsplit("/", 1)[0] == g.path:
                    lines.append(f"{'  ' * (depth + 1)}{s.name}: {s.level.value}"
                                 + (f" ({s.message})" if s.message else ""))
        if self.other:
            lines.append("other:")
            lines.extend(f"  {s.name}: {s.level.value}" + (f" ({s.message})" if s.message
                                                           else "") for s in self.other)
        return "\n".join(lines)


# -- deriving the expected manifest -----------------------------------------------------
def component_path(manifest: ComponentManifest, component_id: str) -> str:
    """``robot_id/<ancestors>/component_id`` along the ``parent`` chain."""
    chain: list[str] = []
    current = manifest.component(component_id)
    while current is not None and current.component_id not in chain:
        chain.append(current.component_id)
        current = manifest.component(current.parent) if current.parent else None
    return "/".join([manifest.robot_id, *reversed(chain)])


def derive_manifest(manifest: ComponentManifest, catalog: Any, *,
                    timeout_s: float = 5.0,
                    gates: Iterable[CapabilityGate] = (),
                    robot_owned_recovery: Iterable[str] = (),
                    extra: Iterable[ExpectedItem] = ()) -> HealthManifest:
    """Expected items from the catalog telemetry limits of each typed component (the
    component's own limit overrides win), plus default gates:

    - a component whose catalog type ``provides`` a nominal capability gates it;
    - safety-relevant components gate every capability (WARN restricts, ERROR and STALE
      make it unavailable).
    """
    from ..hardware.validate import component_type, telemetry_limits

    items: list[ExpectedItem] = []
    for lim in telemetry_limits(manifest, catalog):
        t = catalog.type(lim.type_id)
        s = lim.signal
        items.append(ExpectedItem(
            name=f"{lim.component_id}.{s.name}",
            path=f"{component_path(manifest, lim.component_id)}/{s.name}",
            component_id=lim.component_id, signal=s.name, unit=s.unit,
            warn_above=s.warn_above, critical_above=s.critical_above,
            warn_below=s.warn_below, critical_below=s.critical_below,
            timeout_s=timeout_s, critical=bool(t and t.safety_relevant),
            limit_source=lim.source))
    items.extend(extra)

    derived: list[CapabilityGate] = []
    nominal = set(manifest.nominal_capabilities)
    safety: list[str] = []
    for c in manifest.components:
        t = component_type(c, catalog)
        if t is None:
            continue
        if t.safety_relevant:
            safety.append(f"{c.component_id}.*")
        for cap in sorted(nominal & set(t.provides)):
            derived.append(CapabilityGate(cap, (f"{c.component_id}.*",),
                                          on_warn=ProfileStatus.RESTRICTED,
                                          reason=f"{c.component_id} provides {cap}"))
    if safety:
        derived.append(CapabilityGate("*", tuple(safety), on_warn=ProfileStatus.RESTRICTED,
                                      reason="safety-relevant component"))
    return HealthManifest(robot_id=manifest.robot_id, expected=tuple(items),
                          gates=(*derived, *gates),
                          robot_owned_recovery=tuple(robot_owned_recovery),
                          default_timeout_s=timeout_s)
