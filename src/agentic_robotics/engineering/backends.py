"""Experiment backends: simulators, emulators and reference models behind one protocol.

A backend *declares* what it can do (W3.1). Every capability flag defaults to false and
determinism defaults to ``unknown``: nothing is assumed because a backend has a seed.
Fidelity is declared per aspect, never as one "sim" label. Requesting a feature the
backend does not support raises :class:`UnsupportedFeature`; it never silently falls
back. :class:`BackendInfo` records what was *actually* used (a GPU request may run on a
CPU; a variable-step solver resolves its own step size).

    backend = PointMassBackend()
    info = backend.load({"target": 1.0}, TimePolicy(physics_dt=0.01, control_dt=0.05))
    obs = backend.reset(seed=7)
    obs = backend.step({"kp": 4.0, "kd": 1.5})
"""

from __future__ import annotations

import math
import platform as _platform
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..core.errors import FrameworkError, SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ04", "G03", "G04")

#: Version of AIR's experiment-runner semantics. Results are only compared within one
#: runner version (Microduck arena ``RUNNER_VERSION``).
RUNNER_VERSION = "1"


class UnsupportedFeature(FrameworkError):
    """A requested feature or time policy is not supported by this backend (MJX raises
    on unsupported features; AIR never falls back silently)."""

    code = "unsupported_feature"


class Fidelity(StrEnum):
    """Declared fidelity of one aspect (dynamics, contact, sensing, actuation, timing...)."""

    UNKNOWN = "unknown"
    NONE = "none"
    KINEMATIC = "kinematic"
    SIMPLIFIED = "simplified"
    PHYSICAL = "physical"
    HARDWARE_IN_LOOP = "hardware_in_loop"


class DeterminismClass(StrEnum):
    """What a backend *claims*; the probe measures it (``engineering.probe``)."""

    UNKNOWN = "unknown"
    STATISTICAL = "statistical"
    BITWISE_SAME_HOST = "bitwise_same_host"
    BITWISE = "bitwise"


class RestoreLevel(StrEnum):
    """MuJoCo state taxonomy: PHYSICS ⊂ FULL_PHYSICS ⊂ INTEGRATION."""

    NONE = "none"
    PHYSICS = "physics"
    FULL_PHYSICS = "full_physics"
    INTEGRATION = "integration"


@dataclass(frozen=True)
class BackendCapabilities:
    """Declared capabilities. Every flag defaults to false (RoboVerse ``BaseSimHandler``)."""

    fidelity: dict[str, Fidelity] = field(default_factory=dict)
    determinism: DeterminismClass = DeterminismClass.UNKNOWN
    restore_level: RestoreLevel = RestoreLevel.NONE
    #: State that a restore does *not* bring back (ManiSkill drops controller state).
    restore_lossy: tuple[str, ...] = ()
    #: Features that can be requested at load; anything else raises.
    features: tuple[str, ...] = ()
    #: Features known to be unsupported (documented; requesting them raises).
    unsupported: tuple[str, ...] = ()
    #: Random generators the backend seeds from ``reset(seed)``.
    seeded_rngs: tuple[str, ...] = ()
    supports_snapshot: bool = False
    supports_lockstep: bool = False
    supports_variable_step: bool = False
    supports_rng_state: bool = False
    vectorized: bool = False

    def fidelity_of(self, aspect: str) -> Fidelity:
        return self.fidelity.get(aspect, Fidelity.UNKNOWN)


@dataclass(frozen=True)
class TimePolicy:
    """Simulation timing (ManiSkill ``sim_freq % control_freq == 0``; Isaac Lab
    ``decimation``). ``control_dt`` must be an integer multiple of ``physics_dt``; this is
    enforced when the policy is built or parsed. A free-running (``lockstep=False``) or
    variable-step run is valid but marked non-reproducible."""

    physics_dt: float
    control_dt: float
    episode_length_s: float | None = None
    lockstep: bool = True
    variable_step: bool = False
    clock_domain: str = "sim"

    def __post_init__(self) -> None:
        if not (self.physics_dt > 0 and self.control_dt > 0):
            raise SchemaError("time steps must be positive", physics_dt=self.physics_dt,
                              control_dt=self.control_dt)
        ratio = self.control_dt / self.physics_dt
        if abs(ratio - round(ratio)) > 1e-9 * max(1.0, ratio) or round(ratio) < 1:
            raise SchemaError("control_dt must be an integer multiple of physics_dt",
                              physics_dt=self.physics_dt, control_dt=self.control_dt)
        if self.episode_length_s is not None and self.episode_length_s <= 0:
            raise SchemaError("episode_length_s must be positive")

    @property
    def decimation(self) -> int:
        return round(self.control_dt / self.physics_dt)

    @property
    def episode_steps(self) -> int | None:
        if self.episode_length_s is None:
            return None
        return math.floor(self.episode_length_s / self.control_dt + 1e-9)

    @property
    def reproducible(self) -> bool:
        return self.lockstep and not self.variable_step

    def non_reproducible_reasons(self) -> tuple[str, ...]:
        out = []
        if not self.lockstep:
            out.append("free-running (not lockstep)")
        if self.variable_step:
            out.append("variable-step integration")
        return tuple(out)


