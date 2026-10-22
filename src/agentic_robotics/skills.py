"""Skill registry: discovery and lookup of declared skills."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .contracts.skill import SkillSet, SkillSpec
from .core.errors import SchemaError, UnsupportedOperation

__requirements__ = ("REQ05", "REQ18")


class SkillRegistry:
    def __init__(self, skills: Iterable[SkillSpec] = ()) -> None:
        self._skills: dict[str, SkillSpec] = {}
        for s in skills:
            self.register(s)

    @classmethod
    def load(cls, path: str | Path) -> SkillRegistry:
        return cls(SkillSet.load(path).skills)

    def register(self, spec: SkillSpec) -> None:
        existing = self._skills.get(spec.skill_id)
        if existing is not None and existing != spec:
            raise SchemaError("a different skill with this id is already registered",
                              skill_id=spec.skill_id)
        self._skills[spec.skill_id] = spec

    def get(self, skill_id: str) -> SkillSpec:
        try:
            return self._skills[skill_id]
        except KeyError:
            raise UnsupportedOperation("unknown skill", skill_id=skill_id,
                                       known=sorted(self._skills)) from None

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._skills

    def __iter__(self) -> Iterator[SkillSpec]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def tool_descriptions(self, only: Iterable[str] | None = None) -> list[dict[str, Any]]:
        allowed = set(only) if only is not None else None
        return [s.tool_description() for s in self
                if allowed is None or s.skill_id in allowed]
