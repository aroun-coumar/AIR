"""Provenance data model, following W3C PROV: activities *use* and *generate* entities
and are *associated with* agents. Plus the baseline's requirement, maintenance and
experiment records (sections 2, 4 and 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ02", "REQ10", "REQ16")


class ActivityKind(StrEnum):
    RESEARCH = "research"
    DEVELOPMENT = "development"
    SIMULATION = "simulation"
    EXPERIMENT = "experiment"
    EVALUATION = "evaluation"
    RELEASE = "release"
    DEPLOYMENT = "deployment"
    EXECUTION = "execution"
    MAINTENANCE = "maintenance"
    CALIBRATION = "calibration"
    INCIDENT = "incident"
    DIAGNOSIS = "diagnosis"
    LEARNING = "learning"
    DECISION = "decision"


class ActivityStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


@contract("entity_ref", version="1.0")
class EntityRef(Contract):
    """A versioned thing an activity used or produced: a dataset, model, CAD file,
    configuration, simulation result, firmware image, report, physical component..."""

    entity_id: str
    kind: str
    name: str
    version: str | None = None
    digest: str | None = None
    uri: str | None = None
    size_bytes: int | None = None
    physical: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


@contract("activity", version="1.1")
class Activity(Contract):
    activity_id: str
    kind: ActivityKind
    title: str
    status: ActivityStatus
    started_at: float
    ended_at: float | None = None
    actors: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    used: tuple[str, ...] = ()
    generated: tuple[str, ...] = ()
    parent_id: str | None = None
    trace_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    outcome: str = ""
    error: str | None = None


class RequirementKind(StrEnum):
    REQUIREMENT = "requirement"
    GATE = "gate"
    DOMAIN = "domain"
    FOUNDATION = "foundation"


class RequirementStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Requirement:
    req_id: str
    title: str
    kind: RequirementKind = RequirementKind.REQUIREMENT
    text: str = ""
    owner: str = ""
    status: RequirementStatus = RequirementStatus.PLANNED
    traces_to: tuple[str, ...] = ()
    verification: tuple[str, ...] = ("test",)


@contract("requirement_set", version="1.0")
class RequirementSet(Contract):
    name: str
    requirements: tuple[Requirement, ...]

    def __post_init__(self) -> None:
        ids = [r.req_id for r in self.requirements]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SchemaError("duplicate requirement ids", ids=dupes)
        known = set(ids)
        for r in self.requirements:
            missing = [t for t in r.traces_to if t not in known]
            if missing:
                raise SchemaError("requirement traces to unknown ids", req_id=r.req_id,
                                  missing=missing)

    def get(self, req_id: str) -> Requirement | None:
        return next((r for r in self.requirements if r.req_id == req_id), None)


@dataclass(frozen=True)
class Replacement:
    removed: str
    installed: str
    reason: str = ""


@contract("maintenance_record", version="1.0")
class MaintenanceRecord(Contract):
    """Baseline section 16: lineage-preserving service record."""

    record_id: str
    robot_id: str
    components: tuple[str, ...]
    reason: str
    maintenance_type: str = "corrective"  # scheduled | usage | condition | predictive
    operating_mode: str = "maintenance"
    procedure: str = ""
    performed_by: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    replacements: tuple[Replacement, ...] = ()
    calibration_invalidated: tuple[str, ...] = ()
    return_to_service_checks: dict[str, bool] = field(default_factory=dict)
    exposure: dict[str, float] = field(default_factory=dict)

    @property
    def returned_to_service(self) -> bool:
        return bool(self.return_to_service_checks) and all(
            self.return_to_service_checks.values())


class SeedSource(StrEnum):
    """Where a seed came from. ``unset`` is recorded, never hidden (Isaac Lab ``seed=None``)."""

    USER = "user"
    DERIVED = "derived"
    DRAWN = "drawn"
    UNSET = "unset"


@dataclass(frozen=True)
class SeedRecord:
    """Every seed an experiment used (ManiSkill main/episode seeds, W3.2).

    ``seeded_rngs`` lists the generators that were *actually* seeded; anything else that
    draws random numbers makes the run irreproducible. ``sample_states`` points to the
    stored per-sample RNG snapshots, so one failing sample can be replayed alone."""

    root: int | None = None
    source: SeedSource = SeedSource.UNSET
    per_episode: tuple[int, ...] = ()
    per_env: tuple[int, ...] = ()
    seeded_rngs: tuple[str, ...] = ()
    derivation: str = ""
    sample_states: str = ""


@contract("experiment_record", version="1.1")
class ExperimentRecord(Contract):
    """Baseline section 10: everything needed to interpret, compare and reproduce.

    1.1 adds the seed record, the backend actually used, the platform and runner version
    that results may be compared within, the measured reproducibility, and the search
    trial identity (key, status and reason)."""

    experiment_id: str
    hypothesis: str
    requirement_ids: tuple[str, ...] = ()
    candidate: str = ""
    scenario: str = ""
    backend: str = ""
    backend_version: str = ""
    model_fidelity: str = ""
    seed: int | None = None
    numerical_settings: dict[str, Any] = field(default_factory=dict)
    resource_limits: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    accepted: bool | None = None
    limitations: tuple[str, ...] = ()
    seeds: SeedRecord | None = None
    #: An ``ar.backend_info`` document: requested vs actual device, resolved timing,
    #: declared capabilities (parsed strictly by ``engineering.backends``).
    backend_info: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    scenario_params: dict[str, Any] = field(default_factory=dict)
    platform: str = ""
    runner_version: str = ""
    physics_fingerprint: str = ""
    #: Measured by a reproducibility probe: exact | within_tolerance | differs; "" = not measured.
    reproducibility: str = ""
    search_id: str = ""
    trial_key: str = ""
    #: Terminal trial status: complete | failed | pruned | abandoned; "" outside a search.
    status: str = ""
    status_reason: str = ""
    budget: dict[str, Any] = field(default_factory=dict)
