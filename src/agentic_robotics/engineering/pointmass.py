"""Deterministic pure-Python reference backend: a 1-D point mass driven to a target by a
PD controller, with seeded force disturbances.

It needs no simulator, so every engineering path (probe, golden runs, search, release)
is testable offline. The action is the controller configuration (``kp``, ``kd``,
optionally ``force_limit``); the backend applies it at every physics substep.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from typing import Any

from ..core.errors import SchemaError
from .backends import (
    BackendCapabilities,
    BackendInfo,
    DeterminismClass,
    Fidelity,
    RestoreLevel,
    Snapshot,
    TimePolicy,
    check_load,
    platform_key,
)

__requirements__ = ("REQ04", "G03")


def encode_rng_state(state: Any) -> list[Any]:
    """``random.Random.getstate()`` as plain JSON."""
    version, internal, gauss = state
    return [version, list(internal), gauss]


def decode_rng_state(data: Any) -> tuple[Any, ...]:
    if not (isinstance(data, list) and len(data) == 3):
        raise SchemaError("bad RNG state snapshot")
    return (data[0], tuple(data[1]), data[2])


class PointMassBackend:
    name = "pointmass"
    version = "1.0"
    capabilities = BackendCapabilities(
        fidelity={"dynamics": Fidelity.SIMPLIFIED, "contact": Fidelity.NONE,
                  "sensing": Fidelity.NONE, "actuation": Fidelity.SIMPLIFIED,
                  "timing": Fidelity.SIMPLIFIED},
        determinism=DeterminismClass.BITWISE_SAME_HOST,
        restore_level=RestoreLevel.INTEGRATION,
        features=("disturbance", "friction"),
        unsupported=("contact", "gpu"),
        seeded_rngs=("random.Random(disturbance)",),
        supports_snapshot=True, supports_lockstep=True, supports_rng_state=True)

    def __init__(self) -> None:
        self._tp: TimePolicy | None = None
        self._scenario: dict[str, Any] = {}
        self._rng = random.Random(0)
        self._x = self._v = self._t = 0.0
        self._steps = 0
        self._acc: dict[str, float] = {}

    def load(self, scenario: Mapping[str, Any], time_policy: TimePolicy, *,
             features: Iterable[str] = (), device: str = "cpu") -> BackendInfo:
        feats = tuple(features)
        reasons = check_load(self.capabilities, time_policy, feats, self.name)
        unknown = set(scenario) - {"target", "mass", "noise", "friction", "x0", "tolerance"}
        if unknown:
            raise SchemaError("unknown scenario keys", backend=self.name, keys=sorted(unknown))
        self._tp = time_policy
        self._scenario = dict(scenario)
        return BackendInfo(
            backend=self.name, version=self.version, platform=platform_key(),
            requested_device=device, actual_device="cpu", capabilities=self.capabilities,
            time_policy=time_policy, resolved_physics_dt=time_policy.physics_dt,
            resolved_output_interval=time_policy.control_dt, features=feats,
            non_reproducible=reasons)

    def _p(self, key: str, default: float) -> float:
        return float(self._scenario.get(key, default))

    def reset(self, seed: int | None) -> dict[str, Any]:
        if self._tp is None:
            raise SchemaError("load() before reset()")
        self._rng = random.Random(seed)
        self._x, self._v, self._t, self._steps = self._p("x0", 0.0), 0.0, 0.0, 0
        self._acc = {"energy": 0.0, "overshoot": 0.0, "settle_time": 0.0}
        return self._obs()

    def _obs(self) -> dict[str, Any]:
        return {"t": self._t, "x": self._x, "v": self._v, "step": self._steps}

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        tp = self._tp
        if tp is None:
            raise SchemaError("load() before step()")
        kp, kd = float(action.get("kp", 1.0)), float(action.get("kd", 0.0))
        limit = float(action.get("force_limit", 10.0))
        target, mass = self._p("target", 1.0), self._p("mass", 1.0)
        noise, friction, tol = self._p("noise", 0.05), self._p("friction", 0.1), \
            self._p("tolerance", 0.02)
        dt = tp.physics_dt
        for _ in range(tp.decimation):
            force = kp * (target - self._x) - kd * self._v
            force = max(-limit, min(limit, force))
            disturbance = self._rng.gauss(0.0, noise) if noise > 0 else 0.0
            accel = (force + disturbance - friction * self._v) / mass
            self._v += accel * dt
            self._x += self._v * dt
            self._t += dt
            self._acc["energy"] += force * force * dt
            self._acc["overshoot"] = max(self._acc["overshoot"], self._x - target)
            if abs(target - self._x) > tol:
                self._acc["settle_time"] = self._t
        self._steps += 1
        return self._obs()

    def snapshot(self) -> Snapshot:
        return Snapshot(state={"x": self._x, "v": self._v, "t": self._t,
                               "steps": self._steps, "acc": dict(self._acc),
                               "rng": encode_rng_state(self._rng.getstate())},
                        level=RestoreLevel.INTEGRATION)

    def restore(self, snapshot: Snapshot) -> None:
        s = snapshot.state
        self._x, self._v, self._t = float(s["x"]), float(s["v"]), float(s["t"])
        self._steps = int(s["steps"])
        self._acc = {k: float(v) for k, v in s["acc"].items()}
        self._rng.setstate(decode_rng_state(s["rng"]))

    def rng_state(self) -> Any:
        return encode_rng_state(self._rng.getstate())

    def set_rng_state(self, state: Any) -> None:
        self._rng.setstate(decode_rng_state(state))

    def metrics(self) -> dict[str, float]:
        target = self._p("target", 1.0)
        return {"final_error": abs(target - self._x), "overshoot": self._acc.get("overshoot", 0.0),
                "energy": self._acc.get("energy", 0.0),
                "settle_time": self._acc.get("settle_time", 0.0)}

    def close(self) -> None:
        self._tp = None
