"""``air`` / ``agentic-robotics`` command-line tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .core.errors import FrameworkError
from .core.schema import load_any, registered_contracts

__requirements__ = ("REQ18",)


def _validate(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.files:
        try:
            doc = load_any(json.loads(Path(path).read_text("utf-8")))
            print(f"ok    {path}  ({doc.schema_id()})")
        except (OSError, json.JSONDecodeError, FrameworkError) as exc:
            failed += 1
            print(f"FAIL  {path}  {exc}")
    return 1 if failed else 0


def _schemas(_: argparse.Namespace) -> int:
    for cls in sorted(registered_contracts().values(), key=lambda c: c.SCHEMA):
        print(f"{cls.schema_id():40s} {cls.__module__}.{cls.__qualname__}")
    return 0


def _schema(args: argparse.Namespace) -> int:
    from .core.jsonschema import check_schemas, export_schemas

    if args.action == "export":
        written = export_schemas(args.dir)
        print(f"wrote {len(written)} schemas to {args.dir}")
        return 0
    problems = check_schemas(args.dir)
    for p in problems:
        print(f"SCHEMA FAIL: {p}", file=sys.stderr)
    if not problems:
        print("schemas up to date and backwards compatible")
    return 1 if problems else 0


def _adapters(_: argparse.Namespace) -> int:
    from .plugins import available_adapters
    for kind in sorted(available_adapters()):
        print(kind)
    return 0


def _conformance(args: argparse.Namespace) -> int:
    from .conformance import check_adapter
    from .robot import Robot
    from .skills import SkillRegistry

    skills = SkillRegistry.load(args.skills)
    robot = Robot.from_manifest(args.manifest)
    report = asyncio.run(check_adapter(robot, skills))
    print(report.summary())
    return 0 if report.passed else 1


def _quickstart(_: argparse.Namespace) -> int:
    from .quickstart import run
    return 0 if asyncio.run(run()) else 1


def _scenario(args: argparse.Namespace) -> int:
    from .provenance.project import Project
    from .scenarios import run_handoff

    project = Project.open(args.project) if args.record else None
    return 0 if asyncio.run(run_handoff(project=project)).verified else 1


def _registry(args: argparse.Namespace) -> int:
    from .registry import LocalRegistry
    reg = LocalRegistry(args.root)
    if args.action == "publish":
        doc = load_any(json.loads(Path(args.file).read_text("utf-8")))
        print(reg.publish(args.name, args.version, doc))
    else:
        for ref in reg.list():
            print(f"{ref}  {ref.schema}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=Path(sys.argv[0]).stem if sys.argv[0] else "air",
                                description="AIR: Agentic Robotics Framework tools")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate contract documents ($schema-tagged JSON)")
    v.add_argument("files", nargs="+")
    v.set_defaults(fn=_validate)

    sub.add_parser("schemas", help="list registered contract schemas").set_defaults(fn=_schemas)

    js = sub.add_parser("schema", help="JSON Schema export and drift/compatibility check")
    js.add_argument("action", choices=["export", "check"])
    js.add_argument("--dir", default="schemas")
    js.set_defaults(fn=_schema)
    sub.add_parser("adapters", help="list discoverable robot adapters").set_defaults(fn=_adapters)

    c = sub.add_parser("conformance", help="run the adapter conformance suite")
    c.add_argument("manifest", help="robot ComponentManifest JSON")
    c.add_argument("--skills", required=True, help="SkillSet JSON")
    c.set_defaults(fn=_conformance)

    sub.add_parser("quickstart", help="run the fake-device quickstart").set_defaults(
        fn=_quickstart)

    sc = sub.add_parser("scenario", help="run a reference scenario")
    sc.add_argument("name", choices=["handoff"])
    sc.add_argument("--record", action="store_true",
                    help="record evidence into the AIR project ledger")
    sc.add_argument("--project", default=".")
    sc.set_defaults(fn=_scenario)

    r = sub.add_parser("registry", help="local asset registry")
    r.add_argument("--root", default=".ar-registry")
    rs = r.add_subparsers(dest="action", required=True)
    rp = rs.add_parser("publish")
    rp.add_argument("name")
    rp.add_argument("version")
    rp.add_argument("file")
    rs.add_parser("list")
    r.set_defaults(fn=_registry)

    from .provenance.cli import add_commands
    add_commands(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except FrameworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
