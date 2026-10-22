"""ROS 2 action RobotAdapter (W6.2, ADR 0019).

Mapping, and what it deliberately does not do:

- AIR admission runs first (authority, capability, preconditions). The action server's
  **goal accept/reject** is the robot side of admission: a rejected goal ends FAILED with
  ``goal_rejected`` before anything moved.
- **Goal acceptance is never completion.** Verification reads the action **result**:
  the ``action.<skill>.result`` observation exists only once the goal is terminal, and
  the standard postcondition requires ``status == "succeeded"``. ``CANCELED`` is never
  mapped to success (Nav2's ``CANCELED -> SUCCESS`` default is the counter-example);
  ``ABORTED`` is a failure. A lost server or a result that never arrives leaves the
  outcome unknown (UNRESOLVED).
- **MoveIt plan-only preflight.** A skill with ``"preflight": "moveit_plan_only"`` first
  sends the same ``MoveGroup`` goal with ``planning_options.plan_only = true`` and only
  executes when the plan's ``error_code.val`` is ``SUCCESS`` (1).
- Interrupt sends ``cancel_goal``; the stop is confirmed only when the goal's terminal
  status is observed ``CANCELED``.

Config (``adapter.config``)::

    {"capability": "navigation",
     "actions": {"navigate_to_pose": {
         "action": "/navigate_to_pose", "type": "nav2_msgs/action/NavigateToPose",
         "goal": {"pose": {"header": {"frame_id": "map"},
                           "pose": {"position": {"x": "$args.x", "y": "$args.y"}}}}}}}
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ...contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ...contracts.execution import ExecutionContract
from ...contracts.manifest import ComponentManifest
from ...contracts.observation import Observation
from ...contracts.skill import Condition, Op, ParamSpec, SkillSpec
from ...core.errors import UnsupportedOperation
from ...plugins import AdapterReport, ExecutionContext, InterruptReport, RobotAdapter
from ..common import CapabilityTracker, InterruptGate, observation, wait_until
from .actions import MOVEIT_SUCCESS, ActionLayer, CancelReturn, GoalHandle, GoalStatus

__requirements__ = ("REQ03", "REQ05", "REQ14", "REQ17")

ARG = "$args."


def action_skill(skill_id: str, description: str, inputs: tuple[ParamSpec, ...] = (), *,
                 capability: str = "ros2_actions", deadline_s: float = 120.0,
                 side_effects: tuple[str, ...] = ("moves_robot",)) -> SkillSpec:
    """A skill whose verification is the action *result* (never goal acceptance)."""
    return SkillSpec(
        skill_id=skill_id, version="0.1.0", description=description, inputs=inputs,
        required_capabilities=(capability,),
        postconditions=(Condition(signal=f"action.{skill_id}.result", field="status",
                                  op=Op.EQ, value="succeeded",
                                  description="action result is SUCCEEDED"),),
        side_effects=side_effects,
        execution=ExecutionContract(deadline_s=deadline_s, max_input_age_s=5.0),
        expected_evidence=(f"action.{skill_id}.result",))


def fill(template: Any, args: Mapping[str, Any]) -> Any:
    """Substitute ``"$args.name"`` strings in a goal template (no evaluation, no code)."""
    if isinstance(template, str) and template.startswith(ARG):
        key = template[len(ARG):]
        if key not in args:
            raise KeyError(key)
        return args[key]
    if isinstance(template, dict):
        return {k: fill(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, args) for v in template]
    return template


class Ros2ActionAdapter(RobotAdapter):
    kind = "ros2_action"

    def __init__(self, manifest: ComponentManifest, config: Mapping[str, Any]) -> None:
        super().__init__(manifest, config)
        self.capability = str(self.config.get("capability", "ros2_actions"))
        self.actions: dict[str, dict[str, Any]] = {
            str(k): dict(v) for k, v in dict(self.config.get("actions", {})).items()}
        for name, spec in self.actions.items():
            if "action" not in spec or "type" not in spec:
                raise ValueError(f"action mapping for {name!r} needs 'action' and 'type'")
        self.server_timeout = float(self.config.get("server_timeout_s", 2.0))
        self._layer: ActionLayer | None = None
        self.servers: dict[str, bool] = {}
        self._obs: dict[str, Observation] = {}
        self._caps = CapabilityTracker()
        self._goals: dict[str, GoalHandle] = {}
        self._final: dict[str, int] = {}
        self._gate = InterruptGate()

    def use_actions(self, layer: ActionLayer) -> None:
        self._layer = layer

    async def start(self) -> None:
        if self._layer is None:  # pragma: no cover - needs ROS 2
            from .actions import RclpyActionLayer
            self._layer = RclpyActionLayer(str(self.config.get("node_name", "air_action_bridge")))
        for name in self.actions:
            await self._probe(name)

    async def stop(self) -> None:
        if self._layer is not None:
            self._layer.close()

    async def _probe(self, name: str) -> bool:
        assert self._layer is not None
        spec = self.actions[name]
        try:
            ok = await self._layer.wait_for_server(spec["action"], spec["type"],
                                                   float(spec.get("server_timeout_s",
                                                                  self.server_timeout)))
        except Exception:
            ok = False
        self.servers[name] = bool(ok)
        return bool(ok)

    # -- RobotAdapter ----------------------------------------------------------------
    def supported_skills(self) -> frozenset[str]:
        return frozenset(self.actions)

    def capability_report(self) -> CapabilityReport:
        down = sorted(n for n in self.actions if not self.servers.get(n))
        if not self.actions or len(down) == len(self.actions):
            status = ProfileStatus.UNAVAILABLE
        elif down:
            status = ProfileStatus.RESTRICTED
        else:
            status = ProfileStatus.AVAILABLE
        prof = CapabilityProfile(self.capability, status,
                                 limiting_factors=tuple(f"server_unavailable:{n}" for n in down))
        return self._caps.report(self.robot_id, self.require_clock(), (prof,))

    def observe(self) -> dict[str, Observation]:
        clock = self.require_clock()
        out = dict(self._obs)
        for name in self.actions:
            sig = f"action.{name}.server"
            out[sig] = observation(clock, f"{self.robot_id}/ros2", sig,
                                   bool(self.servers.get(name)))
        return out

    def _record(self, signal: str, value: Any) -> None:
        self._obs[signal] = observation(self.require_clock(), f"{self.robot_id}/ros2", signal,
                                        value)

    async def execute(self, ctx: ExecutionContext) -> AdapterReport:
        cmd = ctx.command
        if cmd.skill_id not in self.actions:
            raise UnsupportedOperation("no ROS 2 action is mapped to this skill",
                                       skill_id=cmd.skill_id)
        assert self._layer is not None
        spec = self.actions[cmd.skill_id]
        self._obs.pop(f"action.{cmd.skill_id}.result", None)
        if not await self._probe(cmd.skill_id):
            return AdapterReport(False, f"action server {spec['action']} is not available",
                                 failure_reason="action_server_unavailable")
        try:
            goal = fill(spec.get("goal", {}), cmd.args)
        except KeyError as exc:
            return AdapterReport(False, f"goal template needs argument {exc}",
                                 failure_reason="invalid_goal_template")
        effects: dict[str, Any] = {}
        if spec.get("preflight") == "moveit_plan_only":
            plan = await self._preflight(cmd.skill_id, spec, goal, ctx)
            effects["preflight"] = plan
            if not plan["ok"]:
                return AdapterReport(False, "MoveIt plan-only preflight failed",
                                     effects, failure_reason="preflight_plan_failed")
            goal = _set_plan_only(goal, False)
        try:
            report = await self._run_goal(cmd.command_id, cmd.skill_id, spec, goal, ctx,
                                          effects)
            await self._gate.settle(cmd.command_id, self.server_timeout * 2)
            return report
        finally:
            self._gate.clear(cmd.command_id)
            self._final.pop(cmd.command_id, None)

    async def _preflight(self, name: str, spec: dict[str, Any], goal: dict[str, Any],
                         ctx: ExecutionContext) -> dict[str, Any]:
        assert self._layer is not None
        handle = await self._layer.send_goal(spec["action"], spec["type"],
                                             _set_plan_only(goal, True),
                                             lambda fb: ctx.progress("preflight", fb))
        if not handle.accepted:
            out = {"ok": False, "reason": "plan_goal_rejected"}
        else:
            status, result = await handle.result()
            code = (result.get("error_code") or {}).get("val")
            out = {"ok": status == GoalStatus.SUCCEEDED and code == MOVEIT_SUCCESS,
                   "status": GoalStatus.NAMES.get(status, "unknown"), "error_code": code,
                   "planning_time": result.get("planning_time")}
        self._record(f"action.{name}.preflight", out)
        return out

    async def _run_goal(self, command_id: str, name: str, spec: dict[str, Any],
                        goal: dict[str, Any], ctx: ExecutionContext,
                        effects: dict[str, Any]) -> AdapterReport:
        assert self._layer is not None
        handle = await self._layer.send_goal(spec["action"], spec["type"], goal,
                                             lambda fb: ctx.progress("feedback", fb))
        self._record(f"action.{name}.goal", {"goal_id": handle.goal_id,
                                             "accepted": handle.accepted})
        if not handle.accepted:
            return AdapterReport(False, "goal rejected by the action server",
                                 {**effects, "goal_id": handle.goal_id},
                                 failure_reason="goal_rejected")
        self._goals[command_id] = handle
        try:
            status, result = await handle.result()
        finally:
            self._goals.pop(command_id, None)
        self._final[command_id] = status
        name_s = GoalStatus.NAMES.get(status, "unknown")
        self._record(f"action.{name}.result", {"status": name_s, "goal_id": handle.goal_id,
                                               "result": result})
        effects = {**effects, "goal_id": handle.goal_id, "status": name_s, "result": result}
        if status == GoalStatus.SUCCEEDED:
            return AdapterReport(True, "action result SUCCEEDED", effects)
        if status in (GoalStatus.ABORTED, GoalStatus.CANCELED):
            return AdapterReport(False, f"action result {name_s.upper()}", effects,
                                 failure_reason=name_s)
        raise ConnectionError(f"action ended in non-terminal status {status}; outcome unknown")

    async def interrupt(self, command_id: str) -> InterruptReport:
        handle = self._goals.get(command_id)
        if handle is None:
            return InterruptReport(False, False, "command is not active")
        done = self._gate.begin(command_id)
        try:
            return await self._cancel(command_id, handle)
        finally:
            done.set()

    async def _cancel(self, command_id: str, handle: GoalHandle) -> InterruptReport:
        try:
            code = await handle.cancel()
        except Exception as exc:
            return InterruptReport(True, False, f"cancel request failed: {exc}")
        if code != CancelReturn.ERROR_NONE:
            return InterruptReport(False, False, f"cancel rejected (return_code {code})")
        await wait_until(lambda: command_id in self._final, self.server_timeout)
        confirmed = self._final.get(command_id) == GoalStatus.CANCELED
        return InterruptReport(True, confirmed, "goal CANCELED" if confirmed
                               else "cancel accepted; terminal status not yet CANCELED")


def _set_plan_only(goal: dict[str, Any], plan_only: bool) -> dict[str, Any]:
    out = copy.deepcopy(goal)
    out.setdefault("planning_options", {})["plan_only"] = plan_only
    return out


__all__ = ["Ros2ActionAdapter", "action_skill", "fill"]
