"""Rate and latency monitors (ROS ``diagnostic_updater`` FrequencyStatus/TimeStampStatus
semantics, re-implemented).

    rate = FrequencyMonitor("control_loop.rate", min_hz=45, max_hz=55, tolerance=0.1,
                            window_s=2.0)
    rate.tick(t)                       # once per loop iteration
    agg.report_status(rate.status(now))

- Frequency: no events in the window is ERROR; below ``min_hz * (1 - tolerance)`` or above
  ``max_hz * (1 + tolerance)`` is WARN.
- Latency (receipt minus acquisition): a stamp further in the future than
  ``max_future_s`` is ERROR (clocks disagree); the window maximum above ``error_s`` is
  ERROR; the window p95 above ``warn_s`` is WARN; no samples is WARN.
"""

from __future__ import annotations

import math
from collections import deque

from ..core.errors import SchemaError
from .model import HealthLevel, HealthStatus

__requirements__ = ("REQ12", "REQ13")


class FrequencyMonitor:
    def __init__(self, name: str, *, min_hz: float, max_hz: float | None = None,
                 tolerance: float = 0.1, window_s: float = 5.0, start: float | None = None
                 ) -> None:
        if min_hz < 0 or (max_hz is not None and max_hz < min_hz):
            raise SchemaError("need 0 <= min_hz <= max_hz", name=name)
        if window_s <= 0 or not 0 <= tolerance < 1:
            raise SchemaError("window_s must be positive and 0 <= tolerance < 1", name=name)
        self.name, self.min_hz, self.max_hz = name, min_hz, max_hz
        self.tolerance, self.window_s, self.start = tolerance, window_s, start
        self._events: deque[float] = deque()

    def tick(self, t: float) -> None:
        if self.start is None:
            self.start = t
        self._events.append(t)

    def frequency(self, now: float) -> float:
        while self._events and self._events[0] < now - self.window_s:
            self._events.popleft()
        span = self.window_s if self.start is None else min(self.window_s, now - self.start)
        return len(self._events) / span if span > 0 else 0.0

    def status(self, now: float) -> HealthStatus:
        hz = self.frequency(now)
        values = {"frequency_hz": round(hz, 6), "min_hz": self.min_hz,
                  "window_s": self.window_s, "events": float(len(self._events))}
        if self.max_hz is not None:
            values["max_hz"] = self.max_hz
        if not self._events:
            level, msg = HealthLevel.ERROR, "no events recorded in the window"
        elif hz < self.min_hz * (1 - self.tolerance):
            level, msg = HealthLevel.WARN, f"frequency too low ({hz:.3g} Hz)"
        elif self.max_hz is not None and hz > self.max_hz * (1 + self.tolerance):
            level, msg = HealthLevel.WARN, f"frequency too high ({hz:.3g} Hz)"
        else:
            level, msg = HealthLevel.OK, ""
        return HealthStatus(self.name, level, msg, values=values, observed_at=now,
                            source="monitor:frequency")


class LatencyMonitor:
    def __init__(self, name: str, *, warn_s: float, error_s: float, window: int = 100,
                 max_future_s: float = 0.0) -> None:
        if not 0 < warn_s <= error_s or window < 1 or max_future_s < 0:
            raise SchemaError("need 0 < warn_s <= error_s, window >= 1, max_future_s >= 0",
                              name=name)
        self.name, self.warn_s, self.error_s = name, warn_s, error_s
        self.max_future_s = max_future_s
        self._samples: deque[float] = deque(maxlen=window)
        self._future = 0

    def record(self, acquired_at: float, received_at: float) -> float:
        latency = received_at - acquired_at
        if latency < -self.max_future_s:
            self._future += 1
        self._samples.append(latency)
        return latency

    def status(self, now: float) -> HealthStatus:
        samples = sorted(self._samples)
        if not samples:
            return HealthStatus(self.name, HealthLevel.WARN, "no samples", observed_at=now,
                                source="monitor:latency")
        p95 = samples[min(len(samples) - 1, math.ceil(0.95 * len(samples)) - 1)]
        worst = samples[-1]
        values = {"p95_s": p95, "max_s": worst, "samples": float(len(samples)),
                  "future_stamps": float(self._future)}
        if self._future:
            level, msg = HealthLevel.ERROR, f"{self._future} timestamp(s) in the future"
        elif worst > self.error_s:
            level, msg = HealthLevel.ERROR, f"latency {worst:.3g} s > {self.error_s:g} s"
        elif p95 > self.warn_s:
            level, msg = HealthLevel.WARN, f"p95 latency {p95:.3g} s > {self.warn_s:g} s"
        else:
            level, msg = HealthLevel.OK, ""
        self._future = 0
        return HealthStatus(self.name, level, msg, values=values, observed_at=now,
                            source="monitor:latency")
