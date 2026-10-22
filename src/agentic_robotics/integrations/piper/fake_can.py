"""A fake Piper behind :class:`~.can.CanLayer`, in SDK units (ADR 0019).

Joints move toward the last ``JointCtrl`` target only while the interface is up, the arm
is enabled and in CAN command mode, at a bounded rate per feedback read (time is driven by
reads, so tests are deterministic). The interface starts ``down`` unless told otherwise:
activation is the operator's job. Faults: ``set_interface``, ``collide()``.
"""

from __future__ import annotations

from typing import Any

from . import can as c

__requirements__ = ("REQ03", "REQ18")


class FakePiperCan:
    def __init__(self, *, operstate: str = "up", bitrate: int = 1_000_000,
                 step_mdeg: int = 4000) -> None:
        self.operstate = operstate
        self.bitrate = bitrate
        self.step = step_mdeg
        self.connected = False
        self.enabled = [False] * 6
        self.ctrl_mode = 0x00
        self.status = c.ARM_STATUS_NORMAL
        self.q = [0, 1000, -1000, 0, 0, 0]
        self.target = list(self.q)
        self.gripper = 0
        self.gripper_target = 0
        self.t = 0.0
        self.log: list[tuple[str, Any]] = []

    def set_interface(self, operstate: str) -> None:
        self.operstate = operstate

    def collide(self) -> None:
        self.status = 0x07

    def _bus_ok(self) -> bool:
        return self.operstate == "up" and self.connected

    # -- CanLayer -------------------------------------------------------------------
    def interface_state(self) -> dict[str, Any]:
        return {"name": "can0", "operstate": self.operstate, "bitrate": self.bitrate}

    def connect(self) -> None:
        if self.operstate != "up":
            raise ConnectionError("['can0' CreateCanBus ERROR]")
        self.connected = True

    def enable_arm(self) -> None:
        self.log.append(("enable", None))
        if self._bus_ok():
            self.enabled = [True] * 6

    def motion_ctrl_2(self, ctrl_mode: int, move_mode: int, speed_pct: int) -> None:
        self.log.append(("motion_ctrl_2", (ctrl_mode, move_mode, speed_pct)))
        if self._bus_ok():
            self.ctrl_mode = ctrl_mode

    def joint_ctrl(self, mdeg: list[int]) -> None:
        self.log.append(("joint_ctrl", list(mdeg)))
        if self._bus_ok():
            self.target = [int(v) for v in mdeg]

    def gripper_ctrl(self, stroke_um: int, effort_mnm: int, code: int) -> None:
        self.log.append(("gripper_ctrl", (stroke_um, effort_mnm, code)))
        if self._bus_ok() and code == c.GRIPPER_ENABLE:
            self.gripper_target = int(stroke_um)

    def emergency_stop(self, code: int) -> None:
        self.log.append(("emergency_stop", code))
        if not self._bus_ok():
            return
        if code == c.ESTOP:
            self.status = c.ARM_STATUS_ESTOP
            self.target = list(self.q)
        elif code == c.RESUME:
            self.status = c.ARM_STATUS_NORMAL

    def _advance(self) -> None:
        self.t += 0.01
        if not (self._bus_ok() and all(self.enabled) and self.ctrl_mode == c.CTRL_MODE_CAN
                and self.status == c.ARM_STATUS_NORMAL):
            return
        for i in range(6):
            d = self.target[i] - self.q[i]
            self.q[i] += max(-self.step, min(self.step, d))
        d = self.gripper_target - self.gripper
        self.gripper += max(-5000, min(5000, d))

    def joint_feedback(self) -> tuple[float, list[int]]:
        if not self._bus_ok():
            return self.t, list(self.q)        # the SDK keeps returning the last frame
        self._advance()
        return self.t, list(self.q)

    def gripper_feedback(self) -> tuple[float, int, int]:
        return self.t, self.gripper, 500

    def arm_status(self) -> dict[str, Any]:
        moving = self.q != self.target
        return {"ctrl_mode": self.ctrl_mode, "arm_status": self.status,
                "motion_status": 0x01 if moving else c.MOTION_REACHED}

    def enable_status(self) -> list[bool]:
        return list(self.enabled)

    def disconnect(self) -> None:
        self.connected = False


__all__ = ["FakePiperCan"]
