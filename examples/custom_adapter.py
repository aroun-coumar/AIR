"""Guide: writing an adapter for your own robot.

An adapter is the only code a product team must write to bring a new robot into the
framework. Implement the five abstract methods, register the class (or expose it via the
``agentic_robotics.adapters`` entry point in your package), and run the conformance
suite against it:

    python examples/custom_adapter.py
"""

from __future__ import annotations

import asyncio

from agentic_robotics import (
    AdapterReport,
    ExecutionContext,
    InterruptReport,
    Robot,
    RobotAdapter,
    SkillRegistry,
    UnsupportedOperation,
    register_adapter,
)
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import (
    AdapterRef,
    CapabilityProfile,
    CapabilityReport,
    ComponentManifest,
    Condition,
    Observation,
    Op,
    ProfileStatus,
    SkillSpec,
)

BEEP = SkillSpec(
    skill_id="beep", version="1.0.0", description="Sound the buzzer once.",
    required_capabilities=("audio",),
    postconditions=(Condition(signal="buzzer.count", op=Op.GE, value=1),),
)


class BuzzerAdapter(RobotAdapter):
    kind = "example_buzzer"

    def __init__(self, manifest, config):  # type: ignore[no-untyped-def]
        super().__init__(manifest, config)
        self.count = 0

    def supported_skills(self) -> frozenset[str]:
        return frozenset({"beep"})

    def capability_report(self) -> CapabilityReport:
        clock = self.require_clock()
        return CapabilityReport(
            robot_id=self.robot_id, capability_revision=1, observed_at=clock.now(),
            clock_domain=clock.domain,
            profiles=(CapabilityProfile("audio", ProfileStatus.AVAILABLE),))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        t = clock.now()
        return {"buzzer.count": Observation(
            source=self.robot_id, signal="buzzer.count", value=self.count,
            clock_domain=clock.domain, acquired_at=t, received_at=t)}

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        if ctx.command.skill_id != "beep":
            raise UnsupportedOperation("buzzer only beeps", skill_id=ctx.command.skill_id)
        self.count += 1  # talk to your real device here
        return AdapterReport(True, "beeped")

    async def interrupt(self, command_id: str) -> InterruptReport:
        return InterruptReport(False, False, "beeps are instantaneous")


register_adapter(BuzzerAdapter.kind, BuzzerAdapter)


async def main() -> None:
    manifest = ComponentManifest(robot_id="buzzer-1", name="Buzzer", hardware_revision="r1",
                                 adapter=AdapterRef(kind="example_buzzer"),
                                 nominal_capabilities=("audio",))
    report = await check_adapter(Robot.from_manifest(manifest), SkillRegistry([BEEP]))
    print(report.summary())


if __name__ == "__main__":
    asyncio.run(main())
