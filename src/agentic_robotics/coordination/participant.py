"""Participant: the robot-side half of team coordination.

Wraps one robot and its robot-local runtime. It answers proposals with bids, confirms
readiness for a specific task revision, executes released order steps through its own
runtime (so every command still passes local admission, authority and fencing checks),
and answers status queries for reconciliation.

Orders follow the VDA 5050 acceptance rules (:mod:`.orders`): retransmitted updates are
ignored as identical, conflicting or outdated ones are rejected while current work
continues, and nothing is accepted after a cancel. Released steps run in order; a step
whose released predecessor did not verify is reported NOT_EXECUTED, never guessed. A
participant that loses its coordinator keeps executing only what was already released.

Instant actions (pause, resume, cancel, state_request) are answered at any revision. A
cancel gets its own response per command (accepted, or rejected with a reason); the
command's result still reports what physically happened.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from ..contracts.capability import ProfileStatus
from ..contracts.execution import Command, CommandState
from ..contracts.skill import Interruption
from ..core.errors import AuthorityError, UnsupportedOperation
from ..core.ids import new_id
from ..robot import Robot
from ..runtime.runtime import Runtime
from .hlc import ClockDriftError, HybridLogicalClock
from .liveness import ConnectionState, LivenessMonitor, SeqStatus, SeqTracker, Stamper
from .orders import Acceptance, OrderBook, OrderStep, OrderUpdate
from .protocol import CancelMode, CoordinationMessage, InstantAction, MessageKind
from .transport import LivelinessTransport, Transport

__requirements__ = ("REQ06", "REQ07", "REQ09", "REQ17", "G07", "G14")

COORDINATOR = "coordinator"
OCCUPANCY_PREFIX = "occupancy."


@dataclass
class _Order:
    book: OrderBook
    conversation: str
    trace_id: str | None = None
    next_index: int = 0
    outcomes: dict[str, str] = field(default_factory=dict)   # task_id -> state/reason
    cancelled_steps: set[str] = field(default_factory=set)
    reported: set[str] = field(default_factory=set)
    worker: asyncio.Task[None] | None = None


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
        self.hlc = HybridLogicalClock(robot.robot_id, runtime.clock.now)
        self.stamper = Stamper(self.hlc)
        self.seq = SeqTracker()
        self.liveness = LivenessMonitor(runtime.clock.now)
        self.orders: dict[str, _Order] = {}
        self.paused = False
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._heartbeat: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        transport.register(robot.robot_id, self.handle)
        if isinstance(transport, LivelinessTransport):
            transport.subscribe_liveliness(self.endpoint, self.liveness.liveliness)
            self.liveness.on_change(self._connection_changed)
            transport.declare_liveliness(self.endpoint)

    @property
    def endpoint(self) -> str:
        return self.robot.robot_id

    @property
    def coordinator_state(self) -> ConnectionState:
        return self.liveness.state(self.coordinator)

    # -- messaging -------------------------------------------------------------------
    async def _emit(self, recipient: str, *, conversation: str, task_id: str, revision: int,
                    kind: MessageKind, causal_ref: str | None = None,
                    trace_id: str | None = None, update_id: int = 0,
                    **payload: Any) -> None:
        seq, hlc = self.stamper.stamp(recipient)
        await self.transport.send(CoordinationMessage(
            message_id=new_id("msg"), conversation_id=conversation, task_id=task_id,
            task_revision=revision, sender=self.endpoint, recipients=(recipient,), kind=kind,
            sent_at=self.runtime.clock.now(), causal_ref=causal_ref, payload=payload,
            trace_id=trace_id, seq=seq, update_id=update_id, hlc=hlc))

    async def _reply(self, to: CoordinationMessage, kind: MessageKind,
                     **payload: Any) -> None:
        await self._emit(to.sender, conversation=to.conversation_id, task_id=to.task_id,
                         revision=to.task_revision, kind=kind, causal_ref=to.message_id,
                         trace_id=to.trace_id, update_id=to.update_id, **payload)

    async def announce(self) -> None:
        cap = self.robot.capability_report()
        await self._emit(self.coordinator, conversation="discovery", task_id="-", revision=0,
                         kind=MessageKind.ANNOUNCE, principal=self.principal,
                         skills=sorted(self.robot.adapter.supported_skills()),
                         capability_revision=cap.capability_revision,
                         capabilities={p.name: p.status.value for p in cap.profiles})

    # -- liveness ----------------------------------------------------------------------
    def start_heartbeats(self, period_s: float) -> None:
        if self._heartbeat is None:
            self._heartbeat = asyncio.get_running_loop().create_task(self._beat(period_s))

    async def _beat(self, period_s: float) -> None:
        while True:
            await self._emit(self.coordinator, conversation="liveness", task_id="-",
                             revision=0, kind=MessageKind.HEARTBEAT, paused=self.paused)
            await asyncio.sleep(period_s)

    async def stop(self) -> None:
        tasks = [t for t in (self._heartbeat, *self._background,
                             *(o.worker for o in self.orders.values())) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._heartbeat = None

    def _connection_changed(self, endpoint: str, old: ConnectionState,
                            new: ConnectionState, cause: str) -> None:
        if endpoint != self.coordinator:
            return
        # Without its coordinator the robot finishes only what is already released.
        self.runtime.trace.record("participant_link", self.endpoint, peer=endpoint,
                                  old=old.value, new=new.value, cause=cause,
                                  released_pending=self._pending_released())

    def _pending_released(self) -> int:
        return sum(1 for o in self.orders.values() for s in o.book.steps
                   if s.task_id not in o.reported)

    # -- inbound -----------------------------------------------------------------------
    async def handle(self, msg: CoordinationMessage) -> None:
        if msg.message_id in self._seen:
            self.runtime.trace.record("duplicate_message_ignored", self.endpoint,
                                      message_id=msg.message_id, message_kind=msg.kind.value)
            return
        self._seen.add(msg.message_id)
        self.liveness.heard(msg.sender)
        verdict = self.seq.observe(msg.sender, msg.seq)
        if verdict.status in (SeqStatus.GAP, SeqStatus.REORDERED):
            self.runtime.trace.record(f"message_{verdict.status.value}", self.endpoint,
                                      sender=msg.sender, seq=msg.seq,
                                      expected=verdict.expected, missing=verdict.missing)
        if msg.hlc:
            try:
                self.hlc.update(msg.hlc)
            except ClockDriftError as exc:
                self.runtime.trace.record("hlc_drift_rejected", self.endpoint,
                                          sender=msg.sender, **exc.details)
                return
        if msg.kind is MessageKind.PROPOSE:
            await self._on_propose(msg)
        elif msg.kind is MessageKind.READY_CHECK:
            await self._on_ready_check(msg)
        elif msg.kind is MessageKind.COMMIT:
            await self._on_commit(msg)
        elif msg.kind is MessageKind.QUERY:
            await self._on_query(msg)
        elif msg.kind is MessageKind.CANCEL:
            await self._on_cancel(msg)
        elif msg.kind is MessageKind.INSTANT:
            await self._on_instant(msg)

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

    def _busy(self) -> bool:
        return self.runtime.busy(self.robot.robot_id) or self._pending_released() > 0

    async def _on_ready_check(self, msg: CoordinationMessage) -> None:
        skill_ids = [str(msg.payload["skill_id"]),
                     *(str(s) for s in msg.payload.get("also", ()))]
        current = self.robot.capability_report().capability_revision
        expected = msg.payload.get("capability_revision")
        if expected is not None and expected != current:
            await self._reply(msg, MessageKind.NOT_READY, reason="capability_changed",
                              capability_revision=current)
            return
        reason = next((r for r in map(self._eligibility, skill_ids) if r), None)
        if reason is None and self.paused:
            reason = "paused"
        if reason is None and self._busy():
            reason = "busy"
        if reason:
            await self._reply(msg, MessageKind.NOT_READY, reason=reason)
        else:
            await self._reply(msg, MessageKind.READY, capability_revision=current)

    # -- orders --------------------------------------------------------------------------
    async def _on_commit(self, msg: CoordinationMessage) -> None:
        update = OrderUpdate.from_payload(msg.update_id, msg.payload)
        order = self.orders.get(update.order_id)
        if order is None:
            order = self.orders[update.order_id] = _Order(OrderBook(update.order_id),
                                                          msg.conversation_id, msg.trace_id)
        verdict = order.book.offer(update)
        self.runtime.trace.record("order_update", self.endpoint, order_id=update.order_id,
                                  update_id=update.update_id,
                                  acceptance=verdict.acceptance.value, reason=verdict.reason,
                                  steps=[s.task_id for s in update.steps],
                                  horizon=list(update.horizon))
        if verdict.rejected:
            # On rejection the robot keeps its current work unchanged.
            await self._reply(msg, MessageKind.NACK, order_id=update.order_id,
                              reason=verdict.reason, current_update_id=order.book.update_id)
            return
        await self._reply(msg, MessageKind.ACK, order_id=update.order_id,
                          acceptance=verdict.acceptance.value,
                          command_ids=[s.command_id for s in update.steps])
        if verdict.acceptance is Acceptance.IGNORED_IDENTICAL:
            # Retransmission: never execute twice; resend outcomes already known.
            for step in update.steps:
                handle = self.runtime.find(step.command_id)
                if handle is not None and handle.done:
                    await self._send_result(order, step)
            return
        if order.worker is None or order.worker.done():
            order.worker = asyncio.get_running_loop().create_task(self._work(order))

    async def _work(self, order: _Order) -> None:
        """Execute released steps in order; stop at the end of the released prefix."""
        while order.next_index < len(order.book.steps):
            step = order.book.steps[order.next_index]
            if step.task_id in order.reported:
                order.next_index += 1
                continue
            if step.task_id in order.cancelled_steps or order.book.cancelled:
                await self._not_executed(order, step, "cancelled")
                order.next_index += 1
                continue
            await self._resumed.wait()          # paused: start nothing new
            if step.task_id in order.cancelled_steps or order.book.cancelled:
                continue
            blocked = [a for a in step.after if order.outcomes.get(a) != "verified"]
            if blocked:
                await self._not_executed(order, step, "dependency_not_verified",
                                         blocked=blocked)
                order.next_index += 1
                continue
            await self._execute(order, step)
            order.next_index += 1

    async def _execute(self, order: _Order, step: OrderStep) -> None:
        handle = self.runtime.find(step.command_id)
        if handle is None:
            handle = self.runtime.submit(Command(
                command_id=step.command_id, robot_id=self.robot.robot_id,
                skill_id=step.skill_id, issued_by=self.principal,
                delegation_id=self.delegation_id,
                authority_revision=self.runtime.authority.revision, args=dict(step.args),
                fencing=dict(step.fencing), task_id=step.task_id, plan_id=order.conversation,
                trace_id=order.trace_id))
            if not handle.done:
                await self._emit(self.coordinator, conversation=order.conversation,
                                 task_id=step.task_id, revision=step.task_revision,
                                 kind=MessageKind.STARTED, trace_id=order.trace_id,
                                 command_id=step.command_id)
        await handle.wait()
        await self._send_result(order, step)

    async def _send_result(self, order: _Order, step: OrderStep) -> None:
        handle = self.runtime.find(step.command_id)
        assert handle is not None and handle.result is not None
        order.outcomes[step.task_id] = handle.result.state.value
        order.reported.add(step.task_id)
        await self._emit(self.coordinator, conversation=order.conversation,
                         task_id=step.task_id, revision=step.task_revision,
                         kind=MessageKind.RESULT, trace_id=order.trace_id,
                         command_id=step.command_id, result=handle.result.to_dict(),
                         occupancy=self.occupancy())

    async def _not_executed(self, order: _Order, step: OrderStep, reason: str,
                            **detail: Any) -> None:
        if step.task_id in order.reported:
            return
        order.outcomes[step.task_id] = f"not_executed:{reason}"
        order.reported.add(step.task_id)
        self.runtime.trace.record("step_not_executed", step.command_id, reason=reason,
                                  **detail)
        await self._emit(self.coordinator, conversation=order.conversation,
                         task_id=step.task_id, revision=step.task_revision,
                         kind=MessageKind.NOT_EXECUTED, trace_id=order.trace_id,
                         command_id=step.command_id, reason=reason,
                         occupancy=self.occupancy(), **detail)

    def _step(self, command_id: str) -> tuple[_Order, OrderStep] | None:
        for order in self.orders.values():
            for step in order.book.steps:
                if step.command_id == command_id:
                    return order, step
        return None

    async def _on_query(self, msg: CoordinationMessage) -> None:
        command_id = str(msg.payload["command_id"])
        handle = self.runtime.find(command_id)
        if handle is None:
            found = self._step(command_id)
            if found is not None and found[1].task_id not in found[0].reported:
                # Released, not started yet (queued behind earlier steps or paused).
                await self._reply(msg, MessageKind.STATUS, command_id=command_id, known=True,
                                  done=False, result=None, occupancy=self.occupancy())
                return
            reason = "not_received" if found is None else \
                found[0].outcomes.get(found[1].task_id, "not_executed")
            await self._reply(msg, MessageKind.STATUS, command_id=command_id, known=False,
                              reason=reason, occupancy=self.occupancy())
            return
        if handle.state is CommandState.UNRESOLVED and msg.payload.get("reconcile"):
            self.runtime.reconcile(command_id)
        await self._reply(msg, MessageKind.STATUS, command_id=command_id, known=True,
                          done=handle.done,
                          result=handle.result.to_dict() if handle.result else None,
                          occupancy=self.occupancy())

    # -- cancellation and instant actions --------------------------------------------------
    async def _on_cancel(self, msg: CoordinationMessage) -> None:
        results = await self._cancel(msg.payload)
        await self._reply(msg, MessageKind.CANCEL_RESPONSE, results=results,
                          order_id=msg.payload.get("order_id"))

    async def _cancel(self, p: dict[str, Any]) -> dict[str, dict[str, Any]]:
        mode = CancelMode(str(p.get("mode", CancelMode.CANCEL.value)))
        reason = str(p.get("reason", "cancel_requested"))
        order = self.orders.get(str(p.get("order_id", "")))
        if order is not None and p.get("whole_order", True):
            order.book.cancelled = True   # nothing is accepted after a cancel
        results: dict[str, dict[str, Any]] = {}
        for command_id in [str(c) for c in p.get("command_ids", ())]:
            results[command_id] = await self._cancel_one(command_id, mode, reason)
        self.runtime.trace.record("cancel_handled", self.endpoint, mode=mode.value,
                                  results={k: v["accepted"] for k, v in results.items()})
        return results

    async def _cancel_one(self, command_id: str, mode: CancelMode,
                          reason: str) -> dict[str, Any]:
        handle = self.runtime.find(command_id)
        if handle is None:
            found = self._step(command_id)
            if found is None:
                return {"accepted": False, "reason": "unknown_command"}
            order, step = found
            if step.task_id in order.reported:
                return {"accepted": False, "reason": "already_finished"}
            order.cancelled_steps.add(step.task_id)
            await self._not_executed(order, step, "cancelled")
            return {"accepted": True, "reason": "not_started", "stopped_confirmed": True}
        if handle.done:
            return {"accepted": False, "reason": "already_finished",
                    "state": handle.state.value}
        skill = self.runtime.skills.get(handle.command.skill_id)
        if handle.state is CommandState.EXECUTING and \
                skill.interruption is Interruption.NONE:
            return {"accepted": False, "reason": "not_interruptible", "mode": mode.value}

        async def run() -> None:
            with contextlib.suppress(UnsupportedOperation):
                await self.runtime.cancel(command_id, reason=f"{mode.value}:{reason}")

        task = asyncio.get_running_loop().create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return {"accepted": True, "reason": "stop_requested", "mode": mode.value}

    async def _on_instant(self, msg: CoordinationMessage) -> None:
        action = InstantAction(str(msg.payload["action"]))
        out: dict[str, Any] = {"action": action.value, "accepted": True}
        if action is InstantAction.PAUSE:
            self.paused = True
            self._resumed.clear()
            active = self.runtime.busy(self.robot.robot_id)
            # A running command is not paused mid-motion; nothing new starts.
            out.update(paused=True, current_continues=active)
        elif action is InstantAction.RESUME:
            self.paused = False
            self._resumed.set()
            out.update(paused=False)
        elif action is InstantAction.CANCEL:
            out.update(results=await self._cancel(msg.payload))
        else:
            out.update(state=self.state())
        self.runtime.trace.record("instant_action", self.endpoint, action=action.value)
        await self._reply(msg, MessageKind.INSTANT_RESPONSE, **out)

    def state(self) -> dict[str, Any]:
        """The robot-side view (answer to ``state_request``)."""
        return {
            "paused": self.paused,
            "busy": self.runtime.busy(self.robot.robot_id),
            "coordinator": self.coordinator_state.value,
            "capability_revision": self.robot.capability_report().capability_revision,
            "occupancy": self.occupancy(),
            "orders": {oid: {"update_id": o.book.update_id,
                             "released": [s.task_id for s in o.book.steps],
                             "horizon": list(o.book.horizon),
                             "cancelled": o.book.cancelled,
                             "outcomes": dict(o.outcomes)}
                       for oid, o in self.orders.items()},
        }

    def occupancy(self) -> dict[str, str]:
        """Physical occupancy this robot can observe: ``occupancy.<resource>`` signals."""
        return {k.removeprefix(OCCUPANCY_PREFIX): str(o.value or "")
                for k, o in self.robot.observe().items() if k.startswith(OCCUPANCY_PREFIX)}
