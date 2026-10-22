"""Diagnosis table: fault id -> debounce threshold, severity, delay before the error state
(foxBMS-2 ``diag`` pattern, re-implemented).

Every fault has an entry. A failed check increments the fault's counter, a passed check
decrements it. When the counter reaches ``threshold`` the fault is *confirmed*
(PENDING); after ``delay_s`` more it enters the error state (ACTIVE), which is what
readiness and the capability gate act on. It clears only when the counter is back at
zero, so a flapping signal cannot toggle the error state.

    diag = Diagnoser(builtin_table())
    diag.report("link_lost", ok=False, at=t)
    diag.state("link_lost").phase        # OK | DEBOUNCING | PENDING | ACTIVE

:func:`exercise` drives one entry through its whole life; the test suite runs it for
every entry of the built-in table, and projects can do the same for their own tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from .model import HealthLevel, HealthStatus
from .readiness import ALL_MODES, CheckResult, ReadinessContext

__requirements__ = ("REQ14", "G11")


class DiagSeverity(StrEnum):
    INFO = "info"          # recorded only
    WARNING = "warning"    # degrades
    ERROR = "error"        # the affected function must stop
    FATAL = "fatal"        # everything must stop: raises a failsafe flag

    @property
    def level(self) -> HealthLevel:
        return {DiagSeverity.INFO: HealthLevel.OK, DiagSeverity.WARNING: HealthLevel.WARN,
                DiagSeverity.ERROR: HealthLevel.ERROR,
                DiagSeverity.FATAL: HealthLevel.ERROR}[self]


class FaultPhase(StrEnum):
    OK = "ok"
    DEBOUNCING = "debouncing"   # failures seen, threshold not reached
    PENDING = "pending"         # confirmed, waiting out delay_s
    ACTIVE = "active"           # error state entered


@dataclass(frozen=True)
class DiagnosisEntry:
    fault_id: str
    description: str
    threshold: int = 1
    severity: DiagSeverity = DiagSeverity.ERROR
    delay_s: float = 0.0
    failure_class: str | None = None     # domain-pack failure id, e.g. ``D07.link_loss``
    signal: str | None = None            # the signal that is checked, for novelty matching

    def __post_init__(self) -> None:
        if self.threshold < 1 or self.delay_s < 0:
            raise SchemaError("threshold must be >= 1 and delay_s >= 0", fault=self.fault_id)


@contract("diagnosis_table", version="1.0")
class DiagnosisTable(Contract):
    entries: tuple[DiagnosisEntry, ...] = ()

    def __post_init__(self) -> None:
        ids = [e.fault_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise SchemaError("duplicate fault ids in diagnosis table")

    def get(self, fault_id: str) -> DiagnosisEntry:
        for e in self.entries:
            if e.fault_id == fault_id:
                return e
        raise SchemaError("unknown fault id", fault=fault_id)

    def problems(self, known_failure_classes: Iterable[str] = ()) -> list[str]:
        known = set(known_failure_classes)
        return [f"{e.fault_id}: unknown failure class {e.failure_class}"
                for e in self.entries
                if known and e.failure_class and e.failure_class not in known]


@dataclass
class FaultState:
    fault_id: str
    counter: int = 0
    phase: FaultPhase = FaultPhase.OK
    confirmed_at: float | None = None
    last_at: float | None = None


class Diagnoser:
    def __init__(self, table: DiagnosisTable) -> None:
        self.table = table
        self._states = {e.fault_id: FaultState(e.fault_id) for e in table.entries}

    def state(self, fault_id: str) -> FaultState:
        self.table.get(fault_id)
        return self._states[fault_id]

    def report(self, fault_id: str, *, ok: bool, at: float) -> FaultState:
        entry, s = self.table.get(fault_id), self._states[fault_id]
        s.last_at = at
        if ok:
            s.counter = max(s.counter - 1, 0)
            if s.counter == 0:
                s.phase, s.confirmed_at = FaultPhase.OK, None
        else:
            s.counter = min(s.counter + 1, entry.threshold)
            if s.counter >= entry.threshold and s.phase in (FaultPhase.OK,
                                                            FaultPhase.DEBOUNCING):
                s.phase, s.confirmed_at = FaultPhase.PENDING, at
            elif s.phase is FaultPhase.OK:
                s.phase = FaultPhase.DEBOUNCING
        self._advance(entry, s, at)
        return s

    def poll(self, at: float) -> list[FaultState]:
        """Advance delays; returns faults that entered the error state now."""
        out = []
        for e in self.table.entries:
            s = self._states[e.fault_id]
            if s.phase is FaultPhase.PENDING and self._advance(e, s, at):
                out.append(s)
        return out

    @staticmethod
    def _advance(entry: DiagnosisEntry, s: FaultState, at: float) -> bool:
        if s.phase is FaultPhase.PENDING and s.confirmed_at is not None \
                and at - s.confirmed_at >= entry.delay_s:
            s.phase = FaultPhase.ACTIVE
            return True
        return False

    def active(self) -> list[DiagnosisEntry]:
        return [e for e in self.table.entries
                if self._states[e.fault_id].phase is FaultPhase.ACTIVE]

    def statuses(self, now: float) -> list[HealthStatus]:
        """Pending faults are WARN; active faults take their severity's level."""
        self.poll(now)
        out = []
        for e in self.table.entries:
            s = self._states[e.fault_id]
            if s.phase is FaultPhase.PENDING:
                level = HealthLevel.WARN if e.severity is not DiagSeverity.INFO \
                    else HealthLevel.OK
            elif s.phase is FaultPhase.ACTIVE:
                level = e.severity.level
            else:
                continue
            out.append(HealthStatus(f"diag.{e.fault_id}", level,
                                    f"{e.description} ({s.phase.value})", observed_at=now,
                                    source="diagnosis",
                                    values={"counter": float(s.counter)}))
        return out


