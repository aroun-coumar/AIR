"""VDA 5050 v2/v3 bridge over an injected MQTT publisher. Optional; paho-mqtt is imported
only by :class:`PahoLink` (extra ``[vda5050]``). Register with :func:`register`."""

from ...plugins import register_adapter
from . import messages
from .adapter import SKILLS, MqttPublisher, PahoLink, VdaAdapter, vda_skill_set
from .fake_vehicle import FakeVehicle

__requirements__ = ("REQ03", "REQ06")


def register() -> None:
    register_adapter(VdaAdapter.kind, VdaAdapter)


__all__ = ["SKILLS", "FakeVehicle", "MqttPublisher", "PahoLink", "VdaAdapter", "messages",
           "register", "vda_skill_set"]
