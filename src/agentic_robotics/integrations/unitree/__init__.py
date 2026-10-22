"""Unitree bridge over the unitree_sdk2 DDS channel model. Optional; no cyclonedds import
unless :class:`SdkChannelLayer` is constructed. Register explicitly with :func:`register`."""

from ...plugins import register_adapter
from .adapter import POSTURES, SKILLS, UnitreeAdapter, UnknownOutcome, unitree_skill_set
from .channels import ChannelLayer, SdkChannelLayer
from .fake_channels import FakeUnitree

__requirements__ = ("REQ03",)


def register() -> None:
    register_adapter(UnitreeAdapter.kind, UnitreeAdapter)


__all__ = ["POSTURES", "SKILLS", "ChannelLayer", "FakeUnitree", "SdkChannelLayer",
           "UnitreeAdapter", "UnknownOutcome", "register", "unitree_skill_set"]
