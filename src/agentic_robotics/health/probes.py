"""Host probes: Linux pressure stall information (PSI), read with the standard library.

``/proc/pressure/{cpu,memory,io}`` (kernel >= 4.20, ``CONFIG_PSI``) contain lines like::

    some avg10=1.23 avg60=0.80 avg300=0.20 total=123456
    full avg10=0.00 avg60=0.00 avg300=0.00 total=0

``some`` is the share of time at least one task stalled on the resource, ``full`` the
share all non-idle tasks stalled. Where the files are absent (other kernels, macOS,
Windows, containers without PSI) the probe says *unsupported*, which is reported STALE:
no information is never OK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model import HealthLevel, HealthStatus

__requirements__ = ("REQ13", "G10")

RESOURCES = ("cpu", "memory", "io")


@dataclass(frozen=True)
class PsiLine:
    avg10: float
    avg60: float
    avg300: float
    total_us: int


@dataclass(frozen=True)
class PsiReading:
    resource: str
    some: PsiLine
    full: PsiLine | None = None


@dataclass
class PsiProbe:
    root: Path = field(default_factory=lambda: Path("/proc/pressure"))
    warn_some_avg10: float = 10.0      # % of time some task stalled
    error_full_avg10: float = 20.0     # % of time every task stalled
    prefix: str = "host.psi"

    @property
    def supported(self) -> bool:
        return any((self.root / r).is_file() for r in RESOURCES)

    def read(self) -> list[PsiReading]:
        out = []
        for r in RESOURCES:
            path = self.root / r
            try:
                text = path.read_text("ascii")
            except OSError:
                continue
            lines = {}
            for raw in text.splitlines():
                kind, _, rest = raw.partition(" ")
                fields = dict(part.split("=", 1) for part in rest.split() if "=" in part)
                try:
                    lines[kind] = PsiLine(float(fields["avg10"]), float(fields["avg60"]),
                                          float(fields["avg300"]), int(fields["total"]))
                except (KeyError, ValueError):
                    continue
            if "some" in lines:
                out.append(PsiReading(r, lines["some"], lines.get("full")))
        return out

    def statuses(self, now: float) -> list[HealthStatus]:
        readings = self.read()
        if not readings:
            return [HealthStatus(self.prefix, HealthLevel.STALE,
                                 f"unsupported: no PSI files under {self.root}",
                                 observed_at=now, source="probe:psi")]
        out = []
        for r in readings:
            values = {"some_avg10": r.some.avg10, "some_avg60": r.some.avg60,
                      "some_avg300": r.some.avg300}
            if r.full is not None:
                values.update(full_avg10=r.full.avg10, full_avg60=r.full.avg60,
                              full_avg300=r.full.avg300)
            if r.full is not None and r.full.avg10 > self.error_full_avg10:
                level, msg = HealthLevel.ERROR, f"all tasks stalled {r.full.avg10:g}% (10 s)"
            elif r.some.avg10 > self.warn_some_avg10:
                level, msg = HealthLevel.WARN, f"tasks stalled {r.some.avg10:g}% (10 s)"
            else:
                level, msg = HealthLevel.OK, ""
            out.append(HealthStatus(f"{self.prefix}.{r.resource}", level, msg, values=values,
                                    observed_at=now, source="probe:psi"))
        return out
