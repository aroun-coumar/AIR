"""``air doctor``: report environment and tool availability. Never raises.

Every probe is isolated: a failing probe becomes a ``fail``/``missing`` line, never an
exception. Optional integrations are *reported*, not required (AIR needs no hardware and
no third-party package to run its core).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__requirements__ = ("REQ18",)

#: (import name, extra or install hint, what it enables)
OPTIONAL_MODULES = (
    ("anthropic", "[anthropic]", "Claude model provider"),
    ("websockets", "[openpi]", "openpi websocket policy transport"),
    ("msgpack", "-", "cross-check of the stdlib msgpack codec"),
    ("piper_sdk", "[piper]", "AgileX Piper CAN layer"),
    ("can", "[piper]", "python-can (used by piper_sdk)"),
    ("unitree_sdk2py", "git install", "Unitree DDS channel layer"),
    ("cyclonedds", "with unitree_sdk2py", "DDS for Unitree"),
    ("rclpy", "source a ROS 2 distro", "ROS 2 action layer"),
    ("paho", "[vda5050]", "MQTT link for VDA 5050"),
    ("dora", "[dora]", "dora-rs operator runtime"),
    ("pyarrow", "with dora", "Arrow values in dora events"),
    ("yaml", "-", "reading YAML dataflows"),
    ("jsonschema", "[dev]", "JSON Schema validation in tests"),
    ("lerobot", "[lerobot]", "LeRobot policies (contracts work without it)"),
)
TOOLS = (("git", "version control"), ("uv", "environment manager"),
         ("docker", "container sandboxes"), ("ros2", "ROS 2 CLI"), ("dora", "dora CLI"),
         ("bwrap", "Linux sandbox"), ("kicad-cli", "electronics checks"),
         ("ip", "inspect CAN interfaces (read-only)"))


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    status: str            # ok | missing | warn | fail
    detail: str = ""


def _probe(group: str, name: str, fn: Callable[[], tuple[str, str]]) -> Check:
    try:
        status, detail = fn()
    except BaseException as exc:
        status, detail = "fail", f"{type(exc).__name__}: {exc}"
    return Check(group, name, status, detail)


def _module(name: str) -> tuple[str, str]:
    return ("ok", "importable") if importlib.util.find_spec(name) else ("missing", "")


def run_checks(root: str | Path = ".") -> list[Check]:
    base = Path(root)
    out: list[Check] = []

    def python() -> tuple[str, str]:
        ok = sys.version_info >= (3, 11)
        return ("ok" if ok else "fail",
                f"{platform.python_version()} ({sys.executable}); needs >= 3.11")

    def package() -> tuple[str, str]:
        import agentic_robotics
        return "ok", f"{agentic_robotics.__version__} at {Path(agentic_robotics.__file__).parent}"

    def venv() -> tuple[str, str]:
        inside = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        return ("ok" if inside else "warn",
                sys.prefix if inside else "not in a virtual environment (CLAUDE.md: .venv)")

    out += [_probe("environment", "python", python),
            _probe("environment", "agentic_robotics", package),
            _probe("environment", "virtualenv", venv),
            _probe("environment", "platform", lambda: ("ok", platform.platform()))]

    def contracts() -> tuple[str, str]:
        from .core.schema import registered_contracts
        return "ok", f"{len(registered_contracts())} contracts registered"

    def schemas() -> tuple[str, str]:
        d = base / "schemas"
        if not d.is_dir():
            return "warn", f"no {d} directory (run from the repository root)"
        return "ok", f"{sum(1 for _ in d.rglob('*.json'))} published schema files"

    def adapters() -> tuple[str, str]:
        from .plugins import available_adapters
        return "ok", ", ".join(sorted(available_adapters())) or "none"

    def project() -> tuple[str, str]:
        p = base / ".air" / "project.json"
        return ("ok", str(p.parent)) if p.is_file() else ("warn", "no .air project here")

    out += [_probe("air", "contracts", contracts), _probe("air", "schemas", schemas),
            _probe("air", "adapters", adapters), _probe("air", "project", project)]

    for mod, hint, what in OPTIONAL_MODULES:
        c = _probe("optional", mod, lambda m=mod: _module(m))  # type: ignore[misc]
        out.append(Check(c.group, c.name, c.status,
                         f"{what}; {c.detail or 'install: ' + hint}"))
    for tool, what in TOOLS:
        c = _probe("tools", tool, lambda t=tool: (  # type: ignore[misc]
            ("ok", shutil.which(t) or "") if shutil.which(t) else ("missing", "")))
        out.append(Check(c.group, c.name, c.status, f"{what}; {c.detail}".rstrip("; ")))
    return out


def report(checks: list[Check]) -> str:
    lines, group = [], ""
    for c in checks:
        if c.group != group:
            group = c.group
            lines.append(f"{group}:")
        lines.append(f"  [{c.status:<7}] {c.name:<18} {c.detail}")
    fails = sum(c.status == "fail" for c in checks)
    lines.append(f"{fails} failing check(s); missing optional items are informational")
    return "\n".join(lines)


def _doctor(args: argparse.Namespace) -> int:
    try:
        checks = run_checks(args.root)
        if args.json:
            print(json.dumps([asdict(c) for c in checks], indent=2))
        else:
            print(report(checks))
        return 1 if args.strict and any(c.status == "fail" for c in checks) else 0
    except BaseException as exc:
        print(f"air doctor: internal error {exc!r}", file=sys.stderr)
        return 0


def add_commands(sub: Any) -> None:
    p = sub.add_parser("doctor", help="report environment and tool availability (never raises)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=".")
    p.add_argument("--strict", action="store_true", help="exit 1 when a check fails")
    p.set_defaults(fn=_doctor)


__all__ = ["Check", "add_commands", "report", "run_checks"]
