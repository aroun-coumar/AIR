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
from .contracts.execution import Command
from .contracts.manifest import ComponentManifest
from .contracts.observation import Observation
from .contracts.skill import SkillSpec
from .core.clock import Clock
from .core.errors import PluginError

__requirements__ = ("REQ01", "REQ03", "REQ18")

ENTRY_POINT_GROUP = "agentic_robotics.adapters"


@dataclass(frozen=True)
class AdapterReport:
    """What an adapter *reports* about an execution. The runtime still verifies it."""

    completed: bool
    message: str = ""
    effects: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None


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
