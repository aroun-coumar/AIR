"""Health aggregation: expected items, STALE, worst-of rollup, "other", fast path.

    agg = HealthAggregator(derive_manifest(manifest, builtin_catalog()))
    agg.on_critical(lambda s: print("now:", s.name, s.level))
    agg.observe("pack.pack_temperature", 41.0, at=clock.now(), unit="degC")
    report = agg.report(clock.now())          # HealthReport, worst-of by component tree

Rules (ROS ``generic_analyzer`` semantics, tightened):

- an expected item that never reported is ERROR ``missing``;
- an item whose last report is older than its ``timeout_s`` is STALE, and so is one
  whose transport reported it lost (liveliness token gone, MQTT last will) after its
  last report: STALE is derived from facts, never configured, and never OK;
- a group's level is the worst of its children;
- reporters nobody expected go to the ``other`` bucket; they count towards the robot
  level while fresh, but an unexpected reporter going silent does not make the robot
  STALE (nobody depended on it);
- critical items that turn ERROR or STALE are published to ``on_critical`` listeners
  immediately, without waiting for the next report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..contracts.observation import Observation
from ..core.errors import SchemaError, UnitError
from ..core.units import convert
from .model import ExpectedItem, HealthLevel, HealthManifest, HealthReport, HealthStatus

__requirements__ = ("REQ13", "REQ14", "G11")

CriticalListener = Callable[[HealthStatus], None]


def evaluate_item(item: ExpectedItem, value: float) -> tuple[HealthLevel, str]:
    if (item.critical_above is not None and value > item.critical_above) or \
            (item.critical_below is not None and value < item.critical_below):
        return HealthLevel.ERROR, _limit_text(item, value, "critical")
    if (item.warn_above is not None and value > item.warn_above) or \
            (item.warn_below is not None and value < item.warn_below):
        return HealthLevel.WARN, _limit_text(item, value, "warn")
    return HealthLevel.OK, ""


def _limit_text(item: ExpectedItem, value: float, kind: str) -> str:
    above = getattr(item, f"{kind}_above")
    if above is not None and value > above:
        bound = f"> {above:g}"
    else:
        bound = f"< {getattr(item, f'{kind}_below'):g}"
    return f"{value:g} {item.unit or ''} {bound} ({kind}; {item.limit_source or 'limit'})"


class HealthAggregator:
    def __init__(self, manifest: HealthManifest) -> None:
        self.manifest = manifest
        self._expected = {e.name: e for e in manifest.expected}
        self._last: dict[str, HealthStatus] = {}
        self._lost: dict[str, tuple[float, str]] = {}
        self._listeners: list[CriticalListener] = []
        self._published_bad: set[str] = set()

    def on_critical(self, listener: CriticalListener) -> None:
        self._listeners.append(listener)

    # -- input --------------------------------------------------------------------------
    def report_status(self, status: HealthStatus) -> HealthStatus:
        if status.observed_at is None:
            raise SchemaError("health status needs observed_at", name=status.name)
        item = self._expected.get(status.name)
        if item is not None:
            status = replace(status, path=item.path, critical=item.critical or status.critical)
        self._last[status.name] = status
        self._lost.pop(status.name, None)
        self._fast_path(status)
        return status

    def observe(self, name: str, value: float, *, at: float, unit: str | None = None,
                source: str = "") -> HealthStatus:
        """A numeric reading, checked against the item's limits (converted to its unit)."""
        item = self._expected.get(name)
        if item is None:
            return self.report_status(HealthStatus(name, HealthLevel.OK, values={"value": value},
                                                   observed_at=at, source=source))
        reading = float(value)
        if unit and item.unit and unit != item.unit:
            try:
                reading = convert(reading, unit, item.unit)
            except UnitError as exc:
                return self.report_status(HealthStatus(
                    name, HealthLevel.ERROR, f"cannot compare {unit} with {item.unit}: "
                    f"{exc.message}", observed_at=at, source=source))
        level, message = evaluate_item(item, reading)
        return self.report_status(HealthStatus(name, level, message, values={"value": reading},
                                               observed_at=at, source=source))

    def observe_observation(self, obs: Observation, name: str | None = None) -> HealthStatus:
        """An :class:`Observation` (numeric value), timed by when it was acquired."""
        if isinstance(obs.value, bool) or not isinstance(obs.value, (int, float)):
            raise SchemaError("health readings must be numeric", signal=obs.signal)
        return self.observe(name or obs.signal, float(obs.value), at=obs.acquired_at,
                            unit=obs.unit, source=obs.source)

    def lost(self, target: str, *, at: float, reason: str = "transport lost") -> list[str]:
        """A transport fact: ``target`` (an item name or a component id) is gone. Its items
        are STALE until they report again."""
        names = [n for n, e in self._expected.items() if target in (n, e.component_id)]
        names += [n for n in self._last if n == target and n not in names]
        for n in names:
            self._lost[n] = (at, reason)
            self._fast_path(HealthStatus(n, HealthLevel.STALE, reason,
                                         path=self._expected[n].path if n in self._expected
                                         else "", observed_at=at,
                                         critical=self._critical(n)))
        return names

    # -- evaluation ---------------------------------------------------------------------
    def _critical(self, name: str) -> bool:
        item = self._expected.get(name)
        return bool(item and item.critical)

    def _fast_path(self, status: HealthStatus) -> None:
        if not self._critical(status.name):
            return
        if status.level.bad and status.name not in self._published_bad:
            self._published_bad.add(status.name)
            for listener in list(self._listeners):
                listener(status)
        elif not status.level.bad:
            self._published_bad.discard(status.name)

    def _current(self, name: str, now: float, timeout_s: float,
                 item: ExpectedItem | None) -> HealthStatus:
        last = self._last.get(name)
        path = item.path if item else f"{self.manifest.robot_id}/other/{name}"
        critical = bool(item and item.critical)
        if last is None:
            return HealthStatus(name, HealthLevel.ERROR, "missing (never reported)", path=path,
                                critical=critical)
        assert last.observed_at is not None
        lost = self._lost.get(name)
        if lost is not None:
            return replace(last, level=HealthLevel.STALE, message=lost[1], path=path)
        age = now - last.observed_at
        if age > timeout_s:
            return replace(last, level=HealthLevel.STALE,
                           message=f"no report for {age:.3g} s (timeout {timeout_s:g} s)",
                           path=path)
        return replace(last, path=path)

    def check_stale(self, now: float) -> list[HealthStatus]:
        """Fast path for silence: publish critical items that have just gone STALE."""
        out = []
        for name, item in self._expected.items():
            if not item.critical:
                continue
            s = self._current(name, now, item.timeout_s, item)
            if s.level.bad and name not in self._published_bad:
                self._fast_path(s)
                out.append(s)
        return out

    def report(self, now: float) -> HealthReport:
        items = [self._current(n, now, e.timeout_s, e) for n, e in self._expected.items()]
        other = [self._current(n, now, self.manifest.default_timeout_s, None)
                 for n in sorted(self._last) if n not in self._expected]
        groups: dict[str, list[HealthLevel]] = {}
        # An unexpected reporter that went quiet is listed but does not roll up.
        counted_other = [s for s in other if s.level is not HealthLevel.STALE]
        for s in (*items, *counted_other):
            parts = s.path.split("/")
            for i in range(1, len(parts)):
                groups.setdefault("/".join(parts[:i]), []).append(s.level)
        if other:
            groups.setdefault(f"{self.manifest.robot_id}/other", [])
        rollup = tuple(HealthStatus(p, HealthLevel.worst(levels), path=p)
                       for p, levels in sorted(groups.items()))
        counted = [s.level for s in (*items, *counted_other)]
        return HealthReport(
            robot_id=self.manifest.robot_id, observed_at=now,
            level=HealthLevel.worst(counted), items=tuple(items), groups=rollup,
            other=tuple(other),
            critical=tuple(s.name for s in items if s.critical and s.level is not HealthLevel.OK))
