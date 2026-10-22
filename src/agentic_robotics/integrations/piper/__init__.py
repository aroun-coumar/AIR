"""AgileX Piper bridge over CAN (piper_sdk). Optional; ``piper_sdk`` is imported only when
:class:`PiperSdkLayer` is constructed (extra ``[piper]``). Register with :func:`register`."""

from ...plugins import register_adapter
from .adapter import JOINTS, SKILLS, PiperAdapter, piper_skill_set
from .can import CanLayer, PiperSdkLayer, read_interface_state
from .fake_can import FakePiperCan

__requirements__ = ("REQ03",)


def register() -> None:
    register_adapter(PiperAdapter.kind, PiperAdapter)


__all__ = ["JOINTS", "SKILLS", "CanLayer", "FakePiperCan", "PiperAdapter", "PiperSdkLayer",
           "piper_skill_set", "read_interface_state", "register"]
