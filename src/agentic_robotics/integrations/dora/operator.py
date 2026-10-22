"""Expose AIR skills as a dora operator (W6.1 mapping layer, ADR 0019).

Verified from ``dora-rs/dora`` (HEAD 5a2d379, Apache-2.0): ``docs/api-python.md`` (an
``Operator`` class with ``on_event(self, dora_event, send_output) -> DoraStatus``;
``DoraStatus.CONTINUE/STOP/STOP_ALL`` = 0/1/2; ``send_output(output_id, data, metadata)``;
well-known metadata keys ``request_id``, ``goal_id`` and ``goal_status`` in
``"succeeded" | "aborted" | "canceled"``), and ``examples/python-operator-dataflow``.

Ports of the AIR skill operator:

- input ``command``: JSON ``{"robot_id", "skill_id", "args"}`` (bytes, str, a dict, or an
  Arrow array whose first element is such a struct). Metadata ``goal_id``/``request_id``
  pass through to every reply.
- input ``cancel``: the ``goal_id`` to cancel.
- input ``tick`` (e.g. ``dora/timer/millis/20``): polls submitted commands.
- output ``status``: ``{"command_id", "state"}`` right after submission (accepted or
  rejected by AIR admission: a dataflow message is a *proposal*).
- output ``result``: the terminal ExecutionResult; ``goal_status`` is ``succeeded`` only
  for VERIFIED. FAILED, REJECTED, UNRESOLVED and an unverifiable REPORTED_COMPLETE are
  ``aborted`` (``air_state`` says which); CANCELLED is ``canceled``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

from ...contracts.execution import Command, CommandState
from ...core.errors import FrameworkError
from ...core.ids import new_id

__requirements__ = ("REQ05", "REQ07", "REQ12")

CONTINUE, STOP, STOP_ALL = 0, 1, 2
INPUTS = {"command": True, "cancel": False, "tick": True}       # name: required
OUTPUTS = ("status", "result")
SendOutput = Callable[..., None]


def _decode(value: Any) -> Any:
    if hasattr(value, "to_pylist"):
        items = value.to_pylist()
        value = items[0] if items else None
    if isinstance(value, bytes | bytearray):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    return value


def goal_status(state: CommandState) -> str:
    if state is CommandState.VERIFIED:
        return "succeeded"
    if state is CommandState.CANCELLED:
        return "canceled"
    return "aborted"


class SkillOperatorCore:
    """Event handling on the runtime's event loop. Every command goes through
    ``Runtime.submit`` under ``delegation_id``; nothing in a message grants authority."""

    def __init__(self, runtime: Any, *, principal: str, delegation_id: str) -> None:
        self.runtime = runtime
        self.principal = principal
        self.delegation_id = delegation_id
        self.pending: dict[str, tuple[Any, dict[str, Any]]] = {}   # command_id -> handle, meta
        self.by_goal: dict[str, str] = {}

    async def handle(self, event: Mapping[str, Any], send_output: SendOutput) -> int:
        kind = event.get("type")
        if kind == "STOP":
            return STOP
        if kind != "INPUT":
            return CONTINUE
        port = event.get("id")
        meta = dict(event.get("metadata") or {})
        if port == "command":
            self._submit(event.get("value"), meta, send_output)
        elif port == "cancel":
            raw = event.get("value")
            try:
                goal = str(_decode(raw))
            except ValueError:  # a bare goal id string is fine too
                goal = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
            cid = self.by_goal.get(goal)
            if cid is not None:
                await self.runtime.cancel(cid, reason="dora_cancel")
        self._drain(send_output)
        return CONTINUE

    def _submit(self, value: Any, meta: dict[str, Any], send_output: SendOutput) -> None:
        reply: dict[str, Any] = {k: meta[k] for k in ("goal_id", "request_id") if k in meta}
        try:
            body = _decode(value)
            if not isinstance(body, dict):
                raise ValueError("command must be an object")
            unknown = set(body) - {"robot_id", "skill_id", "args"}
            if unknown:
                raise ValueError(f"unknown command fields {sorted(unknown)}")
            cmd = Command(command_id=new_id("cmd"), robot_id=str(body["robot_id"]),
                          skill_id=str(body["skill_id"]), issued_by=self.principal,
                          delegation_id=self.delegation_id,
                          authority_revision=self.runtime.authority.revision,
                          args=dict(body.get("args") or {}))
            handle = self.runtime.submit(cmd)
        except (ValueError, KeyError, TypeError, FrameworkError) as exc:
            send_output("result", _json({"state": "invalid", "error": str(exc)}),
                        {**reply, "goal_status": "aborted", "air_state": "invalid"})
            return
        if "goal_id" in reply:
            self.by_goal[str(reply["goal_id"])] = cmd.command_id
        send_output("status", _json({"command_id": cmd.command_id,
                                     "state": handle.state.value}),
                    {**reply, "air_command_id": cmd.command_id})
        self.pending[cmd.command_id] = (handle, reply)

    def _drain(self, send_output: SendOutput) -> None:
        for cid, (handle, reply) in list(self.pending.items()):
            if not handle.done:
                continue
            res = handle.result
            del self.pending[cid]
            self.by_goal = {g: c for g, c in self.by_goal.items() if c != cid}
            send_output("result", _json(res.to_dict()),
                        {**reply, "goal_status": goal_status(res.state),
                         "air_state": res.state.value, "air_command_id": cid})


def _json(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode("utf-8")


def make_operator(build_core: Callable[[], SkillOperatorCore]) -> type:
    """Build a dora ``Operator`` class whose runtime lives on a background event loop.

    ``build_core`` runs on that loop (create the Runtime, register robots, grant the
    delegation there). Use it in the operator file the dataflow points at::

        Operator = make_operator(my_factory)
    """

    class Operator:
        def __init__(self) -> None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()

            async def build() -> SkillOperatorCore:
                return build_core()

            self.core = asyncio.run_coroutine_threadsafe(build(), self._loop).result()

        def on_event(self, dora_event: Any, send_output: SendOutput) -> Any:
            fut = asyncio.run_coroutine_threadsafe(self.core.handle(dora_event, send_output),
                                                   self._loop)
            return _status(fut.result())

    return Operator


def _status(code: int) -> Any:
    try:
        from dora import DoraStatus  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:
        return code
    return DoraStatus(code)  # pragma: no cover


__all__ = ["CONTINUE", "INPUTS", "OUTPUTS", "STOP", "STOP_ALL", "SkillOperatorCore",
           "goal_status", "make_operator"]
