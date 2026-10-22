"""Adapter conformance suite (baseline sections 4 and 7, "Plugin conformance").

Downstream teams run this against their own adapters to check that they honor the
framework contract before declaring a support profile::

    report = await check_adapter(robot, skills)
    assert report.passed, report.summary()
    report.to_record().save("conformance.json")   # ar.conformance_report/1.0

The record names the suite, AIR and contract versions it was produced with, and its
``passed`` flag is checked against the outcomes, so a badge can only be issued from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .contracts.capability import CapabilityReport
from .contracts.execution import Command
from .contracts.manifest import ComponentManifest
from .contracts.observation import Observation
from .contracts.release import ConformanceCheck, ConformanceReportRecord
from .contracts.skill import SkillSet
from .core.clock import Clock, MonotonicClock
from .core.errors import FrameworkError, UnsupportedOperation
from .plugins import ExecutionContext
from .robot import Robot
from .skills import SkillRegistry

__requirements__ = ("REQ03", "REQ18", "F06")

#: Bump when checks are added, removed or change meaning.
SUITE_VERSION = "1.0"
#: Contracts whose behaviour the suite exercises.
_CHECKED_CONTRACTS = (CapabilityReport, Observation, Command, ComponentManifest, SkillSet)


@dataclass
class CheckOutcome:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ConformanceReport:
    robot_id: str
    adapter_kind: str
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    def summary(self) -> str:
        lines = [f"conformance: {self.adapter_kind} ({self.robot_id})"]
        for o in self.outcomes:
            lines.append(f"  [{'PASS' if o.passed else 'FAIL'}] {o.name}"
                         + (f": {o.detail}" if o.detail else ""))
        lines.append("result: " + ("PASS" if self.passed else "FAIL"))
        return "\n".join(lines)

    def to_record(self, *, manifest_digest: str | None = None,
                  skills_digest: str | None = None,
                  generated_at: float | None = None) -> ConformanceReportRecord:
        """The versioned ``ar.conformance_report/1.0`` document for this run."""
        from . import __version__

        return ConformanceReportRecord(
            robot_id=self.robot_id, adapter_kind=self.adapter_kind,
            suite_version=SUITE_VERSION, air_version=__version__, passed=self.passed,
            outcomes=tuple(ConformanceCheck(o.name, o.passed, o.detail)
                           for o in self.outcomes),
            contract_versions={c.SCHEMA: c.VERSION for c in _CHECKED_CONTRACTS},
            manifest_digest=manifest_digest, skills_digest=skills_digest,
            generated_at=time.time() if generated_at is None else generated_at)


async def check_adapter(robot: Robot, skills: SkillRegistry,
                        clock: Clock | None = None) -> ConformanceReport:
    clock = clock or robot.adapter.clock or MonotonicClock()
    adapter = robot.adapter
    if adapter.clock is None:
        adapter.bind_clock(clock)
    report = ConformanceReport(robot.robot_id, adapter.kind)

    def record(name: str, ok: bool, detail: str = "") -> None:
        report.outcomes.append(CheckOutcome(name, ok, detail))

    record("declares adapter kind", bool(adapter.kind))

    supported = adapter.supported_skills()
    unknown = sorted(s for s in supported if s not in skills)
    record("supported skills are declared in the skill registry", not unknown,
           f"undeclared: {unknown}" if unknown else "")

    try:
        cap = adapter.capability_report()
        ok = isinstance(cap, CapabilityReport) and cap.robot_id == robot.robot_id
        record("capability report identifies this robot", ok)
        record("capability report uses the bound clock domain", cap.clock_domain == clock.domain,
               f"{cap.clock_domain!r} vs {clock.domain!r}")
        record("capability report is fresh", cap.age(clock.now()) <= 1.0,
               f"age {cap.age(clock.now()):.3f}s")
        nominal = set(robot.manifest.nominal_capabilities)
        reported = {p.name for p in cap.profiles}
        record("every nominal capability is reported", nominal <= reported,
               f"missing: {sorted(nominal - reported)}" if nominal - reported else "")
        CapabilityReport.from_dict(cap.to_dict())
        record("capability report round-trips through JSON", True)
    except FrameworkError as exc:
        record("capability report is valid", False, str(exc))

    try:
        obs = adapter.observe()
        bad = [k for k, o in obs.items() if not isinstance(o, Observation) or o.signal != k]
        record("observations are keyed by signal", not bad, f"bad: {bad}" if bad else "")
        domains = {o.clock_domain for o in obs.values()}
        record("observations carry the bound clock domain", domains <= {clock.domain},
               str(sorted(domains)))
        for o in obs.values():
            Observation.from_dict(o.to_dict())
        record("observations round-trip through JSON", True)
    except FrameworkError as exc:
        record("observations are valid", False, str(exc))

    probe = Command(command_id="conformance_probe", robot_id=robot.robot_id,
                    skill_id="__conformance_unsupported__", issued_by="conformance",
                    delegation_id="none", authority_revision=0)
    ctx = ExecutionContext(command=probe, skill=next(iter(skills)), clock=clock,
                           progress=lambda *_: None, authority_valid=lambda: True)
    try:
        await adapter.execute(ctx)
        record("unsupported skill fails explicitly", False, "execute() returned normally")
    except UnsupportedOperation:
        record("unsupported skill fails explicitly", True)
    except Exception as exc:
        record("unsupported skill fails explicitly", False, f"raised {exc!r}")

    ir = await adapter.interrupt("conformance_not_active")
    record("interrupting an inactive command is refused, not faked",
           not ir.accepted and not ir.stopped_confirmed)
    return report
