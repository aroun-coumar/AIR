"""Contracts for agent-performed engineering work (baseline sections 8, 9, 13, 16)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..agents.providers.base import Location
from ..core.schema import Contract, contract
from .sandbox import SandboxPolicy

__requirements__ = ("AIR01", "AIR03", "REQ02", "REQ04", "REQ11", "REQ16", "REQ17")


class WorkKind(StrEnum):
    RESEARCH = "research"
    DEVELOPMENT = "development"
    SIMULATION = "simulation"
    EVALUATION = "evaluation"
    MAINTENANCE = "maintenance"
    DIAGNOSIS = "diagnosis"


class SideEffect(StrEnum):
    """What a tool can change. Ordered from harmless to consequential."""

    READ = "read"            # reads the workspace or project evidence
    WORKSPACE = "workspace"  # writes files inside the sandbox workspace
    COMPUTE = "compute"      # runs code inside the sandbox
    PROJECT = "project"      # records findings or proposals in the project ledger
    EXTERNAL = "external"    # reaches outside the sandbox (network, other systems)
    ROBOT = "robot"          # asks a robot to act (always through Runtime admission)
    PROMOTE = "promote"      # turns a proposal into an accepted change


class DataClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"   # must not leave the device: no cloud models


@dataclass(frozen=True)
class WorkBudget:
    max_model_calls: int = 20
    max_tool_calls: int = 60
    max_wall_s: float = 900.0


@dataclass(frozen=True)
class ModelConstraints:
    allowed_locations: tuple[Location, ...] = tuple(Location)
    allow_data_egress: bool = True
    robot_id: str | None = None
    prefer: tuple[str, ...] = ()


@contract("work_task", version="1.0")
class WorkTask(Contract):
    task_id: str
    kind: WorkKind
    goal: str
    requirements: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()          # project-relative files copied into the sandbox
    deliverables: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()           # allowed tool names; empty = the kind's defaults
    approval_required: tuple[SideEffect, ...] = (SideEffect.EXTERNAL, SideEffect.ROBOT,
                                                 SideEffect.PROMOTE)
    data_classification: DataClass = DataClass.INTERNAL
    budget: WorkBudget = field(default_factory=WorkBudget)
    sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    models: ModelConstraints = field(default_factory=ModelConstraints)
    robot_id: str | None = None           # the robot the work concerns, if any


class ProposalKind(StrEnum):
    REPORT = "report"
    PATCH = "patch"
    CONFIG = "config"
    EXPERIMENT_RESULT = "experiment_result"
    MAINTENANCE_PLAN = "maintenance_plan"
    DATASET = "dataset"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


@contract("work_proposal", version="1.0")
class Proposal(Contract):
    """An agent's deliverable. Proposals are never applied by the agent itself; a
    principal with ``promote`` authority approves or rejects them."""

    proposal_id: str
    task_id: str
    kind: ProposalKind
    title: str
    summary: str
    artifacts: tuple[str, ...] = ()       # entity ids (content-addressed in the project)
    evidence: tuple[str, ...] = ()        # ledger subjects / entity ids backing the claim
    requirements: tuple[str, ...] = ()
    produced_by: str = ""
    status: ProposalStatus = ProposalStatus.PROPOSED
