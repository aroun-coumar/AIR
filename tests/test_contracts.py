"""Stage 1 evidence: schema, compatibility and semantics tests for the public contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_robotics.contracts import (
    CapabilityProfile,
    CapabilityReport,
    Command,
    ComponentManifest,
    Condition,
    Delegation,
    Observation,
    Op,
    ProfileStatus,
    SkillSet,
    TaskPlan,
    TaskSpec,
)
from agentic_robotics.core import (
    IncompatibleSchemaVersion,
    Quantity,
    SchemaError,
    UnitError,
    convert,
    load_any,
)
from agentic_robotics.integrations.fake import fake_skill_set

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def obs(signal: str, value: object, t: float = 0.0, **kw: object) -> Observation:
    return Observation(source="s", signal=signal, value=value, clock_domain="sim",
                       acquired_at=t, received_at=t, **kw)  # type: ignore[arg-type]


def test_skill_set_round_trips_through_json() -> None:
    skills = fake_skill_set()
    again = SkillSet.from_json(skills.to_json())
    assert again == skills
    assert skills.to_dict()["$schema"] == "ar.skill_set/1.0"


@pytest.mark.parametrize("contract", [
    Delegation(delegation_id="d1", grantor="a", grantee="b", robots=("r1",)),
    Command(command_id="c1", robot_id="r1", skill_id="s", issued_by="b", delegation_id="d1",
            authority_revision=3, args={"x": 1.5}, fencing={"station": 2}),
    CapabilityReport(robot_id="r1", capability_revision=2, observed_at=1.0, clock_domain="sim",
                     profiles=(CapabilityProfile("mobility", ProfileStatus.RESTRICTED,
                                                 limits={"v": 0.5},
                                                 limiting_factors=("battery_low",)),)),
    obs("base.pose", {"x": 1.0}, frame="map", spatial=True, unit="m"),
])
def test_contracts_round_trip(contract: object) -> None:
    cls = type(contract)
    assert cls.from_dict(contract.to_dict()) == contract  # type: ignore[attr-defined]
    assert load_any(contract.to_dict()) == contract  # type: ignore[attr-defined]


def test_unknown_fields_are_rejected() -> None:
    doc = Delegation(delegation_id="d1", grantor="a", grantee="b").to_dict()
    doc["robtos"] = ["typo"]
    with pytest.raises(SchemaError, match="unknown fields"):
        Delegation.from_dict(doc)


def test_missing_and_mistyped_fields_are_rejected() -> None:
    with pytest.raises(SchemaError, match="missing required field"):
        Delegation.from_dict({"delegation_id": "d1", "grantor": "a"})
    with pytest.raises(SchemaError, match="expected an integer"):
        Command.from_dict({"command_id": "c", "robot_id": "r", "skill_id": "s",
                           "issued_by": "b", "delegation_id": "d",
                           "authority_revision": "3"})


def test_major_version_mismatch_is_rejected_minor_is_accepted() -> None:
    doc = Delegation(delegation_id="d1", grantor="a", grantee="b").to_dict()
    doc["$schema"] = "ar.delegation/2.0"
    with pytest.raises(IncompatibleSchemaVersion):
        Delegation.from_dict(doc)
    doc["$schema"] = "ar.delegation/1.7"
    assert Delegation.from_dict(doc).delegation_id == "d1"


def test_wrong_schema_name_is_rejected() -> None:
    doc = Delegation(delegation_id="d1", grantor="a", grantee="b").to_dict()
    with pytest.raises(SchemaError, match="different schema"):
        Command.from_dict(doc)


def test_spatial_observation_requires_frame() -> None:
    with pytest.raises(SchemaError, match="frame"):
        obs("base.pose", {"x": 0}, spatial=True)


def test_observation_receipt_cannot_precede_acquisition() -> None:
    with pytest.raises(SchemaError):
        Observation(source="s", signal="x", value=1, clock_domain="sim", acquired_at=2.0,
                    received_at=1.0)


def test_units() -> None:
    assert convert(1500, "mm", "m") == pytest.approx(1.5)
    assert Quantity(90, "deg").to("rad").value == pytest.approx(3.14159265 / 2)
    with pytest.raises(UnitError):
        convert(1, "m", "s")
    with pytest.raises(UnitError):
        Quantity(1, "furlong")


def test_condition_resolves_arguments_and_tolerance() -> None:
    c = Condition(signal="base.pose", field="x", op=Op.APPROX, value="$args.x", tolerance=0.1)
    observations = {"base.pose": obs("base.pose", {"x": 2.05}, frame="map", spatial=True)}
    assert c.evaluate(observations, {"x": 2.0}, now=0.0).satisfied
    assert not c.evaluate(observations, {"x": 3.0}, now=0.0).satisfied
    missing = c.evaluate({}, {"x": 2.0}, now=0.0)
    assert not missing.satisfied and missing.detail


def test_skill_argument_validation() -> None:
    nav = next(s for s in fake_skill_set().skills if s.skill_id == "navigate_to")
    assert nav.validate_args({"x": 1, "y": 2}) == []
    problems = nav.validate_args({"x": "far", "z": 1})
    assert any("x" in p for p in problems)
    assert any("y: required" in p for p in problems)
    assert any("z: unknown" in p for p in problems)
    assert nav.validate_args({"x": 99, "y": 0}) == ["x: above maximum 50.0"]


def test_plan_rejects_forward_dependencies() -> None:
    with pytest.raises(SchemaError, match="later or unknown"):
        TaskPlan(plan_id="p", goal="g", steps=(
            TaskSpec(task_id="a", skill_id="s", robot_id="r", depends_on=("b",)),
            TaskSpec(task_id="b", skill_id="s", robot_id="r")))


def test_manifest_component_tree_is_checked() -> None:
    doc = json.loads((EXAMPLES / "robots" / "carrier.manifest.json").read_text())
    doc["components"][1]["parent"] = "nonexistent"
    with pytest.raises(SchemaError, match="parent not found"):
        ComponentManifest.from_dict(doc)


def test_delegation_narrowing() -> None:
    parent = Delegation(delegation_id="p", grantor="root", grantee="lead", robots=("r1", "r2"),
                        valid_until=100.0, limits={"speed": 1.0})
    ok = Delegation(delegation_id="c", grantor="lead", grantee="agent", robots=("r1",),
                    parent_id="p", valid_until=50.0, limits={"speed": 0.5})
    assert ok.within(parent) == []
    broad = Delegation(delegation_id="c2", grantor="lead", grantee="agent", parent_id="p")
    problems = broad.within(parent)
    assert "robots broader than parent" in problems
    assert "outlives parent" in problems
    assert "limit speed broader than parent" in problems


@pytest.mark.parametrize("path", sorted(EXAMPLES.rglob("*.json")), ids=lambda p: p.name)
def test_example_documents_validate(path: Path) -> None:
    load_any(json.loads(path.read_text("utf-8")))
