"""LeRobot inference contracts, mirrored in the standard library (mapping layer only).

Verified from ``huggingface/lerobot`` (HEAD 8a12558, Apache-2.0; read, not copied):
``src/lerobot/policies/chunk.py`` (``FeatureSpec``, ``ChunkPolicySpec``, ``ExecutionMode``)
and ``src/lerobot/inference/contracts.py`` (``PolicyCapabilities`` "advertised before
session admission", ``ObservationSnapshot``, ``ActionProvenance``, ``ActionChunk``), plus
``src/lerobot/remote_inference/README.md`` (compatibility is checked before motion: feature
names, order, shapes, modality, semantics, action interval and mode; "neither transport
delivery nor a successful model call alone authorizes motion").

**Not modelled:** LeRobot's remote wire protocol (Zenoh envelopes in
``remote_inference/protocol.py``). The transport is injected; AIR only checks contracts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...core.errors import SchemaError

__requirements__ = ("REQ01", "REQ11", "REQ12")

DTYPES = frozenset({"bool", "uint8", "int32", "int64", "float32", "float64"})


class ExecutionMode(StrEnum):
    CHUNK = "chunk"
    RTC_GUIDED = "rtc_guided"
    RTC_TRAINED = "rtc_trained"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    shape: tuple[int, ...]
    dtype: str
    kind: str = "tensor"
    names: tuple[str, ...] = ()
    semantics: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "shape", tuple(self.shape))
        object.__setattr__(self, "names", tuple(self.names))
        if not self.name.strip() or not self.semantics.strip():
            raise SchemaError("feature names and explicit semantic conventions are required",
                              feature=self.name)
        if self.kind not in ("tensor", "rgb"):
            raise SchemaError("unsupported feature modality", kind=self.kind)
        if len(self.shape) > 4 or any(type(d) is not int or d <= 0 for d in self.shape):
            raise SchemaError("invalid feature shape", shape=self.shape)
        if self.dtype not in DTYPES:
            raise SchemaError("unsupported feature dtype", dtype=self.dtype)
        if self.kind == "rgb" and (len(self.shape) != 3 or self.shape[-1] != 3
                                   or self.dtype != "uint8"):
            raise SchemaError("RGB features require HWC uint8 with three channels")
        if self.names and (len(self.shape) != 1 or len(self.names) != self.shape[0]):
            raise SchemaError("ordered component names must match a 1-D feature")
        if len(set(self.names)) != len(self.names):
            raise SchemaError("feature component names must be unique")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FeatureSpec:
        return cls(name=str(d["name"]), shape=tuple(int(x) for x in d["shape"]),
                   dtype=str(d["dtype"]), kind=str(d.get("kind", "tensor")),
                   names=tuple(str(x) for x in d.get("names", ())),
                   semantics=str(d.get("semantics", "")))


@dataclass(frozen=True)
class PolicyCapabilities:
    """The serving contract a policy advertises before session admission."""

    modes: tuple[ExecutionMode, ...]
    prediction_steps: int
    execution_steps: int
    action_interval: float
    features: tuple[FeatureSpec, ...]
    action_feature: FeatureSpec
    current_observation_only: bool = True
    artifact_identity: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.action_interval) or self.action_interval <= 0:
            raise SchemaError("policy action interval must be finite and positive")
        if not self.modes or not 0 < self.execution_steps <= self.prediction_steps:
            raise SchemaError("a valid mode and execution length are required")
        if len({f.name for f in self.features}) != len(self.features):
            raise SchemaError("duplicate observation feature names")

    @property
    def horizon_s(self) -> float:
        """``N`` steps at the action interval represent ``N * interval`` seconds."""
        return self.execution_steps * self.action_interval

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PolicyCapabilities:
        return cls(modes=tuple(ExecutionMode(m) for m in d["modes"]),
                   prediction_steps=int(d["prediction_steps"]),
                   execution_steps=int(d["execution_steps"]),
                   action_interval=float(d["action_interval"]),
                   features=tuple(FeatureSpec.from_dict(f) for f in d["features"]),
                   action_feature=FeatureSpec.from_dict(d["action_feature"]),
                   current_observation_only=bool(d.get("current_observation_only", True)),
                   artifact_identity=str(d.get("artifact_identity", "")))


@dataclass(frozen=True)
class RobotInterface:
    """What the robot side offers: its observation features and its action feature."""

    features: tuple[FeatureSpec, ...]
    action_feature: FeatureSpec
    control_interval: float
    mode: ExecutionMode = ExecutionMode.CHUNK
    expected_artifact: str = ""


def compatibility_problems(caps: PolicyCapabilities, robot: RobotInterface) -> list[str]:
    """Every reason this policy must not drive this robot. Empty means compatible.
    Checked *before motion*; a compatible policy still goes through AIR admission."""
    problems: list[str] = []
    if robot.mode not in caps.modes:
        problems.append(f"mode {robot.mode.value} not offered (offers "
                        f"{[m.value for m in caps.modes]})")
    want = [f.name for f in caps.features]
    have = [f.name for f in robot.features]
    if want != have:
        problems.append(f"observation features/order differ: policy {want}, robot {have}")
    for p, r in zip(caps.features, robot.features, strict=False):
        if p.name == r.name:
            problems.extend(_feature_diff(p, r))
    problems.extend(_feature_diff(caps.action_feature, robot.action_feature))
    if not math.isclose(caps.action_interval, robot.control_interval, rel_tol=1e-6):
        problems.append(f"action interval {caps.action_interval}s != robot control interval "
                        f"{robot.control_interval}s")
    if robot.expected_artifact and caps.artifact_identity != robot.expected_artifact:
        problems.append("policy artifact identity does not match the expected artifact")
    return problems


def _feature_diff(p: FeatureSpec, r: FeatureSpec) -> list[str]:
    out = []
    for attr in ("shape", "dtype", "kind", "names", "semantics"):
        if getattr(p, attr) != getattr(r, attr):
            out.append(f"{p.name}.{attr}: policy {getattr(p, attr)!r} != robot "
                       f"{getattr(r, attr)!r}")
    return out


@dataclass(frozen=True)
class ActionProvenance:
    capture_time: float
    task: str
    observation_id: str = ""
    request_id: str = ""
    generation: int = 0
    session_id: str = ""
    server_instance_id: str = ""
    artifact_identity: str = ""


@dataclass(frozen=True)
class ActionChunk:
    actions: tuple[tuple[float, ...], ...]
    provenance: ActionProvenance
    execution_steps: int
    received_at: float
    server_durations: dict[str, float] = field(default_factory=dict)

    def validate(self, caps: PolicyCapabilities) -> None:
        if len(self.actions) < self.execution_steps:
            raise SchemaError("chunk is shorter than its execution steps")
        dim = caps.action_feature.shape[-1]
        for row in self.actions:
            if len(row) != dim or not all(math.isfinite(v) for v in row):
                raise SchemaError("action row has the wrong width or a non-finite value")


__all__ = ["ActionChunk", "ActionProvenance", "ExecutionMode", "FeatureSpec",
           "PolicyCapabilities", "RobotInterface", "compatibility_problems"]
