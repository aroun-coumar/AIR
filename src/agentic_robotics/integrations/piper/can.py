"""The injectable CAN layer for the AgileX Piper arm (ADR 0019).

Mirrors the subset of ``piper_sdk`` ``C_PiperInterface_V2`` AIR uses (agilexrobotics/
piper_sdk HEAD b847e53, MIT), in SDK units: joint angles in 0.001 degrees, gripper
stroke in 0.001 mm, gripper effort in 0.001 N*m. CAN ids per the SDK docstrings:
0x151 MotionCtrl_2, 0x155-0x157 JointCtrl, 0x159 GripperCtrl, 0x150 MotionCtrl_1
(emergency stop / resume), 0x471 EnableArm, 0x2A1 arm status feedback.

**CAN activation is an operator precondition, not something AIR does.** The SDK ships
``can_activate.sh`` (``ip link`` with sudo); AIR only *reads* the interface state
(``/sys/class/net/<can>/operstate`` and the bitrate) and reports it as health.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Protocol

from ...core.errors import UnsupportedOperation

__requirements__ = ("REQ03",)

#: Joint limits from ``JointCtrl``'s docstring, radians.
JOINT_LIMITS_RAD = ((-2.6179, 2.6179), (0.0, 3.14), (-2.967, 0.0), (-1.745, 1.745),
                    (-1.22, 1.22), (-2.09439, 2.09439))
CTRL_MODE_CAN = 0x01
MOVE_J = 0x01
ENABLE_ALL = 7
ENABLE_FLAG = 0x02
ESTOP = 0x01
RESUME = 0x02
GRIPPER_ENABLE = 0x01
ARM_STATUS_NORMAL = 0x00
ARM_STATUS_ESTOP = 0x01
MOTION_REACHED = 0x00


class CanLayer(Protocol):
    def interface_state(self) -> dict[str, Any]:
        """``{"name", "operstate", "bitrate"}`` of the CAN interface (read-only)."""
        ...

    def connect(self) -> None: ...

    def enable_arm(self) -> None: ...

    def motion_ctrl_2(self, ctrl_mode: int, move_mode: int, speed_pct: int) -> None: ...

    def joint_ctrl(self, mdeg: list[int]) -> None: ...

    def gripper_ctrl(self, stroke_um: int, effort_mnm: int, code: int) -> None: ...

    def emergency_stop(self, code: int) -> None: ...

    def joint_feedback(self) -> tuple[float, list[int]]:
        """``(time_stamp, [joint_1..joint_6 in 0.001 deg])`` from ``GetArmJointMsgs``."""
        ...

    def gripper_feedback(self) -> tuple[float, int, int]:
        """``(time_stamp, grippers_angle 0.001 mm, grippers_effort 0.001 N*m)``."""
        ...

    def arm_status(self) -> dict[str, Any]:
        """``GetArmStatus().arm_status`` fields: ctrl_mode, arm_status, motion_status..."""
        ...

    def enable_status(self) -> list[bool]: ...

    def disconnect(self) -> None: ...


def read_interface_state(name: str) -> dict[str, Any]:
    """Read-only interface facts from sysfs (Linux). Never activates anything."""
    base = Path("/sys/class/net") / name
    try:
        operstate = (base / "operstate").read_text().strip()
    except OSError:
        return {"name": name, "operstate": "absent", "bitrate": None}
    bitrate = None
    with contextlib.suppress(OSError, ValueError):
        bitrate = int((base / "can_bittiming" / "bitrate").read_text().strip())
    return {"name": name, "operstate": operstate, "bitrate": bitrate}


class PiperSdkLayer:  # pragma: no cover - needs piper_sdk, python-can and a CAN interface
    def __init__(self, can_name: str = "can0") -> None:
        try:
            from piper_sdk import C_PiperInterface_V2  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:
            raise UnsupportedOperation("piper_sdk is not installed (pip install "
                                       "'agentic-robotics[piper]')", error=str(exc)) from exc
        self.can_name = can_name
        self._arm = C_PiperInterface_V2(can_name)

    def interface_state(self) -> dict[str, Any]:
        return read_interface_state(self.can_name)

    def connect(self) -> None:
        self._arm.ConnectPort()

    def enable_arm(self) -> None:
        self._arm.EnableArm(ENABLE_ALL, ENABLE_FLAG)

    def motion_ctrl_2(self, ctrl_mode: int, move_mode: int, speed_pct: int) -> None:
        self._arm.MotionCtrl_2(ctrl_mode, move_mode, speed_pct, 0x00)

    def joint_ctrl(self, mdeg: list[int]) -> None:
        self._arm.JointCtrl(*mdeg)

    def gripper_ctrl(self, stroke_um: int, effort_mnm: int, code: int) -> None:
        self._arm.GripperCtrl(stroke_um, effort_mnm, code, 0)

    def emergency_stop(self, code: int) -> None:
        self._arm.EmergencyStop(code)

    def joint_feedback(self) -> tuple[float, list[int]]:
        m = self._arm.GetArmJointMsgs()
        js = m.joint_state
        return float(m.time_stamp), [js.joint_1, js.joint_2, js.joint_3, js.joint_4,
                                     js.joint_5, js.joint_6]

    def gripper_feedback(self) -> tuple[float, int, int]:
        m = self._arm.GetArmGripperMsgs()
        g = m.gripper_state
        return float(m.time_stamp), int(g.grippers_angle), int(g.grippers_effort)

    def arm_status(self) -> dict[str, Any]:
        s = self._arm.GetArmStatus().arm_status
        return {"ctrl_mode": int(s.ctrl_mode), "arm_status": int(s.arm_status),
                "motion_status": int(s.motion_status)}

    def enable_status(self) -> list[bool]:
        return [bool(x) for x in self._arm.GetArmEnableStatus()]

    def disconnect(self) -> None:
        self._arm.DisconnectPort()


__all__ = ["JOINT_LIMITS_RAD", "CanLayer", "PiperSdkLayer", "read_interface_state"]
