"""Pollen Robotics Microduck bridge: robotd JSON-RPC/NDJSON (W3.6, G02). Optional; nothing
in the core imports it. Register explicitly::

    from agentic_robotics.integrations import microduck
    microduck.register()
"""

from ...plugins import register_adapter
from .adapter import SKILLS, MicroduckAdapter, microduck_skill_set
from .client import RobotdClient
from .fake_robotd import FakeRobotd
from .transport import MemoryTransport, memory_pair

__requirements__ = ("REQ03", "G02")


def register() -> None:
    register_adapter(MicroduckAdapter.kind, MicroduckAdapter)


__all__ = ["SKILLS", "FakeRobotd", "MemoryTransport", "MicroduckAdapter", "RobotdClient",
           "memory_pair", "microduck_skill_set", "register"]
