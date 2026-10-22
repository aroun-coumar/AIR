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

pytestmark = pytest.mark.req("REQ18", "REQ01")
reuse = pytest.mark.req("G01")
conformance = pytest.mark.req("F06", "REQ03")
integrity = pytest.mark.req("REQ10", "G15")


@reuse
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


@conformance
async def test_fake_adapter_passes_conformance() -> None:
    robot = Robot.from_manifest(manifest())
    robot.adapter.bind_clock(MonotonicClock())
    report = await check_adapter(robot, SkillRegistry(fake_skill_set().skills))
    assert report.passed, report.summary()


@conformance
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


@conformance
def test_cli_conformance(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli(["conformance", str(EXAMPLES / "robots" / "carrier.manifest.json"),
              "--skills", str(EXAMPLES / "skills" / "manifest.json")])
    assert rc == 0, capsys.readouterr().out


@reuse
def test_cli_quickstart(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["quickstart"]) == 0
    out = capsys.readouterr().out
    assert "plan outcome: verified" in out
    assert "rejected: authority_revoked" in out


@integrity
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


@integrity
def test_registry_detects_tampering(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    reg.publish("policy", "1", Delegation(delegation_id="d", grantor="a", grantee="b"))
    path = tmp_path / "policy" / "1.json"
    record = json.loads(path.read_text())
    record["document"]["grantee"] = "agent:attacker"
    path.write_text(json.dumps(record))
    with pytest.raises(SchemaError, match="integrity"):
        reg.fetch("policy")


@integrity
def test_registry_detects_document_and_stored_digest_rewritten_together(
        tmp_path: Path) -> None:
    from agentic_robotics.registry import document_digest

    reg = LocalRegistry(tmp_path)
    reg.publish("policy", "1", Delegation(delegation_id="d", grantor="a", grantee="b"))
    path = tmp_path / "policy" / "1.json"
    record = json.loads(path.read_text())
    record["document"]["grantee"] = "agent:attacker"
    record["ref"]["digest"] = document_digest(record["document"])  # self-consistent forgery
    path.write_text(json.dumps(record))
    with pytest.raises(SchemaError, match="integrity"):
        reg.fetch("policy")
    assert reg.verify_log()


@integrity
def test_registry_digest_pinning_and_refs(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    ref = reg.publish("skills", "1.0.0", fake_skill_set())
    assert reg.fetch("skills", "1.0.0", digest=ref.digest) == fake_skill_set()
    assert reg.resolve(str(ref)) == fake_skill_set()
    with pytest.raises(SchemaError, match="pinned digest"):
        reg.fetch("skills", "1.0.0", digest="sha256:" + "0" * 64)


def test_registry_version_precedence_and_prereleases(tmp_path: Path) -> None:
    from agentic_robotics.registry import is_prerelease, version_key

    ordered = ["0.9", "1.0.0-alpha", "1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0rc1",
               "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2"]
    assert sorted(reversed(ordered), key=version_key) == ordered
    assert version_key("1.0") == version_key("1.0.0")
    assert is_prerelease("1.0.0-rc.1") and is_prerelease("2.0.0rc1")
    assert not is_prerelease("1.10.0")

    reg = LocalRegistry(tmp_path)
    skills = fake_skill_set()
    reg.publish("s", "1.0.0", skills)
    reg.publish("s", "1.1.0-rc1", Delegation(delegation_id="d", grantor="a", grantee="b"))
    assert reg.latest("s") == "1.0.0"  # a prerelease is never "latest" by default
    assert reg.fetch("s") == skills
    assert reg.latest("s", include_prerelease=True) == "1.1.0-rc1"
    reg.publish("only", "0.1.0-dev1", skills)
    with pytest.raises(SchemaError, match="only prereleases"):
        reg.fetch("only")


@integrity
def test_registry_lockfile(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path / "reg")
    reg.publish("skills", "1.0.0", fake_skill_set())
    lock = tmp_path / "air.lock"
    locked = reg.lock(["skills"], lock)
    assert locked["skills"].startswith("skills@1.0.0#sha256:")
    reg.publish("skills", "2.0.0", Delegation(delegation_id="d", grantor="a", grantee="b"))
    assert reg.fetch_locked("skills", lock) == fake_skill_set()  # lock wins over latest


def test_registry_rejects_path_like_names(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path)
    with pytest.raises(SchemaError):
        reg.publish("../escape", "1", Delegation(delegation_id="d", grantor="a", grantee="b"))
