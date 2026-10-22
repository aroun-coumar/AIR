"""Release, activation, asset-card and conformance-report contracts (ADR 0016).

- ``ar.release/1.0``: a set of target files with a monotonic **security counter** and
  the named checks a trial must pass before it confirms itself (mcuboot pattern).
- ``ar.release_activation/1.0``: one robot slot's activation state: active, previous,
  pending, trial, stored security counter and history.
- ``ar.asset_card/1.0``: model/dataset/policy/skill cards. Cards are claims for people
  and agents to read; they never grant authority.
- ``ar.conformance_report/1.0``: the versioned output of the adapter conformance suite.
  ``passed`` must agree with the outcomes, so a report cannot assert its own badge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.ids import is_valid_id
from ..core.schema import Contract, contract

__requirements__ = ("REQ10", "REQ16", "REQ18", "F04", "F06", "F10", "G14", "G15")


@dataclass(frozen=True)
class ReleaseTarget:
    path: str                            # e.g. "policies/walk/1.2.0.onnx" or a registry ref
    digest: str                          # sha256:<hex>
    length: int
    slot: str = ""                       # where it installs, e.g. "policy.locomotion"

    def __post_init__(self) -> None:
        if not self.digest.startswith("sha256:") or self.length < 0:
            raise SchemaError("release target needs a sha256 digest and a length",
                              path=self.path)


@contract("release", version="1.0")
class Release(Contract):
    release_id: str
    name: str
    version: str
    security_counter: int
    targets: tuple[ReleaseTarget, ...]
    confirm_checks: tuple[str, ...]      # every one must pass, observed, before confirm
    confirm_within_s: float | None = None
    notes: str = ""
    created_at: float | None = None

    def __post_init__(self) -> None:
        if not is_valid_id(self.release_id):
            raise SchemaError("invalid release id", release_id=self.release_id)
        if self.security_counter < 0:
            raise SchemaError("security counter must be non-negative")
        if not self.targets:
            raise SchemaError("a release needs at least one target")
        if not self.confirm_checks:
            raise SchemaError("a release must name the checks that confirm it; a release "
                              "that confirms unconditionally is not a trial")
        if self.confirm_within_s is not None and self.confirm_within_s <= 0:
            raise SchemaError("confirm_within_s must be positive")

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


class ActivationStateKind(StrEnum):
    IDLE = "idle"                        # nothing pending
    STAGED = "staged"                    # pending release waits for the next boot
    TRIAL = "trial"                      # running provisionally; reverts unless confirmed
    CONFIRMED = "confirmed"              # active release confirmed itself


@dataclass(frozen=True)
class ActivationEvent:
    t: float
    kind: str                            # staged, trial, confirmed, reverted, refused
    release: str = ""
    detail: str = ""


@contract("release_activation", version="1.0")
class ReleaseActivation(Contract):
    robot_id: str
    slot: str
    state: ActivationStateKind = ActivationStateKind.IDLE
    active: Release | None = None
    previous: Release | None = None
    pending: Release | None = None
    security_counter: int = 0            # only rises, and only on confirm
    trial_started_at: float | None = None
    trial_boots: int = 0
    history: tuple[ActivationEvent, ...] = ()


# -- asset cards ---------------------------------------------------------------------------

class AssetKind(StrEnum):
    MODEL = "model"
    DATASET = "dataset"
    POLICY = "policy"
    SKILL = "skill"


_SPDX = re.compile(r"^(?:[A-Za-z0-9.+-]+|LicenseRef-[A-Za-z0-9.-]+)"
                   r"(?:\s+(?:AND|OR|WITH)\s+(?:[A-Za-z0-9.+-]+|LicenseRef-[A-Za-z0-9.-]+))*$")


@dataclass(frozen=True)
class EvaluationEvidence:
    """One evaluation result and where its evidence lives (a ledger entry, a report)."""

    name: str
    metric: str
    value: float | None = None
    scenario_digest: str | None = None
    seeds: tuple[int, ...] = ()
    air_version: str = ""
    platform: str = ""
    evidence: str = ""                   # e.g. "ledger:1234", "experiment:<id>", a URL


@contract("asset_card", version="1.0")
class AssetCard(Contract):
    """What an asset is for and where it stops. A claim, never authority."""

    asset: str                           # registry ref name@version#digest or entity id
    kind: AssetKind
    title: str
    license: str                         # SPDX expression
    summary: str = ""
    intended_use: tuple[str, ...] = ()
    out_of_scope_use: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    hazards: tuple[str, ...] = ()
    operating_envelope: dict[str, Any] = field(default_factory=dict)
    evaluations: tuple[EvaluationEvidence, ...] = ()
    conformance_report: str | None = None    # digest of an ar.conformance_report
    requirements: tuple[str, ...] = ()
    maintainers: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not _SPDX.match(self.license):
            raise SchemaError("license must be an SPDX expression", license=self.license)
        if not self.intended_use:
            raise SchemaError("an asset card must state its intended use", asset=self.asset)

    def problems(self) -> list[str]:
        """Gaps a reviewer should see (not errors: a card may be honest about them)."""
        out = []
        if not self.out_of_scope_use:
            out.append("no out-of-scope use stated")
        if not self.limitations:
            out.append("no limitations stated")
        if not self.evaluations:
            out.append("no evaluation evidence")
        elif any(not e.evidence for e in self.evaluations):
            out.append("an evaluation has no evidence link")
        if self.kind in (AssetKind.POLICY, AssetKind.SKILL) and not self.hazards:
            out.append("no hazards stated for an asset that moves hardware")
        return out


# -- conformance reports -----------------------------------------------------------------

@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    passed: bool
    detail: str = ""


@contract("conformance_report", version="1.0")
class ConformanceReportRecord(Contract):
    robot_id: str
    adapter_kind: str
    suite_version: str
    air_version: str
    passed: bool
    outcomes: tuple[ConformanceCheck, ...]
    contract_versions: dict[str, str] = field(default_factory=dict)
    manifest_digest: str | None = None
    skills_digest: str | None = None
    generated_at: float | None = None

    def __post_init__(self) -> None:
        if not self.outcomes:
            raise SchemaError("a conformance report needs outcomes")
        if self.passed != all(o.passed for o in self.outcomes):
            raise SchemaError("passed does not agree with the outcomes")
