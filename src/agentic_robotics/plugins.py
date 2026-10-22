"""Adapter extension protocol and plugin discovery (baseline sections 5 and 19).

A robot adapter connects one kind of robot, simulator or fake device to the framework.
Adapters are found either by explicit :func:`register_adapter` calls or through the
``agentic_robotics.adapters`` entry-point group, so third-party packages can ship them
without modifying the framework.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, ClassVar

from .contracts.capability import CapabilityReport
from .contracts.execution import Command, ExitMode, FailureKind
from .contracts.manifest import ComponentManifest
from .contracts.observation import Observation
from .contracts.skill import SkillSpec
from .core.clock import Clock
from .core.errors import PluginError, UnsupportedOperation

__requirements__ = ("REQ01", "REQ03", "REQ14", "REQ18")

ENTRY_POINT_GROUP = "agentic_robotics.adapters"


#: Keys an adapter may not use in ``effects``: a report states facts, never a verdict.
#: The runtime strips them and records ``adapter_claim_ignored``.
RESERVED_CLAIMS = frozenset({"verified", "verdict", "state", "outcome", "success"})


@dataclass(frozen=True)
class AdapterReport:
    """What an adapter *reports* about an execution: facts, never a verdict. The runtime
    still verifies it against observations; nothing here can make a command VERIFIED.

    ``failure`` types a failure (taxonomy); ``retriable`` says that trying again is
    physically safe *if someone decides to* (the runtime never resends by itself);
    ``applied``/``limited_by`` report what the device actually applied when it clipped
    the command (LeRobot ``send_action``)."""

    completed: bool
    message: str = ""
    effects: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    failure: FailureKind | None = None
    retriable: bool = False
    applied: dict[str, Any] | None = None
    limited_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterruptReport:
    accepted: bool
    stopped_confirmed: bool
    message: str = ""


@dataclass
class ExecutionContext:
    """Passed to :meth:`RobotAdapter.execute`. Lets the implementation report progress
    and check, mid-execution, whether it still holds authority."""

    command: Command
    skill: SkillSpec
    clock: Clock
    progress: Callable[[str, Mapping[str, Any]], None]
    authority_valid: Callable[[], bool]
    #: The arguments as requested; ``command.args`` holds what was applied after the
    #: skill's motion limits (identical unless ``limited_by`` is non-empty).
    requested_args: Mapping[str, Any] = field(default_factory=dict)
    limited_by: tuple[str, ...] = ()


class RobotAdapter(abc.ABC):
    """Base class for robot, simulator and fake-device adapters."""

    kind: ClassVar[str] = ""

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        self.manifest = manifest
        self.config = dict(config)
        self.clock: Clock | None = None

    @property
    def robot_id(self) -> str:
        return self.manifest.robot_id

    def bind_clock(self, clock: Clock) -> None:
        self.clock = clock

    def require_clock(self) -> Clock:
        if self.clock is None:
            raise PluginError("adapter used before a clock was bound", robot_id=self.robot_id)
        return self.clock

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Connect to the device. Optional."""

    async def stop(self) -> None:  # noqa: B027 - optional hook
        """Disconnect from the device. Optional."""

    @abc.abstractmethod
    def supported_skills(self) -> frozenset[str]:
        """Skill ids this adapter can execute."""

    @abc.abstractmethod
    def capability_report(self) -> CapabilityReport:
        """Current effective capability, not the nominal manifest capability."""

    @abc.abstractmethod
    def observe(self) -> dict[str, Observation]:
        """Latest observation for each signal this adapter publishes."""

    @abc.abstractmethod
    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        """Execute an admitted command. Raise on lost contact; the runtime treats an
        exception after start as an *unresolved* outcome, never as a clean failure."""

    @abc.abstractmethod
    async def interrupt(self, command_id: str) -> InterruptReport:
        """Request that the active command stop. Report whether stopping is confirmed."""

    async def halt(self, command_id: str, exit_mode: ExitMode) -> InterruptReport:
        """Forced stop (deadman, ``Runtime.kill``): stop now and enter ``exit_mode``.
        Defaults to :meth:`interrupt`; override when the device has a real hold or damp
        mode. Called even for skills that cannot be cancelled cooperatively."""
        return await self.interrupt(command_id)

    async def pause(self, command_id: str) -> InterruptReport:
        """Instant action: pause a running command (skills with ``interruption: pause``)."""
        raise UnsupportedOperation("adapter does not support pause", kind=self.kind)

    async def resume(self, command_id: str) -> InterruptReport:
        """Instant action: resume a paused command."""
        raise UnsupportedOperation("adapter does not support resume", kind=self.kind)

    async def on_halted(self, command_id: str, reason: str) -> None:  # noqa: B027
        """Called exactly once when the runtime halted a started command (cancel, kill,
        deadline, deadman, revocation), after it stopped executing (BT.CPP
        ``onHalted``). Use it to release device-side state. Optional."""

    def hosted_models(self) -> list[dict[str, Any]]:
        """Optional: inference servers this robot hosts itself, so agents can use the
        robot's own intelligence (and keep data on the robot). Each entry:
        ``{"name", "model", "endpoint", "provider"="openai_compatible",
        "supports_tools"=True}``."""
        return []

    def estimate_cost(self, skill_id: str, args: Mapping[str, Any]) -> float | None:
        """Optional: relative cost of running ``skill_id`` now (used when bidding for team
        tasks, e.g. travel distance or expected duration). ``None`` means no estimate."""
        return None


AdapterFactory = Callable[[ComponentManifest, Mapping[str, Any]], RobotAdapter]

_ADAPTERS: dict[str, type[RobotAdapter]] = {}


def register_adapter(kind: str, cls: type[RobotAdapter]) -> None:
    existing = _ADAPTERS.get(kind)
    if existing is not None and existing is not cls:
        raise PluginError("adapter kind already registered", kind=kind)
    _ADAPTERS[kind] = cls


def adapter_class(kind: str) -> type[RobotAdapter]:
    if kind in _ADAPTERS:
        return _ADAPTERS[kind]
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == kind:
            cls = ep.load()
            if not (isinstance(cls, type) and issubclass(cls, RobotAdapter)):
                raise PluginError("entry point is not a RobotAdapter", kind=kind,
                                  target=ep.value)
            _ADAPTERS[kind] = cls
            return cls
    raise PluginError("no adapter registered for kind", kind=kind,
                      known=sorted(available_adapters()))


def available_adapters() -> set[str]:
    return set(_ADAPTERS) | {ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)}


def create_adapter(manifest: ComponentManifest) -> RobotAdapter:
    cls = adapter_class(manifest.adapter.kind)
    return cls(manifest, manifest.adapter.config)


__all__ = [
    "ENTRY_POINT_GROUP",
    "RESERVED_CLAIMS",
    "AdapterFactory",
    "AdapterReport",
    "ExecutionContext",
    "InterruptReport",
    "RobotAdapter",
    "adapter_class",
    "available_adapters",
    "create_adapter",
    "register_adapter",
]
