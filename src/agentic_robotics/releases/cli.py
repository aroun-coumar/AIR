"""``air policy``, ``air release``, ``air params`` and ``air dataset`` commands."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..contracts.dataset import Episode, from_lerobot, from_rlds, to_lerobot, to_rlds
from ..contracts.parameters import ParameterSchema, ParameterSet
from ..contracts.policy import ObservationProcessing, PolicyManifest, PolicyRequirements
from ..contracts.release import AssetCard, Release

__requirements__ = ("REQ16", "REQ18", "F04", "F10")




def _project(path: str) -> Any | None:
    """The AIR project at ``path`` if there is one (records go to its ledger)."""
    from ..provenance.project import Project

    try:
        return Project.open(path)
    except Exception:
        return None


def _json(path: str) -> Any:
    return json.loads(Path(path).read_text("utf-8"))


# -- policy --------------------------------------------------------------------------------

def _requirements(path: str) -> PolicyRequirements:
    d = dict(_json(path))
    for k in ("joint_order", "kp", "kd", "inference_backends"):
        if k in d:
            d[k] = tuple(d[k])
    if "observation" in d and d["observation"] is not None:
        o = dict(d["observation"])
        for k in ("scale", "offset"):
            if k in o:
                o[k] = tuple(o[k])
        d["observation"] = ObservationProcessing(**o)
    return PolicyRequirements(**d)


def cmd_policy_check(args: argparse.Namespace) -> int:
    m = PolicyManifest.load(args.sidecar)
    problems: list[str] = []
    if args.model:
        from ..provenance.project import file_digest
        actual = file_digest(Path(args.model))
        if actual != m.model_digest:
            problems.append(f"model digest {actual} != manifest {m.model_digest}")
    if args.requires:
        problems += _requirements(args.requires).mismatches(m)
    print(f"policy   {m.policy_id}@{m.version} ({m.origin.value})")
    print(f"identity {m.identity()}")
    print(f"obs_dim {m.obs_dim}  action_dim {m.action_dim}  steps {m.execution_steps}/"
          f"{m.prediction_steps} every {m.action_interval_s:g} s  max age "
          f"{m.max_action_age_s:g} s  exit {m.exit_mode.value}")
    for p in problems:
        print(f"  REFUSE {p}")
    print("load gate: " + ("REFUSE" if problems else "ok"))
    return 1 if problems else 0


def cmd_policy_publish(args: argparse.Namespace) -> int:
    from ..registry import LocalRegistry

    m = PolicyManifest.load(args.sidecar)
    model = Path(args.model).read_bytes()
    ref = LocalRegistry(args.root).publish_policy(m, model)
    print(ref)
    project = _project(args.project)
    if project is not None:
        from .records import record_policy
        e = record_policy(project, m, model)
        print(f"recorded {e.entity_id}")
    return 0


# -- release -------------------------------------------------------------------------------

def _manager(args: argparse.Namespace) -> Any:
    from .activation import ActivationManager

    return ActivationManager(args.robot, args.slot, clock=time.time, path=args.state,
                             project=_project(args.project))


def _print_state(mgr: Any) -> None:
    s = mgr.state
    print(f"{s.robot_id}/{s.slot}: {s.state.value}  active="
          f"{s.active.ref if s.active else '-'}  pending="
          f"{s.pending.ref if s.pending else '-'}  previous="
          f"{s.previous.ref if s.previous else '-'}  counter={s.security_counter}")


def cmd_release(args: argparse.Namespace) -> int:
    mgr = _manager(args)
    if args.action == "stage":
        mgr.stage(Release.load(args.release))
    elif args.action == "boot":
        mgr.boot()
    elif args.action == "confirm":
        observed: dict[str, bool] = {}
        for item in args.check or []:
            name, _, value = item.partition("=")
            observed[name] = value.lower() in ("pass", "ok", "true", "1")
        if not mgr.confirm(observed):
            _print_state(mgr)
            print("not confirmed: every check the release names must be observed passing",
                  file=sys.stderr)
            return 1
    elif args.action == "revert":
        mgr.revert(args.reason)
    else:
        mgr.check_deadline()
    _print_state(mgr)
    return 0


def cmd_release_targets(args: argparse.Namespace) -> int:
    from ..registry import LocalRegistry
    from .tuf import registry_targets, targets_metadata

    signed = targets_metadata(registry_targets(LocalRegistry(args.root)),
                              version=args.version,
                              expires=time.time() + args.expires_days * 86400)
    print(json.dumps(signed, indent=2, sort_keys=True))
    print("sign this with the targets role keys (releases.tuf.sign)", file=sys.stderr)
    return 0


def cmd_release_card(args: argparse.Namespace) -> int:
    card = AssetCard.load(args.card)
    gaps = card.problems()
    print(f"{card.kind.value} card for {card.asset} ({card.license}): "
          f"{len(gaps)} gap(s)")
    for g in gaps:
        print(f"  gap: {g}")
    project = _project(args.project) if args.record else None
    if project is not None:
        from .records import record_asset_card
        print(f"recorded {record_asset_card(project, card).entity_id}")
    return 1 if gaps and args.strict else 0


# -- params --------------------------------------------------------------------------------

def cmd_params_validate(args: argparse.Namespace) -> int:
    schema = ParameterSchema.load(args.schema)
    pset = ParameterSet.load(args.set)
    problems = schema.validate(pset, allow_unknown=args.allow_unknown)
    for p in problems:
        print(f"  {p}")
    print(f"{args.set}: {len(problems)} problem(s) against {schema.name}")
    return 1 if problems else 0


def cmd_params_diff(args: argparse.Namespace) -> int:
    schema = ParameterSchema.load(args.schema)
    changes = schema.diff(ParameterSet.load(args.old), ParameterSet.load(args.new))
    for c in changes:
        print(f"  {c}")
    reboot = any(c.reboot_required for c in changes)
    print(f"{len(changes)} change(s)" + ("; reboot required" if reboot else ""))
    return 0


def cmd_params_record(args: argparse.Namespace) -> int:
    from ..provenance.project import Project
    from .records import record_parameter_snapshot

    pset = ParameterSet.load(args.set)
    schema = ParameterSchema.load(args.schema) if args.schema else None
    ref = record_parameter_snapshot(Project.open(args.project), pset, schema)
    print(f"recorded {ref.entity_id}")
    return 0


# -- dataset -------------------------------------------------------------------------------

def _load_episode(path: str) -> Episode:
    data = _json(path)
    if isinstance(data, dict) and "steps" in data and "episode_metadata" in data:
        return from_rlds(data)
    if isinstance(data, dict) and "frames" in data and "episode" in data:
        return from_lerobot(data)
    return Episode.from_dict(data)


def cmd_dataset_check(args: argparse.Namespace) -> int:
    ep = _load_episode(args.episode)
    end = "terminated" if ep.terminated else "truncated"
    print(f"{ep.metadata.episode_id}: {len(ep.steps)} steps, {end}")
    return 0


def cmd_dataset_convert(args: argparse.Namespace) -> int:
    ep = _load_episode(args.episode)
    out: dict[str, Any]
    if args.to == "rlds":
        out = to_rlds(ep)
    elif args.to == "lerobot":
        out = to_lerobot(ep)
    else:
        out = ep.to_dict()
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        print(text)
    return 0


def cmd_dataset_record(args: argparse.Namespace) -> int:
    from ..provenance.project import Project
    from .records import record_episode

    ref = record_episode(Project.open(args.project), _load_episode(args.episode))
    print(f"recorded {ref.entity_id}")
    return 0


# -- wiring --------------------------------------------------------------------------------

def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("policy", help="learned policies: load gate checks and publishing")
    ps = p.add_subparsers(dest="action", required=True)
    c = ps.add_parser("check", help="show a policy sidecar and run the load gate")
    c.add_argument("sidecar", help="ar.policy_manifest JSON")
    c.add_argument("--model", help="model file whose digest must match")
    c.add_argument("--requires", help="JSON of the slot's PolicyRequirements fields")
    c.set_defaults(fn=cmd_policy_check)
    pub = ps.add_parser("publish", help="publish sidecar + model bytes to the registry")
    pub.add_argument("sidecar")
    pub.add_argument("model")
    pub.add_argument("--root", default=".ar-registry")
    pub.add_argument("--project", default=".")
    pub.set_defaults(fn=cmd_policy_publish)

    r = sub.add_parser("release", help="trial-then-confirm activation and signed targets")
    rs = r.add_subparsers(dest="action", required=True)
    for name, hlp in (("stage", "stage a release for the next boot"),
                      ("boot", "simulate/record a boot: start trial or revert"),
                      ("confirm", "confirm the trial from observed check results"),
                      ("revert", "revert the trial release"),
                      ("status", "show state (reverts a trial past its deadline)")):
        a = rs.add_parser(name, help=hlp)
        if name == "stage":
            a.add_argument("release", help="ar.release JSON")
        if name == "confirm":
            a.add_argument("--check", action="append", metavar="NAME=pass|fail")
        if name == "revert":
            a.add_argument("--reason", required=True)
        a.add_argument("--robot", required=True)
        a.add_argument("--slot", required=True)
        a.add_argument("--state", required=True, help="activation state file")
        a.add_argument("--project", default=".")
        a.set_defaults(fn=cmd_release)
    t = rs.add_parser("targets", help="TUF targets metadata (unsigned) for registry assets")
    t.add_argument("--root", default=".ar-registry")
    t.add_argument("--version", type=int, default=1)
    t.add_argument("--expires-days", type=int, default=30)
    t.set_defaults(fn=cmd_release_targets)
    cd = rs.add_parser("card", help="check an ar.asset_card and list its gaps")
    cd.add_argument("card")
    cd.add_argument("--strict", action="store_true", help="fail when gaps exist")
    cd.add_argument("--record", action="store_true", help="record it in the project")
    cd.add_argument("--project", default=".")
    cd.set_defaults(fn=cmd_release_card)

    pa = sub.add_parser("params", help="typed parameter sets: validate, diff, record")
    pas = pa.add_subparsers(dest="action", required=True)
    v = pas.add_parser("validate")
    v.add_argument("schema", help="ar.parameter_schema JSON")
    v.add_argument("set", help="ar.parameter_set JSON")
    v.add_argument("--allow-unknown", action="store_true")
    v.set_defaults(fn=cmd_params_validate)
    d = pas.add_parser("diff")
    d.add_argument("schema")
    d.add_argument("old")
    d.add_argument("new")
    d.set_defaults(fn=cmd_params_diff)
    rec = pas.add_parser("record", help="record a parameter snapshot in the project ledger")
    rec.add_argument("set")
    rec.add_argument("--schema")
    rec.add_argument("--project", default=".")
    rec.set_defaults(fn=cmd_params_record)

    ds = sub.add_parser("dataset", help="episodes: check, convert (RLDS/LeRobot), record")
    dss = ds.add_subparsers(dest="action", required=True)
    k = dss.add_parser("check")
    k.add_argument("episode", help="ar.episode, RLDS-like or LeRobot-like JSON")
    k.set_defaults(fn=cmd_dataset_check)
    cv = dss.add_parser("convert")
    cv.add_argument("episode")
    cv.add_argument("--to", choices=["rlds", "lerobot", "air"], required=True)
    cv.add_argument("--out")
    cv.set_defaults(fn=cmd_dataset_convert)
    dr = dss.add_parser("record", help="record an episode in the project ledger")
    dr.add_argument("episode")
    dr.add_argument("--project", default=".")
    dr.set_defaults(fn=cmd_dataset_record)
