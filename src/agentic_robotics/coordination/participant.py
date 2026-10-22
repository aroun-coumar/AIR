"""Participant: the robot-side half of team coordination.

Wraps one robot and its robot-local runtime. It answers proposals with bids, confirms
readiness for a specific task revision, executes committed work through its own runtime
(so every command still passes local admission, authority and fencing checks), and
answers status queries for reconciliation. Retransmitted commitments are deduplicated by
their deterministic command id; nothing is executed twice.
"""

from __future__ import annotations

from typing import Any

from ..contracts.capability import ProfileStatus
from ..contracts.execution import Command, CommandState
from ..core.errors import AuthorityError
from ..core.ids import new_id
from ..robot import Robot
from ..runtime.runtime import Runtime
from .protocol import CoordinationMessage, MessageKind
from .transport import Transport

__requirements__ = ("REQ06", "REQ07", "REQ09")

COORDINATOR = "coordinator"
OCCUPANCY_PREFIX = "occupancy."


class Participant:
    def __init__(self, robot: Robot, runtime: Runtime, transport: Transport, *,
                 principal: str, delegation_id: str, coordinator: str = COORDINATOR) -> None:
        self.robot = robot
        self.runtime = runtime
        self.transport = transport
        self.principal = principal
        self.delegation_id = delegation_id
        self.coordinator = coordinator
        self._seen: set[str] = set()
        transport.register(robot.robot_id, self.handle)

    @property
    def endpoint(self) -> str:
        return self.robot.robot_id

    # -- messaging -------------------------------------------------------------------
    async def _reply(self, to: CoordinationMessage, kind: MessageKind,
                     **payload: Any) -> None:
        await self.transport.send(CoordinationMessage(
            message_id=new_id("msg"), conversation_id=to.conversation_id,
            task_id=to.task_id, task_revision=to.task_revision, sender=self.endpoint,
            recipients=(to.sender,), kind=kind, sent_at=self.runtime.clock.now(),
            causal_ref=to.message_id, payload=payload, trace_id=to.trace_id))

    async def announce(self) -> None:
        cap = self.robot.capability_report()
        await self.transport.send(CoordinationMessage(
            message_id=new_id("msg"), conversation_id="discovery", task_id="-",
            task_revision=0, sender=self.endpoint, recipients=(self.coordinator,),
            kind=MessageKind.ANNOUNCE, sent_at=self.runtime.clock.now(),
            payload={"principal": self.principal,
                     "skills": sorted(self.robot.adapter.supported_skills()),
                     "capability_revision": cap.capability_revision,
                     "capabilities": {p.name: p.status.value for p in cap.profiles}}))

    async def handle(self, msg: CoordinationMessage) -> None:
        if msg.message_id in self._seen:
            self.runtime.trace.record("duplicate_message_ignored", self.endpoint,
                                      message_id=msg.message_id, message_kind=msg.kind.value)
            return
        self._seen.add(msg.message_id)
        if msg.kind is MessageKind.PROPOSE:
            await self._on_propose(msg)
        elif msg.kind is MessageKind.READY_CHECK:
            await self._on_ready_check(msg)
        elif msg.kind is MessageKind.COMMIT:
            await self._on_commit(msg)
        elif msg.kind is MessageKind.QUERY:
            await self._on_query(msg)

    # -- handlers --------------------------------------------------------------------
    def _eligibility(self, skill_id: str) -> str | None:
        """Reason this participant cannot run ``skill_id`` now, or ``None``."""
        if skill_id not in self.runtime.skills:
            return "unknown_skill"
        if skill_id not in self.robot.adapter.supported_skills():
            return "unsupported_skill"
        cap = self.robot.capability_report()
        for name in self.runtime.skills.get(skill_id).required_capabilities:
            p = cap.get(name)
            if p is None or p.status is ProfileStatus.UNAVAILABLE:
                return f"capability_unavailable:{name}"
        try:
            self.runtime.authority.check(self.delegation_id, self.principal,
                                         self.robot.robot_id, skill_id)
        except AuthorityError as exc:
            return f"no_authority:{exc.details.get('reason', 'denied')}"
        return None

    async def _on_propose(self, msg: CoordinationMessage) -> None:
        skill_id = str(msg.payload["skill_id"])
        args = dict(msg.payload.get("args", {}))
        reason = self._eligibility(skill_id)
        if reason:
            await self._reply(msg, MessageKind.DECLINE, reason=reason)
            return
        cost = self.robot.adapter.estimate_cost(skill_id, args)
        await self._reply(msg, MessageKind.BID, cost=1.0 if cost is None else float(cost),
                          capability_revision=self.robot.capability_report()
                          .capability_revision)

    async def _on_ready_check(self, msg: CoordinationMessage) -> None:
        skill_id = str(msg.payload["skill_id"])
        current = self.robot.capability_report().capability_revision
        expected = msg.payload.get("capability_revision")
        if expected is not None and expected != current:
            await self._reply(msg, MessageKind.NOT_READY, reason="capability_changed",
                              capability_revision=current)
            return
        reason = self._eligibility(skill_id)
        if reason is None and self.runtime.busy(self.robot.robot_id):
            reason = "busy"
        if reason:
            await self._reply(msg, MessageKind.NOT_READY, reason=reason)
        else:
            await self._reply(msg, MessageKind.READY, capability_revision=current)

    async def _on_commit(self, msg: CoordinationMessage) -> None:
        p = msg.payload
        command_id = str(p["command_id"])
        existing = self.runtime.find(command_id)
        await self._reply(msg, MessageKind.ACK, command_id=command_id)
        if existing is not None:
            # Retransmitted commitment: never execute twice; resend the outcome if known.
            if existing.done:
                await self._send_result(msg, existing.command_id)
            return
        command = Command(
            command_id=command_id, robot_id=self.robot.robot_id, skill_id=str(p["skill_id"]),
            issued_by=self.principal, delegation_id=self.delegation_id,
            authority_revision=self.runtime.authority.revision,
            args=dict(p.get("args", {})), fencing={k: int(v) for k, v in
                                                   dict(p.get("fencing", {})).items()},
            task_id=msg.task_id, plan_id=msg.conversation_id, trace_id=msg.trace_id)
        handle = self.runtime.submit(command)
        if not handle.done:
            await self._reply(msg, MessageKind.STARTED, command_id=command_id)
        await handle.wait()
        await self._send_result(msg, command_id)

    async def _send_result(self, msg: CoordinationMessage, command_id: str) -> None:
        handle = self.runtime.find(command_id)
        assert handle is not None and handle.result is not None
        await self._reply(msg, MessageKind.RESULT, command_id=command_id,
                          result=handle.result.to_dict(), occupancy=self.occupancy())

    async def _on_query(self, msg: CoordinationMessage) -> None:
        command_id = str(msg.payload["command_id"])
        handle = self.runtime.find(command_id)
        if handle is None:
            await self._reply(msg, MessageKind.STATUS, command_id=command_id, known=False,
                              occupancy=self.occupancy())
            return
        if handle.state is CommandState.UNRESOLVED and msg.payload.get("reconcile"):
            self.runtime.reconcile(command_id)
        await self._reply(msg, MessageKind.STATUS, command_id=command_id, known=True,
                          done=handle.done,
                          result=handle.result.to_dict() if handle.result else None,
                          occupancy=self.occupancy())

    def occupancy(self) -> dict[str, str]:
        """Physical occupancy this robot can observe: ``occupancy.<resource>`` signals."""
        return {k.removeprefix(OCCUPANCY_PREFIX): str(o.value or "")
                for k, o in self.robot.observe().items() if k.startswith(OCCUPANCY_PREFIX)}
