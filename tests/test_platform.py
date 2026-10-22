"""Library, tool and platform surfaces: imports, CLI, plugins, conformance, registry."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import agentic_robotics
from agentic_robotics import LocalRegistry, MonotonicClock, Robot, SkillRegistry
from agentic_robotics.cli import main as cli
from agentic_robotics.conformance import check_adapter
from agentic_robotics.contracts import Delegation
from agentic_robotics.core import SchemaError
from agentic_robotics.integrations.fake import FakeRobotAdapter, fake_skill_set
from agentic_robotics.plugins import adapter_class, available_adapters

from .conftest import manifest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_minimal_import_has_no_heavy_dependencies() -> None:
    code = ("import sys, agentic_robotics; heavy = {'numpy','torch','rclpy','langchain'}; "
            "print(sorted(heavy & set(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"
    assert agentic_robotics.__version__


def test_fake_adapter_is_discovered_through_entry_points() -> None:
    assert "fake" in available_adapters()
    assert adapter_class("fake") is FakeRobotAdapter


async def test_fake_adapter_passes_conformance() -> None:
    robot = Robot.from_manifest(manifest())
    robot.adapter.bind_clock(MonotonicClock())
    report = await check_adapter(robot, SkillRegistry(fake_skill_set().skills))
    assert report.passed, report.summary()


async def test_custom_adapter_guide_passes_conformance() -> None:
    sys.path.insert(0, str(EXAMPLES))
    try:
        import custom_adapter
    finally:
        sys.path.pop(0)
    robot = Robot.from_manifest(custom_adapter.ComponentManifest(
        robot_id="buzzer-1", name="b", hardware_revision="r1",
        adapter=custom_adapter.AdapterRef(kind="example_buzzer"),
        nominal_capabilities=("audio",)))
    report = await check_adapter(robot, SkillRegistry([custom_adapter.BEEP]))
    assert report.passed, report.summary()


def test_cli_validate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    files = [str(p) for p in EXAMPLES.rglob("*.json")]
    assert cli(["validate", *files]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"$schema": "ar.delegation/1.0", "delegation_id": "x"}))
    assert cli(["validate", str(bad)]) == 1
    assert "missing required field" in capsys.readouterr().out


def test_cli_conformance(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli(["conformance", str(EXAMPLES / "robots" / "carrier.manifest.json"),
              "--skills", str(EXAMPLES / "skills" / "manifest.json")])
    assert rc == 0, capsys.readouterr().out


def test_cli_quickstart(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["quickstart"]) == 0
    out = capsys.readouterr().out
    assert "plan outcome: verified" in out
    assert "rejected: authority_revoked" in out


def test_registry_publish_fetch_and_immutability(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    skills = fake_skill_set()
    ref = reg.publish("fake-skills", "1.0.0", skills)
    assert reg.fetch("fake-skills") == skills
    assert reg.publish("fake-skills", "1.0.0", skills) == ref  # idempotent
    other = Delegation(delegation_id="d", grantor="a", grantee="b")
    with pytest.raises(SchemaError, match="immutable"):
        reg.publish("fake-skills", "1.0.0", other)
    reg.publish("fake-skills", "1.10.0", skills)
    reg.publish("fake-skills", "1.2.0", skills)
    assert reg.versions("fake-skills") == ["1.0.0", "1.2.0", "1.10.0"]


def test_registry_detects_tampering(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    reg.publish("policy", "1", Delegation(delegation_id="d", grantor="a", grantee="b"))
    path = tmp_path / "policy" / "1.json"
    record = json.loads(path.read_text())
    record["document"]["grantee"] = "agent:attacker"
    path.write_text(json.dumps(record))
    with pytest.raises(SchemaError, match="integrity"):
        reg.fetch("policy")


def test_registry_rejects_path_like_names(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    with pytest.raises(SchemaError):
        reg.publish("../escape", "1", Delegation(delegation_id="d", grantor="a", grantee="b"))