@contract("backend_info", version="1.0")
class BackendInfo(Contract):
    """The backend actually used for an experiment, as resolved at load."""

    backend: str
    version: str = ""
    build_ids: dict[str, str] = field(default_factory=dict)
    platform: str = ""
    requested_device: str = "cpu"
    actual_device: str = "cpu"
    capabilities: BackendCapabilities = field(default_factory=BackendCapabilities)
    time_policy: TimePolicy | None = None
    resolved_physics_dt: float | None = None
    resolved_output_interval: float | None = None
    features: tuple[str, ...] = ()
    out_of_process: bool = False
    non_reproducible: tuple[str, ...] = ()

    @property
    def reproducible(self) -> bool:
        return not self.non_reproducible

    @property
    def fell_back(self) -> bool:
        return self.requested_device != self.actual_device


@dataclass(frozen=True)
class Snapshot:
    """A restorable backend state with the level it reaches and what it loses."""

    state: dict[str, Any]
    level: RestoreLevel
    lossy: tuple[str, ...] = ()


@runtime_checkable
class ExperimentBackend(Protocol):
    """What the experiment runner, probe, golden runs and search need from a backend."""

    name: str
    version: str
    capabilities: BackendCapabilities

    def load(self, scenario: Mapping[str, Any], time_policy: TimePolicy, *,
             features: Iterable[str] = (), device: str = "cpu") -> BackendInfo: ...

    def reset(self, seed: int | None) -> dict[str, Any]: ...

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]: ...

    def snapshot(self) -> Snapshot: ...

    def restore(self, snapshot: Snapshot) -> None: ...

    def rng_state(self) -> Any: ...

    def set_rng_state(self, state: Any) -> None: ...

    def metrics(self) -> dict[str, float]: ...

    def close(self) -> None: ...


def platform_key() -> str:
    """Results and golden runs are compared only within one platform key (Microduck found
    8.4% arm64-vs-x86 divergence)."""
    return (f"{_platform.system().lower()}-{_platform.machine().lower()}-"
            f"{_platform.python_implementation().lower()}{_platform.python_version()}")


def check_load(capabilities: BackendCapabilities, time_policy: TimePolicy,
               features: Iterable[str], backend: str) -> tuple[str, ...]:
    """Shared load-time checks. Raises on unsupported features or timing; returns the
    reasons this run is non-reproducible."""
    requested = tuple(features)
    bad = [f for f in requested if f not in capabilities.features]
    if bad:
        raise UnsupportedFeature("backend does not support requested features",
                                 backend=backend, features=bad,
                                 supported=list(capabilities.features))
    if time_policy.variable_step and not capabilities.supports_variable_step:
        raise UnsupportedFeature("backend has no variable-step integrator", backend=backend)
    if time_policy.lockstep and not capabilities.supports_lockstep:
        raise UnsupportedFeature("backend cannot run in lockstep", backend=backend)
    reasons = list(time_policy.non_reproducible_reasons())
    if capabilities.determinism == DeterminismClass.UNKNOWN:
        reasons.append("backend declares no determinism class")
    if not capabilities.seeded_rngs:
        reasons.append("backend seeds no random generator")
    return tuple(reasons)


BackendFactory = Callable[[], ExperimentBackend]
_BACKENDS: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory) -> None:
    if name in _BACKENDS and _BACKENDS[name] is not factory:
        raise SchemaError("backend name registered twice", name=name)
    _BACKENDS[name] = factory


def available_backends() -> dict[str, BackendFactory]:
    from .pointmass import PointMassBackend
    from .process import process_factory

    _BACKENDS.setdefault("pointmass", PointMassBackend)
    _BACKENDS.setdefault("process:pointmass", process_factory("pointmass"))
    return dict(_BACKENDS)


def make_backend(name: str) -> ExperimentBackend:
    factories = available_backends()
    if name not in factories:
        raise SchemaError("unknown experiment backend", name=name,
                          available=sorted(factories))
    return factories[name]()
