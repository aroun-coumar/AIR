"""Shared fake world for multi-robot reference scenarios: stations, parts and who
physically occupies each station. Several fake adapters naming the same world (manifest
``config.world``) observe and change the same state, the way robots share a facility."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from ..core.errors import FrameworkError

__requirements__ = ("REQ03", "REQ06")


class FakeWorld:
    _worlds: ClassVar[dict[str, FakeWorld]] = {}

    def __init__(self, name: str) -> None:
        self.name = name
        self.station_part: dict[str, str] = {}
        self.occupant: dict[str, str] = {}
        self.part_location: dict[str, str] = {}

    @classmethod
    def get(cls, name: str) -> FakeWorld:
        if name not in cls._worlds:
            cls._worlds[name] = cls(name)
        return cls._worlds[name]

    @classmethod
    def reset(cls, name: str | None = None) -> None:
        if name is None:
            cls._worlds.clear()
        else:
            cls._worlds.pop(name, None)

    def add_stations(self, names: Mapping[str, object] | list[str]) -> None:
        for s in names:
            self.station_part.setdefault(s, "")
            self.occupant.setdefault(s, "")

    def place_part(self, part: str, location: str) -> None:
        if part in self.part_location:
            return
        self.part_location[part] = location
        if location in self.station_part:
            self.station_part[location] = part

    def move_part(self, part: str, src: str, dst: str) -> None:
        if self.part_location.get(part) != src:
            raise FrameworkError("part is not where expected", part=part, expected=src,
                                 actual=self.part_location.get(part))
        if src in self.station_part and self.station_part[src] == part:
            self.station_part[src] = ""
        if dst in self.station_part:
            if self.station_part[dst]:
                raise FrameworkError("station already holds a part", station=dst)
            self.station_part[dst] = part
        self.part_location[part] = dst

    def occupy(self, station: str, robot: str) -> None:
        self.add_stations([station])
        current = self.occupant[station]
        if current and current != robot:
            raise FrameworkError("collision: station physically occupied", station=station,
                                 occupant=current, robot=robot)
        self.occupant[station] = robot

    def vacate(self, station: str, robot: str) -> None:
        if self.occupant.get(station) == robot:
            self.occupant[station] = ""
