"""The injectable channel layer between the Unitree adapter and DDS.

Messages cross this seam as plain dicts that mirror the IDL field names
(``LowState_.motor_state[i].q``, ``SportModeState_.position``...), so the adapter and its
tests never import cyclonedds. :class:`SdkChannelLayer` is the production binding; it
imports ``unitree_sdk2py`` lazily and is not exercised by the offline test-suite (the
SDK is not on PyPI; install it from its repository).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any, Protocol

from ...core.errors import UnsupportedOperation
from . import api

__requirements__ = ("REQ03",)

Handler = Callable[[dict[str, Any]], None]


class ChannelLayer(Protocol):
    def rpc(self, service: str, api_id: int, parameter: dict[str, Any], *, reply: bool,
            timeout_s: float) -> tuple[int, Any]:
        """Call ``api_id`` on ``service``. Returns ``(code, data)``; ``data`` is the decoded
        JSON reply (or ``None``). ``reply=False`` is the SDK's ``_CallNoReply``."""
        ...

    def subscribe(self, topic: str, handler: Handler) -> None: ...

    def publish(self, topic: str, sample: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class SdkChannelLayer:  # pragma: no cover - needs unitree_sdk2py and a DDS network
    """Binding to ``unitree_sdk2py``. Constructed only when the adapter starts."""

    def __init__(self, network_interface: str | None = None, domain_id: int = 0) -> None:
        try:
            from unitree_sdk2py.core.channel import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
                ChannelFactoryInitialize,
            )
        except ImportError as exc:
            raise UnsupportedOperation(
                "unitree_sdk2py is not installed; install it from "
                "github.com/unitreerobotics/unitree_sdk2_python", error=str(exc)) from exc
        if network_interface:
            ChannelFactoryInitialize(domain_id, network_interface)
        else:
            ChannelFactoryInitialize(domain_id)
        self._clients: dict[str, Any] = {}
        self._subs: list[Any] = []
        self._pubs: dict[str, Any] = {}

    def _client(self, service: str, timeout_s: float) -> Any:
        from unitree_sdk2py.rpc.client import Client  # type: ignore[import-not-found,import-untyped,unused-ignore]

        if service not in self._clients:
            client = Client(service, False)
            client._SetApiVerson(api.SPORT_API_VERSION if service == api.SPORT_SERVICE
                                 else api.MOTION_SWITCHER_API_VERSION)
            ids = (range(api.Sport.DAMP, api.Sport.SPEED_LEVEL + 1)
                   if service == api.SPORT_SERVICE else range(1001, 1006))
            for i in ids:
                client._RegistApi(i, 0)
            self._clients[service] = client
        client = self._clients[service]
        client.SetTimeout(timeout_s)
        return client

    def rpc(self, service: str, api_id: int, parameter: dict[str, Any], *, reply: bool,
            timeout_s: float) -> tuple[int, Any]:
        client = self._client(service, timeout_s)
        text = json.dumps(parameter)
        if not reply:
            return int(client._CallNoReply(api_id, text)), None
        code, data = client._Call(api_id, text)
        return int(code), (json.loads(data) if code == 0 and data else None)

    def subscribe(self, topic: str, handler: Handler) -> None:
        from unitree_sdk2py.core.channel import ChannelSubscriber  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            LowState_,
            SportModeState_,
        )

        kind = LowState_ if topic == api.TOPIC_LOWSTATE else SportModeState_
        sub = ChannelSubscriber(topic, kind)
        sub.Init(lambda msg: handler(dataclasses.asdict(msg)), 10)
        self._subs.append(sub)

    def publish(self, topic: str, sample: dict[str, Any]) -> None:
        from unitree_sdk2py.core.channel import ChannelPublisher  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from unitree_sdk2py.idl.default import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            unitree_go_msg_dds__LowCmd_,
        )
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from unitree_sdk2py.utils.crc import CRC  # type: ignore[import-not-found,import-untyped,unused-ignore]

        if topic != api.TOPIC_LOWCMD:
            raise UnsupportedOperation("only rt/lowcmd is published", topic=topic)
        if topic not in self._pubs:
            pub = ChannelPublisher(topic, LowCmd_)
            pub.Init()
            self._pubs[topic] = pub
        cmd = unitree_go_msg_dds__LowCmd_()
        cmd.head[0], cmd.head[1] = sample["head"]
        cmd.level_flag = sample["level_flag"]
        for i, m in enumerate(sample["motor_cmd"]):
            for key in ("mode", "q", "dq", "kp", "kd", "tau"):
                setattr(cmd.motor_cmd[i], key, m[key])
        cmd.crc = CRC().Crc(cmd)
        self._pubs[topic].Write(cmd)

    def close(self) -> None:
        for sub in self._subs:
            sub.Close()
        for pub in self._pubs.values():
            pub.Close()


__all__ = ["ChannelLayer", "Handler", "SdkChannelLayer"]
