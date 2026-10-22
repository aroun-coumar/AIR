"""Generated JSON Schemas agree with the strict parser, and drift/compat checks work."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from agentic_robotics.contracts import (
    CapabilityProfile,
    CapabilityReport,
    Command,
    Delegation,
    Observation,
    ProfileStatus,
)
from agentic_robotics.core import SchemaError, load_any
from agentic_robotics.core.jsonschema import (
    check_schemas,
    contract_schema,
    export_schemas,
    render,
    schema_path,
)
from agentic_robotics.core.schema import registered_contracts, schema_uri
from agentic_robotics.integrations.fake import fake_skill_set
from agentic_robotics.provenance.traceability import baseline_requirements
from agentic_robotics.scenarios import handoff_mission

pytestmark = pytest.mark.req("REQ01", "REQ18", "F02", "F06")
ROOT = Path(__file__).resolve().parents[1]


def samples() -> list[dict[str, Any]]:
    docs = [p for p in (ROOT / "examples").rglob("*.json")]
    out = [json.loads(p.read_text("utf-8")) for p in docs]
    out += [c.to_dict() for c in (
        fake_skill_set(), baseline_requirements(), handoff_mission(),
        Delegation(delegation_id="d", grantor="a", grantee="b", valid_until=5.0),
        Command(command_id="c", robot_id="r", skill_id="s", issued_by="b", delegation_id="d",
                authority_revision=1, args={"x": 1.5}, fencing={"st": 2}, trace_id="ab" * 16),
        CapabilityReport(robot_id="r", capability_revision=1, observed_at=1.0,
                         clock_domain="sim",
                         profiles=(CapabilityProfile("m", ProfileStatus.RESTRICTED),)),
        Observation(source="s", signal="base.pose", value={"x": 1}, clock_domain="sim",
                    acquired_at=0.0, received_at=0.0, frame="map", spatial=True, unit="m"),
    )]
    return out


def validator_for(doc: dict[str, Any]) -> Draft202012Validator:
    cls = type(load_any(doc))
    return Draft202012Validator(contract_schema(cls))


@pytest.mark.parametrize("name", sorted(registered_contracts()))
def test_every_contract_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(contract_schema(registered_contracts()[name]))


@pytest.mark.parametrize("doc", samples(), ids=lambda d: d["$schema"])
def test_valid_documents_pass_both_parser_and_schema(doc: dict[str, Any]) -> None:
    load_any(doc)
    errors = list(validator_for(doc).iter_errors(doc))
    assert not errors, [e.message for e in errors]


def mutations(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    unknown = copy.deepcopy(doc)
    unknown["definitely_not_a_field"] = 1
    out.append(("unknown field", unknown))
    major = copy.deepcopy(doc)
    name, _, ver = doc["$schema"].partition("/")
    major["$schema"] = f"{name}/{int(ver.split('.')[0]) + 1}.0"
    out.append(("next major", major))
    cls = type(load_any(doc))
    required = contract_schema(cls).get("required", [])
    if required:
        missing = copy.deepcopy(doc)
        missing.pop(required[0], None)
        out.append((f"missing {required[0]}", missing))
    return out


@pytest.mark.parametrize("doc", samples(), ids=lambda d: d["$schema"])
def test_invalid_documents_fail_both_parser_and_schema(doc: dict[str, Any]) -> None:
    validator = validator_for(doc)
    for label, bad in mutations(doc):
        with pytest.raises(SchemaError):
            load_any(bad)
        assert list(validator.iter_errors(bad)), f"schema accepted: {label}"


def test_schema_uri_form_is_accepted_by_both() -> None:
    doc = fake_skill_set().to_dict()
    doc["$schema"] = schema_uri(doc["$schema"])
    assert load_any(doc) == fake_skill_set()
    assert not list(validator_for(fake_skill_set().to_dict()).iter_errors(doc))


def test_committed_schemas_are_current() -> None:
    assert check_schemas(ROOT / "schemas") == []


def test_drift_is_detected(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    cls = registered_contracts()["delegation"]
    path = schema_path(tmp_path, cls)
    doc = json.loads(path.read_text())
    doc["properties"]["sneaky"] = {"type": "string"}
    path.write_text(render(doc))
    assert any("differs from the contract" in p for p in check_schemas(tmp_path))


def test_minor_versions_must_be_backwards_compatible(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    cls = registered_contracts()["delegation"]  # current 1.0
    current = json.loads(schema_path(tmp_path, cls).read_text())

    compatible = copy.deepcopy(current)
    compatible["properties"]["new_optional"] = {"type": "string"}
    (tmp_path / "ar.delegation" / "1.1.json").write_text(render(compatible))
    assert not [p for p in check_schemas(tmp_path) if "1.0 -> 1.1" in p]

    breaking = copy.deepcopy(current)
    del breaking["properties"]["reason"]
    breaking["required"] = [*breaking["required"], "new_required"]
    breaking["properties"]["new_required"] = {"type": "string"}
    (tmp_path / "ar.delegation" / "1.1.json").write_text(render(breaking))
    problems = [p for p in check_schemas(tmp_path) if "1.0 -> 1.1" in p]
    assert any("reason: property removed" in p for p in problems)
    assert any("new required fields" in p for p in problems)
