"""Dataset episodes and training-run records (ADR 0016, W5.5).

An ``ar.episode/1.0`` is a sequence of steps that keeps **truncation distinct from
termination**, using the RLDS step flags:

- ``is_first``: the first step of the episode;
- ``is_last``: the last step; its observation is the final observation and its action,
  reward and discount are not meaningful;
- ``is_terminal``: the episode ended in a terminal state of the task (success, failure,
  fall). An episode whose last step is not terminal was **truncated** (time limit,
  operator stop, link loss): it must not be learned from as if the task ended.

Conversion helpers produce plain dictionaries shaped like RLDS episodes and like
LeRobotDataset frames, without depending on TensorFlow, TFDS or LeRobot. Both
conversions are lossless: ``from_rlds(to_rlds(e)) == e`` and
``from_lerobot(to_lerobot(e)) == e``. Field names were checked against
google-research/rlds ``README.md`` and huggingface/lerobot ``src/lerobot/utils/
constants.py`` (``DEFAULT_FEATURES``, ``next.reward``, ``next.done``, ``next.truncated``)
on 2026-10-22; see ADR 0016 for the mapping table.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ02", "REQ11", "F05", "F07", "G03")


@dataclass(frozen=True)
class Step:
    observation: dict[str, Any]
    action: tuple[float, ...] = ()
    reward: float | None = None
    discount: float | None = None
    is_first: bool = False
    is_last: bool = False
    is_terminal: bool = False
    timestamp: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EpisodeMetadata:
    episode_id: str
    agent_id: str = ""                   # policy identity, teleoperator or script
    environment_config: dict[str, Any] = field(default_factory=dict)
    task: str = ""
    robot_type: str = ""
    fps: float | None = None
    experiment_id: str = ""
    invalid: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@contract("episode", version="1.0")
class Episode(Contract):
    metadata: EpisodeMetadata
    steps: tuple[Step, ...]

    def __post_init__(self) -> None:
        problems = episode_problems(self.steps)
        if problems:
            raise SchemaError("invalid episode", episode_id=self.metadata.episode_id,
                              problems=problems)

    @property
    def terminated(self) -> bool:
        return self.steps[-1].is_terminal

    @property
    def truncated(self) -> bool:
        return not self.steps[-1].is_terminal


def episode_problems(steps: Iterable[Step]) -> list[str]:
    s = list(steps)
    out: list[str] = []
    if not s:
        return ["episode has no steps"]
    if not s[0].is_first:
        out.append("first step is not marked is_first")
    if not s[-1].is_last:
        out.append("last step is not marked is_last")
    for i, st in enumerate(s):
        if st.is_first and i != 0:
            out.append(f"step {i} is_first but is not the first step")
        if st.is_last and i != len(s) - 1:
            out.append(f"step {i} is_last but is not the last step")
        if st.is_terminal and not st.is_last:
            out.append(f"step {i} is_terminal but not is_last")
        for name in ("reward", "discount", "timestamp"):
            v = getattr(st, name)
            if v is not None and not math.isfinite(v):
                out.append(f"step {i} {name} is not finite")
    ts = [st.timestamp for st in s if st.timestamp is not None]
    if any(b < a for a, b in itertools.pairwise(ts)):
        out.append("timestamps decrease")
    return out


# -- RLDS-like ---------------------------------------------------------------------------

def to_rlds(ep: Episode) -> dict[str, Any]:
    """``{"episode_metadata": {...}, "steps": [{observation, action, reward, discount,
    is_first, is_last, is_terminal, ...}]}``. AIR-only fields use an ``air_`` prefix."""
    md = ep.metadata
    meta: dict[str, Any] = {"episode_id": md.episode_id, "agent_id": md.agent_id,
                            "environment_config": dict(md.environment_config),
                            "experiment_id": md.experiment_id, "invalid": md.invalid,
                            "air_task": md.task, "air_robot_type": md.robot_type,
                            "air_fps": md.fps, "air_extra": dict(md.extra)}
    steps = []
    for st in ep.steps:
        d: dict[str, Any] = {"observation": dict(st.observation), "action": list(st.action),
                             "reward": st.reward, "discount": st.discount,
                             "is_first": st.is_first, "is_last": st.is_last,
                             "is_terminal": st.is_terminal}
        if st.timestamp is not None:
            d["air_timestamp"] = st.timestamp
        if st.metadata:
            d["air_metadata"] = dict(st.metadata)
        steps.append(d)
    return {"episode_metadata": meta, "steps": steps}


def from_rlds(data: Mapping[str, Any]) -> Episode:
    meta = dict(data.get("episode_metadata") or {})
    md = EpisodeMetadata(episode_id=str(meta["episode_id"]),
                         agent_id=str(meta.get("agent_id", "")),
                         environment_config=dict(meta.get("environment_config") or {}),
                         experiment_id=str(meta.get("experiment_id", "")),
                         invalid=bool(meta.get("invalid", False)),
                         task=str(meta.get("air_task", "")),
                         robot_type=str(meta.get("air_robot_type", "")),
                         fps=meta.get("air_fps"), extra=dict(meta.get("air_extra") or {}))
    steps = tuple(Step(observation=dict(s.get("observation") or {}),
                       action=tuple(float(x) for x in s.get("action") or ()),
                       reward=s.get("reward"), discount=s.get("discount"),
                       is_first=bool(s["is_first"]), is_last=bool(s["is_last"]),
                       is_terminal=bool(s.get("is_terminal", False)),
                       timestamp=s.get("air_timestamp"),
                       metadata=dict(s.get("air_metadata") or {}))
                  for s in data["steps"])
    return Episode(metadata=md, steps=steps)


# -- LeRobotDataset-like -----------------------------------------------------------------

LEROBOT_DEFAULT_FEATURES = ("timestamp", "frame_index", "episode_index", "index", "task_index")


def to_lerobot(ep: Episode, *, episode_index: int = 0, task_index: int = 0,
               start_index: int = 0) -> dict[str, Any]:
    """``{"info": {...}, "episode": {...}, "frames": [...]}`` in LeRobotDataset style.

    Each frame has the default columns (timestamp, frame_index, episode_index, index,
    task_index), ``observation.<key>`` for each observation key, ``action``,
    ``next.reward``, ``next.done`` (= is_last) and ``next.truncated`` (= is_last and
    not is_terminal). Values LeRobot has no column for are kept under ``air.*``
    columns so the conversion is lossless. A frame's timestamp is the step timestamp
    when present, otherwise ``frame_index / fps``."""
    md = ep.metadata
    fps = md.fps
    frames = []
    for i, st in enumerate(ep.steps):
        if st.timestamp is not None:
            ts = st.timestamp
        elif fps:
            ts = i / fps
        else:
            ts = float(i)
        f: dict[str, Any] = {"timestamp": ts, "frame_index": i,
                             "episode_index": episode_index, "index": start_index + i,
                             "task_index": task_index, "task": md.task,
                             "action": list(st.action),
                             "next.reward": st.reward if st.reward is not None else 0.0,
                             "next.done": st.is_last,
                             "next.truncated": st.is_last and not st.is_terminal,
                             "air.has_reward": st.reward is not None,
                             "air.discount": st.discount,
                             "air.has_timestamp": st.timestamp is not None,
                             "air.metadata": dict(st.metadata)}
        for k, v in st.observation.items():
            f[f"observation.{k}"] = v
        frames.append(f)
    info = {"fps": fps, "robot_type": md.robot_type or None, "total_episodes": 1,
            "total_frames": len(frames),
            "features": sorted({k for f in frames for k in f})}
    episode = {"episode_index": episode_index, "tasks": [md.task] if md.task else [],
               "length": len(frames),
               "air.episode_metadata": {"episode_id": md.episode_id,
                                        "agent_id": md.agent_id,
                                        "environment_config": dict(md.environment_config),
                                        "experiment_id": md.experiment_id,
                                        "invalid": md.invalid, "extra": dict(md.extra),
                                        "fps": md.fps}}
    return {"info": info, "episode": episode, "frames": frames}


def from_lerobot(data: Mapping[str, Any]) -> Episode:
    ep = data["episode"]
    em = dict(ep.get("air.episode_metadata") or {})
    frames = sorted(data["frames"], key=lambda f: int(f["frame_index"]))
    task = frames[0].get("task", "") if frames else ""
    md = EpisodeMetadata(episode_id=str(em.get("episode_id", f"episode_{ep['episode_index']}")),
                         agent_id=str(em.get("agent_id", "")),
                         environment_config=dict(em.get("environment_config") or {}),
                         experiment_id=str(em.get("experiment_id", "")),
                         invalid=bool(em.get("invalid", False)), extra=dict(em.get("extra")
                                                                            or {}),
                         task=str(task), robot_type=str(data["info"].get("robot_type") or ""),
                         fps=em.get("fps", data["info"].get("fps")))
    steps = []
    n = len(frames)
    for i, f in enumerate(frames):
        done = bool(f.get("next.done", i == n - 1))
        truncated = bool(f.get("next.truncated", False))
        obs = {k.removeprefix("observation."): v for k, v in f.items()
               if k.startswith("observation.")}
        has_reward = bool(f.get("air.has_reward", True))
        steps.append(Step(observation=obs, action=tuple(float(x) for x in f.get("action") or ()),
                          reward=float(f["next.reward"]) if has_reward else None,
                          discount=f.get("air.discount"), is_first=i == 0, is_last=done,
                          is_terminal=done and not truncated,
                          timestamp=float(f["timestamp"]) if f.get("air.has_timestamp", True)
                          else None,
                          metadata=dict(f.get("air.metadata") or {})))
    return Episode(metadata=md, steps=tuple(steps))


# -- training runs -----------------------------------------------------------------------

@dataclass(frozen=True)
class GitState:
    commit: str
    branch: str | None = None
    dirty: bool = False
    diff_digest: str | None = None       # sha256 of `git diff` when dirty


@dataclass(frozen=True)
class NanCheck:
    """Whether non-finite values were checked for, where, and what was found."""

    enabled: bool
    checked: tuple[str, ...] = ()        # e.g. ("loss", "gradients", "actions")
    found_non_finite: bool = False
    first_step: int | None = None
    detail: str = ""


@contract("training_run", version="1.0")
class TrainingRun(Contract):
    run_id: str
    algorithm: str
    git: GitState | None
    nan_check: NanCheck
    seed: int | None = None
    config: dict[str, Any] = field(default_factory=dict)   # full, incl. randomization
    datasets: tuple[str, ...] = ()       # entity ids of the episodes/datasets used
    outputs: tuple[str, ...] = ()        # entity ids, e.g. the policy model digest
    started_at: float | None = None
    ended_at: float | None = None
    steps: int | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)

    @property
    def reproducible(self) -> bool:
        """Only a clean, known commit with a seed can be reproduced from the record."""
        return self.git is not None and not self.git.dirty and self.seed is not None

    @property
    def trustworthy(self) -> bool:
        return self.nan_check.enabled and not self.nan_check.found_non_finite


def first_non_finite(values: Iterable[float]) -> int | None:
    """Index of the first NaN or infinity in ``values``, or None."""
    for i, v in enumerate(values):
        if not math.isfinite(v):
            return i
    return None

