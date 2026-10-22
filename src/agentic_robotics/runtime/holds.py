"""Token-scoped holds on a robot's own subsystems (a skill's ``occupies``).

A hold is a scoped loan: it is acquired all-or-nothing for one invocation and carries
that invocation's token. Only the same token can release it, so a stale invocation can
never free a live hold (dimos ``CapabilityRegistry``; ros2_control resource manager).
Holds of a command whose outcome is UNRESOLVED are kept until reconciliation settles
it: the subsystem may still be moving.

These are robot-local subsystems ("base", "arm", "gripper"). Shared facility resources
(stations, zones) remain fenced reservations in :mod:`.resources`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..core.errors import AdmissionRejected

__requirements__ = ("REQ05", "REQ06", "REQ14")


@dataclass(frozen=True)
class Hold:
    robot_id: str
    subsystem: str
    token: str
    command_id: str
    acquired_at: float


class HoldRegistry:
    def __init__(self) -> None:
        self._holds: dict[tuple[str, str], Hold] = {}

    def holder(self, robot_id: str, subsystem: str) -> Hold | None:
        return self._holds.get((robot_id, subsystem))

    def held(self, robot_id: str | None = None) -> list[Hold]:
        return [h for h in self._holds.values() if robot_id is None or h.robot_id == robot_id]

    def conflicts(self, robot_id: str, subsystems: Iterable[str],
                  token: str | None = None) -> list[Hold]:
        return [h for s in subsystems
                if (h := self._holds.get((robot_id, s))) is not None and h.token != token]

    def acquire(self, robot_id: str, subsystems: Iterable[str], token: str, command_id: str,
                now: float) -> tuple[Hold, ...]:
        """Acquire every subsystem or none. A conflict names the current holder."""
        wanted = tuple(dict.fromkeys(subsystems))
        taken = self.conflicts(robot_id, wanted, token)
        if taken:
            h = taken[0]
            raise AdmissionRejected("subsystem_occupied", "subsystem is held by another command",
                                    robot_id=robot_id, subsystem=h.subsystem,
                                    holder=h.command_id)
        out = []
        for s in wanted:
            hold = self._holds.get((robot_id, s)) or Hold(robot_id, s, token, command_id, now)
            self._holds[(robot_id, s)] = hold
            out.append(hold)
        return tuple(out)

    def release(self, robot_id: str, token: str) -> tuple[Hold, ...]:
        """Release the holds carrying ``token``. Any other token releases nothing."""
        mine = [k for k, h in self._holds.items() if h.robot_id == robot_id and h.token == token]
        return tuple(self._holds.pop(k) for k in mine)


__all__ = ["Hold", "HoldRegistry"]
