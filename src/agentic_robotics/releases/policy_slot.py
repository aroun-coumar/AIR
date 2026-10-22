"""A robot's policy slot: load gate, origin authority, two-step start, checked inference,
provenance-carrying action chunks with a staleness bound, and a declared exit mode.

    slot = PolicySlot("duck-1", "locomotion", requirements, clock=clock, trace=trace)
    outcome = slot.load(manifest, model_bytes, runner_factory=make_runner)
    token = slot.arm("operator:ana")          # step 1
    slot.start(token, "operator:ana")         # step 2, explicit
    chunk = slot.infer({"imu": [...], "joints": [...]})
    actions = slot.accept(chunk)              # refuses stale or foreign chunks
    slot.stop("done")                          # returns the declared exit mode

Rules (ADR 0016):
- The model bytes must match the manifest digest, and the manifest must pass the slot's
  :class:`PolicyRequirements`. Any mismatch refuses the load; **a failed load keeps the
  previously loaded policy** (and its runner) untouched.
- Only ``official`` policies load without authority. ``community`` and ``local`` ones
  need a delegation covering operation ``load_policy`` on skill ``policy:<slot>``,
  checked by the runtime's :class:`AuthorityService`. A manifest cannot vouch for itself.
- Starting takes two steps (arm, then start with the arm token) within a timeout.
- Observations are checked before inference and actions after it (finite, shaped,
  within declared bounds). The runner is opaque (any callable); AIR never imports a
  machine-learning framework.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts.policy import (
    ActionChunk,
    ActionProvenance,
    ExitMode,
    PolicyManifest,
    PolicyOrigin,
    PolicyRequirements,
)
from ..core.clock import Clock
from ..core.errors import AuthorityError, FrameworkError
from ..core.ids import new_id
from ..provenance.project import bytes_digest

__requirements__ = ("REQ05", "REQ11", "REQ12", "REQ16", "REQ17", "G05", "G12", "G15")

Runner = Callable[[Sequence[float]], Sequence[Sequence[float]]]
RunnerFactory = Callable[[PolicyManifest, bytes], Runner]


class PolicyError(FrameworkError):
    code = "policy_error"

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message, reason=reason, **details)
        self.reason = reason


class SlotState(StrEnum):
    EMPTY = "empty"
    LOADED = "loaded"        # loaded, not running
    ARMED = "armed"          # step 1 of start done
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class LoadOutcome:
    loaded: bool
    identity: str
    reasons: tuple[str, ...] = ()
    active: str | None = None            # identity active after the attempt


@dataclass
class _Loaded:
    manifest: PolicyManifest
    identity: str
    runner: Runner | None
    generation: int
    session_id: str


class PolicySlot:
    def __init__(self, robot_id: str, slot: str, requirements: PolicyRequirements, *,
                 clock: Clock, trace: Any | None = None, authority: Any | None = None,
                 arm_timeout_s: float = 10.0, project: Any | None = None) -> None:
        self.robot_id = robot_id
        self.slot = slot
        self.requirements = requirements
        self.clock = clock
        self.trace = trace
        self.authority = authority
        self.arm_timeout_s = arm_timeout_s
        self.project = project
        self.state = SlotState.EMPTY
        self._current: _Loaded | None = None
        self._previous: _Loaded | None = None
        self._generation = 0
        self._arm: tuple[str, str, float] | None = None   # token, principal, armed_at

    # -- queries -------------------------------------------------------------------------
    @property
    def manifest(self) -> PolicyManifest | None:
        return self._current.manifest if self._current else None

    @property
    def identity(self) -> str | None:
        return self._current.identity if self._current else None

    @property
    def generation(self) -> int:
        return self._current.generation if self._current else 0

    @property
    def previous_identity(self) -> str | None:
        return self._previous.identity if self._previous else None

    # -- load gate -----------------------------------------------------------------------
    def gate(self, manifest: PolicyManifest, model: bytes, *, principal: str | None = None,
             delegation_id: str | None = None) -> list[str]:
        """Every reason this policy may not load here (empty: it may)."""
        reasons = []
        actual = bytes_digest(model)
        if actual != manifest.model_digest:
            reasons.append(f"model digest {actual} does not match the manifest "
                           f"{manifest.model_digest}")
        reasons += self.requirements.mismatches(manifest)
        if manifest.origin is not PolicyOrigin.OFFICIAL:
            if self.authority is None or not delegation_id or not principal:
                reasons.append(f"{manifest.origin.value} policy needs explicit authority "
                               "(a delegation for load_policy)")
            else:
                try:
                    self.authority.check(delegation_id, principal, self.robot_id,
                                         f"policy:{self.slot}", operation="load_policy")
                except AuthorityError as exc:
                    reasons.append(f"authority refused: {exc}")
        if self.state in (SlotState.RUNNING, SlotState.ARMED):
            reasons.append("stop the running policy before loading another")
        return reasons

    def load(self, manifest: PolicyManifest, model: bytes, *,
             runner_factory: RunnerFactory | None = None, principal: str | None = None,
             delegation_id: str | None = None) -> LoadOutcome:
        identity = manifest.identity()
        reasons = self.gate(manifest, model, principal=principal, delegation_id=delegation_id)
        runner: Runner | None = None
        if not reasons and runner_factory is not None:
            try:
                runner = runner_factory(manifest, model)
            except Exception as exc:  # the runner is third-party code
                reasons.append(f"runner failed to load: {exc!r}")
        if reasons:
            self._record("policy_load_refused", identity=identity, reasons=reasons,
                         kept=self.identity, origin=manifest.origin.value)
            return LoadOutcome(False, identity, tuple(reasons), self.identity)
        self._generation += 1
        self._previous = self._current
        self._current = _Loaded(manifest, identity, runner, self._generation,
                                new_id("pss"))
        self.state = SlotState.LOADED
        self._record("policy_loaded", identity=identity, generation=self._generation,
                     previous=self.previous_identity, origin=manifest.origin.value,
                     model_digest=manifest.model_digest, by=principal or "")
        return LoadOutcome(True, identity, (), identity)

    def rollback(self) -> str:
        """Re-activate the previously loaded policy (not running)."""
        if self._previous is None:
            raise PolicyError("no_previous", "there is no previous policy")
        if self.state in (SlotState.RUNNING, SlotState.ARMED):
            raise PolicyError("running", "stop the running policy first")
        self._generation += 1
        prev = self._previous
        self._previous = self._current
        self._current = _Loaded(prev.manifest, prev.identity, prev.runner,
                                self._generation, new_id("pss"))
        self.state = SlotState.LOADED
        self._record("policy_rolled_back", identity=prev.identity,
                     generation=self._generation)
        return prev.identity

    # -- two-step start ------------------------------------------------------------------
    def arm(self, principal: str) -> str:
        if self.state not in (SlotState.LOADED, SlotState.STOPPED) or self._current is None:
            raise PolicyError("not_loaded", "load a policy before arming",
                              state=self.state.value)
        token = new_id("arm")
        self._arm = (token, principal, self.clock.now())
        self.state = SlotState.ARMED
        self._record("policy_armed", identity=self.identity, by=principal)
        return token

    def start(self, token: str, principal: str) -> None:
        if self.state is not SlotState.ARMED or self._arm is None:
            raise PolicyError("not_armed", "arm the slot first (two-step start)")
        expected, _, armed_at = self._arm
        if token != expected:
            raise PolicyError("bad_token", "arm token does not match")
        if self.clock.now() - armed_at > self.arm_timeout_s:
            self._arm = None
            self.state = SlotState.LOADED
            self._record("policy_arm_expired", identity=self.identity)
            raise PolicyError("arm_expired", "arming expired; arm again")
        self._arm = None
        self.state = SlotState.RUNNING
        self._record("policy_started", identity=self.identity, by=principal,
                     exit_mode=self._manifest().exit_mode.value)

    def stop(self, reason: str = "") -> ExitMode:
        m = self._manifest()
        self._arm = None
        self.state = SlotState.STOPPED
        self._record("policy_stopped", identity=self.identity, reason=reason,
                     exit_mode=m.exit_mode.value)
        return m.exit_mode

    # -- inference -----------------------------------------------------------------------
    def check_observation(self, observation: Mapping[str, Sequence[float]]) -> list[float]:
        m = self._manifest()
        declared = {f.name for f in m.observation_features}
        extra = sorted(set(observation) - declared)
        if extra:
            raise PolicyError("bad_observation", "observation has undeclared features",
                              features=extra)
        flat: list[float] = []
        for f in m.observation_features:
            if f.name not in observation:
                raise PolicyError("bad_observation", "observation feature missing",
                                  feature=f.name)
            values = [float(v) for v in observation[f.name]]
            if len(values) != f.size:
                raise PolicyError("bad_observation", "observation feature has the wrong "
                                  "size", feature=f.name, expected=f.size, actual=len(values))
            if not all(math.isfinite(v) for v in values):
                raise PolicyError("bad_observation", "observation is not finite",
                                  feature=f.name)
            flat += values
        return flat

    def check_action(self, actions: Sequence[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
        m = self._manifest()
        rows = tuple(tuple(float(v) for v in row) for row in actions)
        if len(rows) != m.prediction_steps:
            raise PolicyError("bad_action", "runner returned the wrong number of steps",
                              expected=m.prediction_steps, actual=len(rows))
        for i, row in enumerate(rows):
            if len(row) != m.action_dim:
                raise PolicyError("bad_action", "action has the wrong size", step=i,
                                  expected=m.action_dim, actual=len(row))
            if not all(math.isfinite(v) for v in row):
                raise PolicyError("bad_action", "action is not finite", step=i)
            if m.action_low and any(v < lo for v, lo in zip(row, m.action_low, strict=True)):
                raise PolicyError("bad_action", "action below declared bounds", step=i)
            if m.action_high and any(v > hi for v, hi in zip(row, m.action_high,
                                                             strict=True)):
                raise PolicyError("bad_action", "action above declared bounds", step=i)
        return rows

    def infer(self, observation: Mapping[str, Sequence[float]], *, observation_id: str = "",
              capture_time: float | None = None) -> ActionChunk:
        if self.state is not SlotState.RUNNING or self._current is None:
            raise PolicyError("not_running", "the policy is not running",
                              state=self.state.value)
        cur = self._current
        if cur.runner is None:
            raise PolicyError("no_runner", "this slot was loaded without a runner")
        request_id = new_id("req")
        try:
            flat = self.check_observation(observation)
            rows = self.check_action(cur.runner(flat))
        except PolicyError as exc:
            self._record("policy_check_failed", identity=cur.identity, reason=exc.reason,
                         error=str(exc), request_id=request_id)
            raise
        m = cur.manifest
        return ActionChunk(actions=rows, provenance=ActionProvenance(
            request_id=request_id, generation=cur.generation, session_id=cur.session_id,
            artifact=cur.identity, observation_id=observation_id, capture_time=capture_time),
            created_at=self.clock.now(), max_age_s=m.max_action_age_s,
            execution_steps=m.execution_steps)

    def accept(self, chunk: ActionChunk) -> tuple[tuple[float, ...], ...]:
        """The actions to execute from ``chunk``, or a refusal if it is stale or foreign."""
        cur = self._current
        p = chunk.provenance
        reason = None
        if self.state is not SlotState.RUNNING or cur is None:
            reason = "not_running"
        elif p.artifact != cur.identity or p.generation != cur.generation \
                or p.session_id != cur.session_id:
            reason = "foreign_chunk"
        elif chunk.max_age_s > cur.manifest.max_action_age_s:
            reason = "staleness_bound_widened"
        elif chunk.stale(self.clock.now()):
            reason = "stale_chunk"
        if reason:
            self._record("policy_chunk_refused", identity=self.identity, reason=reason,
                         request_id=p.request_id, age_s=chunk.age(self.clock.now()))
            raise PolicyError(reason, "action chunk refused", request_id=p.request_id)
        return chunk.actions[: chunk.execution_steps]

    # -- internals -----------------------------------------------------------------------
    def _manifest(self) -> PolicyManifest:
        if self._current is None:
            raise PolicyError("not_loaded", "no policy is loaded")
        return self._current.manifest

    def _record(self, kind: str, **data: Any) -> None:
        subject = f"{self.robot_id}/{self.slot}"
        if self.trace is not None:
            self.trace.record(kind, subject, **data)
        if self.project is not None and kind in ("policy_loaded", "policy_load_refused",
                                                 "policy_rolled_back", "policy_started",
                                                 "policy_stopped"):
            self.project.ledger.append(kind.replace("_", ".", 1), subject, data)
