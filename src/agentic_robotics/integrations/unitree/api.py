"""Unitree SDK2 channel-model constants, verified from ``unitreerobotics/unitree_sdk2_python``
(HEAD 814556d; BSD-3-Clause; see ADR 0019 for the files read).

- RPC services ride DDS: ``sport`` (API version ``1.0.0.1``) and ``motion_switcher``.
  Each call carries an integer api id and a JSON string parameter; ``Move`` is
  fire-and-forget (``_CallNoReply``), the posture calls are answered with a code.
- Low level: ``rt/lowstate`` (``unitree_go.msg.dds_.LowState_``) and ``rt/lowcmd``
  (``LowCmd_``, 20 ``MotorCmd_`` slots; head ``0xFE 0xEF``, ``level_flag`` ``0xFF``,
  a CRC over the packed message; the stand example writes every 2 ms). Low-level
  control requires the sport service to be released through ``motion_switcher``.
- High-level state: ``SportModeState_``; the topic ``rt/lf/sportmodestate`` is the one
  the A2/AS2 examples use. It is configurable because the Go2 examples do not name it.
"""

from __future__ import annotations

__requirements__ = ("REQ03",)

SPORT_SERVICE = "sport"
SPORT_API_VERSION = "1.0.0.1"


class Sport:
    DAMP = 1001
    BALANCE_STAND = 1002
    STOP_MOVE = 1003
    STAND_UP = 1004
    STAND_DOWN = 1005
    RECOVERY_STAND = 1006
    EULER = 1007
    MOVE = 1008
    SIT = 1009
    RISE_SIT = 1010
    SPEED_LEVEL = 1015


MOTION_SWITCHER_SERVICE = "motion_switcher"
MOTION_SWITCHER_API_VERSION = "1.0.0.1"


class MotionSwitcher:
    CHECK_MODE = 1001
    SELECT_MODE = 1002
    RELEASE_MODE = 1003


class RpcCode:
    OK = 0
    CLIENT_SEND = 3102
    CLIENT_API_NOT_REG = 3103
    CLIENT_API_TIMEOUT = 3104
    CLIENT_LEASE_INVALID = 3107
    SERVER_INTERNAL = 3202
    SERVER_API_NOT_IMPL = 3203
    SERVER_API_PARAMETER = 3204
    SERVER_LEASE_DENIED = 3205
    SPORT_SERVER_OVERTIME = 4201
    SPORT_SERVER_NOT_INIT = 4202


#: Codes after which the physical outcome of a call is unknown (the request may have
#: reached the robot): never retried blindly.
UNKNOWN_OUTCOME = frozenset({RpcCode.CLIENT_API_TIMEOUT, RpcCode.SPORT_SERVER_OVERTIME})

TOPIC_LOWSTATE = "rt/lowstate"
TOPIC_LOWCMD = "rt/lowcmd"
TOPIC_SPORT_STATE = "rt/lf/sportmodestate"

LOWCMD_HEAD = (0xFE, 0xEF)
LOWLEVEL = 0xFF
MOTOR_MODE_PMSM = 0x01
MOTOR_SLOTS = 20
LEG_JOINTS = ("FR_0", "FR_1", "FR_2", "FL_0", "FL_1", "FL_2",
              "RR_0", "RR_1", "RR_2", "RL_0", "RL_1", "RL_2")
#: "No position/velocity control" sentinels from the Go2 low-level example.
POS_STOP_F = 2.146e9
VEL_STOP_F = 16000.0

__all__ = ["LEG_JOINTS", "LOWCMD_HEAD", "LOWLEVEL", "MOTION_SWITCHER_API_VERSION",
           "MOTION_SWITCHER_SERVICE", "MOTOR_MODE_PMSM", "MOTOR_SLOTS", "POS_STOP_F",
           "SPORT_API_VERSION", "SPORT_SERVICE", "TOPIC_LOWCMD", "TOPIC_LOWSTATE",
           "TOPIC_SPORT_STATE", "UNKNOWN_OUTCOME", "VEL_STOP_F", "MotionSwitcher", "RpcCode",
           "Sport"]
