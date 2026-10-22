"""Ledger records for engineering assets: parameter snapshots, episodes, training runs,
policies and asset cards. Each becomes a content-addressed project entity, so lineage
and impact queries can follow it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..contracts.dataset import Episode, GitState, TrainingRun
from ..contracts.parameters import ParameterSchema, ParameterSet
from ..contracts.policy import PolicyManifest
from ..contracts.release import AssetCard
from ..provenance.model import Activity, ActivityKind, EntityRef
from ..provenance.project import Project, bytes_digest, capture_environment

__requirements__ = ("REQ02", "REQ10", "REQ16", "F04", "F05", "G03")


def git_state(root: str | Path = ".") -> GitState | None:
    """The repository state at ``root`` (None outside git or without a git binary).
    When the tree is dirty, the digest of ``git diff HEAD`` is recorded too."""
    root = Path(root)
    g = capture_environment(root).get("git")
    if not g:
        return None
    diff_digest = None
    git = shutil.which("git")
    if g.get("dirty") and git:
        try:
            out = subprocess.run([git, "diff", "HEAD"], cwd=root, capture_output=True,
                                 timeout=10, check=False)
            diff_digest = bytes_digest(out.stdout)
        except (OSError, subprocess.SubprocessError):
            diff_digest = None
    return GitState(commit=str(g["commit"]), branch=g.get("branch"), dirty=bool(g["dirty"]),
                    diff_digest=diff_digest)


def record_parameter_snapshot(project: Project, pset: ParameterSet,
                              schema: ParameterSchema | None = None) -> EntityRef:
    """Store a robot's parameter set as an entity; with a schema, record its validity."""
    attrs: dict[str, Any] = {"robot_id": pset.robot_id, "schema_name": pset.schema_name,
                             "count": len(pset.values), "source": pset.source}
    if schema is not None:
        attrs["problems"] = schema.validate(pset)
    return project.entity(pset.to_json(sort_keys=True).encode(), kind="parameter_set",
                          name=pset.robot_id, store=True, attributes=attrs)


def record_episode(project: Project, episode: Episode) -> EntityRef:
    md = episode.metadata
    return project.entity(episode.to_json(sort_keys=True).encode(), kind="episode",
                          name=md.episode_id, store=True,
                          attributes={"agent_id": md.agent_id, "steps": len(episode.steps),
                                      "terminated": episode.terminated,
                                      "truncated": episode.truncated, "task": md.task})


def record_policy(project: Project, manifest: PolicyManifest,
                  model: bytes | None = None) -> EntityRef:
    """The policy entity carries its identity; the model blob is stored when given."""
    if model is not None:
        project.entity(model, kind="policy_model", name=manifest.policy_id,
                       version=manifest.version, store=True)
    return project.entity(manifest.to_json(sort_keys=True).encode(), kind="policy",
                          name=manifest.policy_id, version=manifest.version, store=True,
                          attributes={"identity": manifest.identity(),
                                      "model_digest": manifest.model_digest,
                                      "origin": manifest.origin.value,
                                      "robot_model": manifest.robot_model})


def record_training_run(project: Project, run: TrainingRun) -> Activity:
    """A learning activity using the datasets and generating the outputs of ``run``."""
    with project.activity(ActivityKind.LEARNING, f"training {run.run_id} ({run.algorithm})",
                          params={"run_id": run.run_id, "seed": run.seed,
                                  "git": run.git.commit if run.git else None,
                                  "dirty": run.git.dirty if run.git else None}) as act:
        for d in run.datasets:
            act.use(d, kind="dataset")
        act.generate(run.to_json(sort_keys=True).encode(), kind="training_run",
                     name=run.run_id, store=True)
        for k, v in run.metrics.items():
            act.metric(k, v)
        act.metric("nan_check", run.nan_check.enabled)
        act.metric("found_non_finite", run.nan_check.found_non_finite)
        if not run.reproducible:
            act.note("not reproducible from this record (dirty tree, unknown commit or "
                     "no seed)")
        if not run.trustworthy:
            act.fail("non-finite values found or NaN check disabled")
    assert act.result is not None
    return act.result


def record_asset_card(project: Project, card: AssetCard) -> EntityRef:
    return project.entity(card.to_json(sort_keys=True).encode(), kind="asset_card",
                          name=card.asset.split("#")[0], store=True,
                          attributes={"asset": card.asset, "kind": card.kind.value,
                                      "license": card.license, "gaps": card.problems()})
