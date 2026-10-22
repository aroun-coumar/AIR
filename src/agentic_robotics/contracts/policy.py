"""Learned-policy contracts: the policy manifest, action chunks and the cross-embodiment
action space (ADR 0016).

A policy is a content-addressed entity: the model file is opaque bytes, identified by
its SHA-256, and described by a JSON sidecar (``ar.policy_manifest/1.0``). The sidecar
advertises everything a robot needs to decide *before admission* whether the policy can
run on it: model API, observation and action features and dimensions, robot model and
joint order, gains, observation scale/clip/history, action scale, prediction vs
execution steps, action interval, inference backend, origin, exit mode and a mandatory
staleness bound for action chunks.

Patterns (not code) from: LeRobot ``inference/contracts.py`` (``PolicyCapabilities``,
``ActionProvenance``), the Microduck policy manifest (load gates, origin classes),
mjlab's exporter (metadata embedded with the model), Isaac-GR00T ``BasePolicy`` (check
the observation before inference and the action after), booster_deploy (declared exit
mode, two-step start) and RDT (one action vector with named slots).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.errors import SchemaError
from ..core.ids import is_valid_id
from ..core.schema import Contract, _dump, contract
from ..core.units import known_units

__requirements__ = ("REQ05", "REQ11", "REQ12", "REQ16", "F03", "F04", "G03", "G05", "G15")


def _check_unit(unit: str, owner: str) -> None:
    if unit not in known_units():
        raise SchemaError("unit is not in AIR's unit table", unit=unit, owner=owner)


class PolicyOrigin(StrEnum):
    """Where a policy came from. Only ``official`` loads without explicit authority."""

    OFFICIAL = "official"      # signed, versioned release from the robot's maintainer
    COMMUNITY = "community"    # repo, revision and digest recorded; never auto-applied
    LOCAL = "local"            # built here; no upstream provenance


class ExitMode(StrEnum):
    """What the robot does when the policy stops (declared, never guessed)."""

    HOLD = "hold"              # hold the last commanded posture
    DAMP = "damp"              # zero torque with damping


@dataclass(frozen=True)
class FeatureSpec:
    """One named observation or action feature. Order of ``names`` is significant."""

    name: str
    shape: tuple[int, ...]
    dtype: str = "float32"
    unit: str | None = None
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or any(d <= 0 for d in self.shape):
            raise SchemaError("feature needs a name and a positive shape", feature=self.name)
        if self.unit is not None:
            _check_unit(self.unit, self.name)
        if self.names and len(self.names) != self.size:
            raise SchemaError("feature names do not match its size", feature=self.name,
                              names=len(self.names), size=self.size)

    @property
    def size(self) -> int:
        return math.prod(self.shape)


@dataclass(frozen=True)
class ObservationProcessing:
    """How raw observations are normalized before inference (part of identity)."""

    scale: tuple[float, ...] = ()        # per element, or empty for 1.0
    offset: tuple[float, ...] = ()       # subtracted before scaling
    clip: float | None = None            # symmetric clip after scaling
    history: int = 1                     # frames stacked into one observation

    def __post_init__(self) -> None:
        if self.history < 1:
            raise SchemaError("observation history must be at least 1")
        if self.clip is not None and not self.clip > 0:
            raise SchemaError("observation clip must be positive")


@contract("policy_manifest", version="1.0")
class PolicyManifest(Contract):
    """The JSON sidecar of a learned policy: identity, capabilities and load gates."""

    policy_id: str
    version: str
    model_digest: str                    # sha256:<hex> of the opaque model bytes
    model_api: str                       # the robot-side runner API this model targets
    model_format: str                    # e.g. "onnx", "torchscript"; opaque to AIR
    inference_backend: str               # e.g. "onnxruntime-cpu"; part of identity
    robot_model: str
    observation_features: tuple[FeatureSpec, ...]
    action_feature: FeatureSpec
    prediction_steps: int                # actions produced per inference
    execution_steps: int                 # actions executed before the next inference
    action_interval_s: float             # time between consecutive actions
    max_action_age_s: float              # staleness bound: older chunks are refused
    exit_mode: ExitMode
    origin: PolicyOrigin
    joint_order: tuple[str, ...] = ()
    kp: tuple[float, ...] = ()
    kd: tuple[float, ...] = ()
    observation: ObservationProcessing = field(default_factory=ObservationProcessing)
    action_scale: tuple[float, ...] = ()
    action_low: tuple[float, ...] = ()
    action_high: tuple[float, ...] = ()
    default_joint_positions: tuple[float, ...] = ()
    source: str = ""                     # e.g. "hf://org/repo@<commit>" for community
    training_run: str | None = None      # entity id of the training run
    derived_from: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not is_valid_id(self.policy_id):
            raise SchemaError("invalid policy id", policy_id=self.policy_id)
        if not self.model_digest.startswith("sha256:") or len(self.model_digest) != 71:
            raise SchemaError("model_digest must be sha256:<64 hex>")
        if not 0 < self.execution_steps <= self.prediction_steps:
            raise SchemaError("need 0 < execution_steps <= prediction_steps",
                              execution_steps=self.execution_steps,
                              prediction_steps=self.prediction_steps)
        for name in ("action_interval_s", "max_action_age_s"):
            v = getattr(self, name)
            if not math.isfinite(v) or v <= 0:
                raise SchemaError(f"{name} must be finite and positive", value=v)
        names = [f.name for f in self.observation_features]
        if not names or len(set(names)) != len(names):
            raise SchemaError("observation features must be non-empty and unique")
        a = self.action_dim
        for attr in ("action_scale", "action_low", "action_high"):
            v = getattr(self, attr)
            if v and len(v) != a:
                raise SchemaError(f"{attr} length does not match action_dim", attr=attr,
                                  length=len(v), action_dim=a)
        j = len(self.joint_order)
        if len(set(self.joint_order)) != j:
            raise SchemaError("joint_order has duplicates")
        for attr in ("kp", "kd", "default_joint_positions"):
            v = getattr(self, attr)
            if v and len(v) != j:
                raise SchemaError(f"{attr} length does not match joint_order", attr=attr)
        if self.action_low and self.action_high and any(
                lo > hi for lo, hi in zip(self.action_low, self.action_high, strict=True)):
            raise SchemaError("action_low exceeds action_high")
        scale = self.observation.scale
        if scale and len(scale) != self.obs_dim:
            raise SchemaError("observation scale length does not match obs_dim")

    @property
    def obs_dim(self) -> int:
        return sum(f.size for f in self.observation_features)

    @property
    def action_dim(self) -> int:
        return self.action_feature.size

    def identity(self) -> str:
        """Content identity: model digest plus every field that changes behaviour.

        Two manifests with the same model but a different backend, gain, scale or joint
        order are different policies. ``meta`` and free text are excluded."""
        body = _dump(self)
        assert isinstance(body, dict)
        for k in ("meta", "description", "derived_from", "training_run"):
            body.pop(k, None)
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "policy:sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyRequirements:
    """What a robot slot requires of any policy it will load (the load gate).

    Empty fields are not checked. Every mismatch is reported; none is coerced."""

    model_api: str
    robot_model: str
    obs_dim: int | None = None
    action_dim: int | None = None
    joint_order: tuple[str, ...] = ()
    kp: tuple[float, ...] = ()
    kd: tuple[float, ...] = ()
    observation: ObservationProcessing | None = None
    action_interval_s: float | None = None
    inference_backends: tuple[str, ...] = ()
    max_action_age_s: float | None = None   # the slot's ceiling on the staleness bound

    def mismatches(self, m: PolicyManifest, tol: float = 1e-9) -> list[str]:
        out: list[str] = []

        def same(a: Sequence[float], b: Sequence[float]) -> bool:
            return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b,
                                                                             strict=True))

        if m.model_api != self.model_api:
            out.append(f"model_api {m.model_api!r} != {self.model_api!r}")
        if m.robot_model != self.robot_model:
            out.append(f"robot_model {m.robot_model!r} != {self.robot_model!r}")
        if self.obs_dim is not None and m.obs_dim != self.obs_dim:
            out.append(f"obs_dim {m.obs_dim} != {self.obs_dim}")
        if self.action_dim is not None and m.action_dim != self.action_dim:
            out.append(f"action_dim {m.action_dim} != {self.action_dim}")
        if self.joint_order and m.joint_order != self.joint_order:
            out.append("joint_order differs")
        if self.kp and not same(m.kp, self.kp):
            out.append("kp gains differ")
        if self.kd and not same(m.kd, self.kd):
            out.append("kd gains differ")
        if self.observation is not None:
            o, r = m.observation, self.observation
            if not same(o.scale, r.scale) or not same(o.offset, r.offset):
                out.append("observation scale/offset differ")
            if o.clip != r.clip:
                out.append(f"observation clip {o.clip} != {r.clip}")
            if o.history != r.history:
                out.append(f"observation history {o.history} != {r.history}")
        if (self.action_interval_s is not None
                and abs(m.action_interval_s - self.action_interval_s) > tol):
            out.append(f"action_interval_s {m.action_interval_s} != {self.action_interval_s}")
        if self.inference_backends and m.inference_backend not in self.inference_backends:
            out.append(f"inference_backend {m.inference_backend!r} not in "
                       f"{list(self.inference_backends)}")
        if self.max_action_age_s is not None and m.max_action_age_s > self.max_action_age_s:
            out.append(f"max_action_age_s {m.max_action_age_s} exceeds the slot ceiling "
                       f"{self.max_action_age_s}")
        return out


@dataclass(frozen=True)
class ActionProvenance:
    """Where an action chunk came from. Chunks without it are refused."""

    request_id: str
    generation: int                      # load generation of the slot that produced it
    session_id: str
    artifact: str                        # PolicyManifest.identity() of the producer
    observation_id: str = ""
    capture_time: float | None = None    # when the observation it answers was captured


@contract("action_chunk", version="1.0")
class ActionChunk(Contract):
    """A block of actions produced by one inference, with provenance and a staleness bound."""

    actions: tuple[tuple[float, ...], ...]
    provenance: ActionProvenance
    created_at: float
    max_age_s: float
    execution_steps: int

    def __post_init__(self) -> None:
        if not self.actions:
            raise SchemaError("action chunk is empty")
        if not 0 < self.execution_steps <= len(self.actions):
            raise SchemaError("execution_steps out of range")
        if not math.isfinite(self.max_age_s) or self.max_age_s <= 0:
            raise SchemaError("an action chunk needs a positive staleness bound")

    def age(self, now: float) -> float:
        """Age from the oldest moment the chunk depends on (its observation's capture
        time when known, otherwise its creation)."""
        start = self.created_at
        if self.provenance.capture_time is not None:
            start = min(start, self.provenance.capture_time)
        return now - start

    def stale(self, now: float) -> bool:
        return self.age(now) > self.max_age_s


# -- cross-embodiment action space (W5.6) ------------------------------------------------


@dataclass(frozen=True)
class ActionSlot:
    """One named, unit-bearing segment of a unified action vector."""

    name: str
    size: int = 1
    unit: str = "1"
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or self.size < 1:
            raise SchemaError("action slot needs a name and a positive size", slot=self.name)
        _check_unit(self.unit, self.name)


@contract("action_space", version="1.0")
class ActionSpace(Contract):
    """An ordered registry of named slots. Every embodiment maps into the same vector;
    slots it does not have are masked out instead of being zero-filled silently."""

    space_id: str
    slots: tuple[ActionSlot, ...]
    description: str = ""

    def __post_init__(self) -> None:
        names = [s.name for s in self.slots]
        if not names or len(set(names)) != len(names):
            raise SchemaError("action space slots must be non-empty and unique")

    @property
    def dim(self) -> int:
        return sum(s.size for s in self.slots)

    def offsets(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        i = 0
        for s in self.slots:
            out[s.name] = (i, i + s.size)
            i += s.size
        return out

    def slot(self, name: str) -> ActionSlot:
        for s in self.slots:
            if s.name == name:
                return s
        raise SchemaError("unknown action slot", slot=name, space=self.space_id)

    def pack(self, values: Mapping[str, float | Sequence[float]],
             units: Mapping[str, str] | None = None) -> UnifiedAction:
        """Place named values into the vector. Units, when given, must match the slot's."""
        vec = [0.0] * self.dim
        mask = [False] * self.dim
        offs = self.offsets()
        for name, v in values.items():
            s = self.slot(name)
            if units and name in units and units[name] != s.unit:
                raise SchemaError("unit does not match the slot", slot=name,
                                  expected=s.unit, actual=units[name])
            seq = [float(v)] if isinstance(v, (int, float)) else [float(x) for x in v]
            if len(seq) != s.size:
                raise SchemaError("value size does not match the slot", slot=name,
                                  expected=s.size, actual=len(seq))
            if not all(math.isfinite(x) for x in seq):
                raise SchemaError("action value is not finite", slot=name)
            lo, hi = offs[name]
            vec[lo:hi] = seq
            mask[lo:hi] = [True] * s.size
        return UnifiedAction(space_id=self.space_id, values=tuple(vec), mask=tuple(mask))

    def unpack(self, action: UnifiedAction) -> dict[str, tuple[float, ...]]:
        """Named values for the slots that are present (fully unmasked) in ``action``."""
        if action.space_id != self.space_id or len(action.values) != self.dim:
            raise SchemaError("action does not belong to this space", space=self.space_id,
                              actual=action.space_id)
        out: dict[str, tuple[float, ...]] = {}
        for name, (lo, hi) in self.offsets().items():
            m = action.mask[lo:hi]
            if all(m):
                out[name] = action.values[lo:hi]
            elif any(m):
                raise SchemaError("slot is partially masked", slot=name)
        return out