@dataclass
class DiagnosisCheck:
    """Readiness: active ERROR/FATAL faults block every mode; FATAL raises a failsafe."""

    diagnoser: Diagnoser
    check_id: str = "diagnosis"
    order: int = 15

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        self.diagnoser.poll(ctx.now)
        for e in self.diagnoser.active():
            if e.severity in (DiagSeverity.ERROR, DiagSeverity.FATAL):
                yield CheckResult(self.check_id, HealthLevel.ERROR, e.description,
                                  subject=e.fault_id, blocks=(ALL_MODES,),
                                  failsafe=(e.fault_id,) if e.severity is DiagSeverity.FATAL
                                  else ())
            elif e.severity is DiagSeverity.WARNING:
                yield CheckResult(self.check_id, HealthLevel.WARN, e.description,
                                  subject=e.fault_id)


def exercise(entry: DiagnosisEntry, *, t0: float = 0.0) -> list[str]:
    """Drive one entry through debounce, delay, error state and recovery; return the
    problems found (empty when the entry behaves as declared)."""
    d = Diagnoser(DiagnosisTable(entries=(entry,)))
    problems, t = [], t0
    for i in range(entry.threshold - 1):
        if d.report(entry.fault_id, ok=False, at=t).phase is not FaultPhase.DEBOUNCING:
            problems.append(f"confirmed after {i + 1} failures, threshold {entry.threshold}")
    s = d.report(entry.fault_id, ok=False, at=t)
    expected = FaultPhase.ACTIVE if entry.delay_s == 0 else FaultPhase.PENDING
    if s.phase is not expected:
        problems.append(f"after {entry.threshold} failures phase is {s.phase}, "
                        f"expected {expected}")
    if entry.delay_s > 0:
        d.poll(t + entry.delay_s * 0.5)
        if d.state(entry.fault_id).phase is not FaultPhase.PENDING:
            problems.append("entered the error state before delay_s")
        d.poll(t + entry.delay_s)
    if d.state(entry.fault_id).phase is not FaultPhase.ACTIVE:
        problems.append("did not enter the error state after delay_s")
    level = next((st.level for st in d.statuses(t + entry.delay_s)
                  if st.name == f"diag.{entry.fault_id}"), HealthLevel.OK)
    if level is not entry.severity.level:
        problems.append(f"active level {level}, expected {entry.severity.level}")
    for i in range(entry.threshold - 1):
        if d.report(entry.fault_id, ok=True, at=t + entry.delay_s).phase is FaultPhase.OK:
            problems.append(f"cleared after {i + 1} passes; needs {entry.threshold}")
    if d.report(entry.fault_id, ok=True, at=t + entry.delay_s).phase is not FaultPhase.OK:
        problems.append("did not clear after the counter returned to zero")
    return problems


def builtin_table() -> DiagnosisTable:
    """Faults every AIR deployment can detect; projects extend or replace the table."""
    E, S = DiagnosisEntry, DiagSeverity
    return DiagnosisTable(entries=(
        E("link_lost", "transport reports the robot link lost", 3, S.ERROR, 0.0,
          "D07.link_loss", "last_seen"),
        E("report_stale", "an expected health item stopped reporting", 1, S.ERROR, 2.0,
          "D11.stale_data", "last_seen"),
        E("rate_low", "a monitored loop runs below its minimum rate", 5, S.WARNING, 1.0,
          "D08.deadline_miss", "update_rate_actual"),
        E("latency_high", "observation latency above its limit", 5, S.WARNING, 1.0,
          "D11.latency_growth", "latency"),
        E("pack_overtemperature", "battery pack above its critical temperature", 2, S.FATAL,
          0.0, "D02.thermal_runaway", "pack_temperature"),
        E("cell_undervoltage", "a cell below its critical voltage", 3, S.ERROR, 0.5,
          "D02.over_discharge", "cell_voltage_min"),
        E("compute_throttling", "compute is thermally throttled", 3, S.WARNING, 5.0,
          "D04.compute_throttling", "throttling_active"),
        E("memory_pressure", "host memory pressure stalls tasks", 3, S.WARNING, 10.0,
          "D10.resource_exhaustion", "memory_usage"),
        E("fall_detected", "the robot reports it has fallen", 1, S.ERROR, 0.0,
          None, "fallen"),
        E("safety_channel_discrepancy", "safety channels disagree", 1, S.FATAL, 0.0,
          "D14.channel_discrepancy", "channel_discrepancy"),
        E("readiness_check_failed", "a readiness check failed", 1, S.INFO, 0.0, None, None),
    ))