@contract("unified_action", version="1.0")
class UnifiedAction(Contract):
    space_id: str
    values: tuple[float, ...]
    mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.mask):
            raise SchemaError("values and mask lengths differ")


def default_action_space() -> ActionSpace:
    """A small reference layout (AIR's own, not RDT's index table): extend or replace it."""
    slots = []
    for side in ("right", "left"):
        slots += [
            ActionSlot(name=f"{side}_arm_joint_pos", size=7, unit="rad"),
            ActionSlot(name=f"{side}_arm_joint_vel", size=7, unit="rad/s"),
            ActionSlot(name=f"{side}_eef_pos", size=3, unit="m"),
            ActionSlot(name=f"{side}_eef_rot6d", size=6, unit="1",
                       description="6D rotation representation (two matrix columns)"),
            ActionSlot(name=f"{side}_gripper_open", size=1, unit="1",
                       description="0 closed .. 1 open"),
        ]
    slots += [
        ActionSlot(name="base_linear_vel", size=2, unit="m/s"),
        ActionSlot(name="base_angular_vel", size=1, unit="rad/s"),
        ActionSlot(name="head_joint_pos", size=2, unit="rad"),
    ]
    return ActionSpace(space_id="air.default/1", slots=tuple(slots),
                       description="AIR reference cross-embodiment action layout")
