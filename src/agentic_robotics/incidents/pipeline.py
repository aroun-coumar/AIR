"""Incident pipeline: detector -> incident -> hypotheses -> bounded tests -> recovery or
human handoff (ADR 0015).

    pipe = IncidentPipeline(project=project, runtime=runtime)
    inc = pipe.open(Detection("health", "carrier-1", "pack_temperature", "pack hot", t))
    h = pipe.propose(inc.incident_id, "cooling fan stalled", proposed_by="agent:diag")
    run = await pipe.run_test(inc.incident_id, test, issued_by=..., delegation_id=...)
    await pipe.recover(inc.incident_id, TestAction("dock_at", {"station": "charger"}), ...)

Rules that keep it honest:

- **Novelty** is decided against the failure taxonomies of the hardware domain packs and
  the signatures of incidents people closed before; a novel incident is flagged.
- **Agents propose.** A hypothesis from ``agent:*`` or ``model:*`` is marked
  non-authoritative; only a bounded test's *observations* move it to supported or
  refuted. Agents cannot hand off or close incidents.
- **Bounded tests** run only if the steady state holds first, stay inside an admission
  budget, and every robot action goes through ``Runtime.submit`` (admission, authority,
  fencing), never a direct adapter call. Rollbacks run by strategy.
- An UNRESOLVED command (physical outcome unknown) stops the test with no further
  commands, and the incident is handed off: it is settled by ``reconcile()``, never by
  a retry.
- Recovery is VERIFIED from observations; recovery the robot owns is never commanded,
  only observed (:meth:`IncidentPipeline.observe_recovery`).
- Every step is a ``incident.*`` ledger entry carrying the incident snapshot, so the
  incident list is rebuilt by replay; with a runtime, every step is also a trace event.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..contracts.execution import Command, CommandState, ConditionResult
from ..contracts.manifest import ComponentManifest
from ..contracts.skill import Condition
from ..core.errors import AdmissionRejected, AuthorityError, SchemaError
from ..core.ids import new_id
from .model import (
    BoundedTest,
    Detection,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentState,
    RollbackStrategy,
    TestAction,
    TestOutcome,
    TestRun,
)

__requirements__ = ("REQ14", "REQ11", "REQ17", "G11", "G13")

NON_AUTHORITATIVE_PREFIXES = ("agent:", "model:")


def is_non_authoritative(principal: str) -> bool:
    return principal.startswith(NON_AUTHORITATIVE_PREFIXES)


def signature(d: Detection) -> str:
    return "|".join((d.detector, d.robot_id, d.component_id or "-", d.signal))


class IncidentError(SchemaError):
    code = "incident_error"


class IncidentPipeline:
    def __init__(self, *, project: Any = None, runtime: Any = None, domains: Any = None,
                 catalog: Any = None, manifests: Iterable[ComponentManifest] = (),
                 robot_owned_recovery: Mapping[str, Iterable[str]] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        """``robot_owned_recovery``: robot id -> skill ids the robot runs itself (from
        each :class:`~agentic_robotics.health.HealthManifest`)."""
        if domains is None:
            from ..hardware.domains import builtin_domains
            domains = builtin_domains()
        self.project, self.runtime, self.domains, self.catalog = project, runtime, domains, \
            catalog
        self.manifests = {m.robot_id: m for m in manifests}
        self.robot_owned = {k: frozenset(v) for k, v in (robot_owned_recovery or {}).items()}
        self._now = now or (runtime.clock.now if runtime is not None else time.time)
        self._incidents: dict[str, Incident] = {}
        self._learned: dict[str, str] = {}
        if project is not None:
            self._replay()

    # -- persistence ------------------------------------------------------------------
    def _replay(self) -> None:
        for e in self.project.ledger.query(prefix="incident."):
            snap = e.data.get("incident")
            if snap is not None:
                inc = Incident.from_dict(snap)
                self._incidents[inc.incident_id] = inc
            if e.kind == "incident.closed" and e.data.get("learned"):
                self._learned[str(e.data["signature"])] = e.subject

    def _record(self, kind: str, inc: Incident, **extra: Any) -> Incident:
        self._incidents[inc.incident_id] = inc
        if self.project is not None:
            self.project.ledger.append(kind, inc.incident_id,
                                       {"incident": inc.to_dict(), **extra})
        if self.runtime is not None:
            self.runtime.trace.record(kind.replace(".", "_"), inc.incident_id,
                                      robot_id=inc.robot_id, state=inc.state.value, **{
                                          k: v for k, v in extra.items()
                                          if isinstance(v, (str, int, float, bool))})
        return inc

    # -- queries ------------------------------------------------------------------------
    def get(self, incident_id: str) -> Incident:
        if incident_id in self._incidents:
            return self._incidents[incident_id]
        hits = [i for i in self._incidents if i.startswith(incident_id)]
        if len(hits) == 1:
            return self._incidents[hits[0]]
        raise IncidentError("unknown incident" if not hits else "ambiguous incident prefix",
                            incident=incident_id)

    def incidents(self, *, active_only: bool = False) -> list[Incident]:
        out = [i for i in self._incidents.values() if not active_only or i.state.active]
        return sorted(out, key=lambda i: (-int(i.novel), -i.detected_at))

    # -- detection ----------------------------------------------------------------------
    def known_classes(self, d: Detection) -> list[str]:
        """Failure classes from the domain packs that explain this detection."""
        packs = list(self.domains.domains)
        m = self.manifests.get(d.robot_id)
        if self.catalog is not None and m is not None and d.component_id:
            c = m.component(d.component_id)
            if c is not None and c.type_id:
                scoped = self.domains.for_types([c.type_id], self.catalog)
                packs = scoped or packs
        classes = {f.failure_id: f for p in packs for f in p.failure_taxonomy}
        if d.failure_class:
            every = {f.failure_id for p in self.domains.domains for f in p.failure_taxonomy}
            return [d.failure_class] if d.failure_class in every else []
        return sorted(fid for fid, f in classes.items() if d.signal in f.detect_via)

    def open(self, d: Detection) -> Incident:
        """Open an incident (or count a recurrence of an active one with the same
        signature)."""
        sig = signature(d)
        for inc in self._incidents.values():
            if inc.signature == sig and inc.state.active:
                return self._record("incident.recurred", inc.evolve(
                    occurrences=inc.occurrences + 1,
                    evidence=tuple(dict.fromkeys((*inc.evidence, *d.evidence)))))
        known = self.known_classes(d)
        if sig in self._learned:
            known.append(f"learned:{self._learned[sig]}")
        inc = Incident(incident_id=new_id("inc"), robot_id=d.robot_id, detected_at=d.observed_at,
                       detector=d.detector, signal=d.signal, summary=d.summary, signature=sig,
                       novel=not known, known_classes=tuple(known), component_id=d.component_id,
                       labels=dict(d.labels), evidence=d.evidence)
        return self._record("incident.opened", inc, novel=inc.novel)

    def propose(self, incident_id: str, statement: str, *, proposed_by: str,
                failure_class: str | None = None, evidence: Sequence[str] = ()) -> Hypothesis:
        inc = self._active(incident_id)
        h = Hypothesis(new_id("hyp"), statement, proposed_by,
                       authoritative=not is_non_authoritative(proposed_by),
                       failure_class=failure_class, evidence=tuple(evidence))
        self._record("incident.hypothesis", inc.evolve(
            state=IncidentState.INVESTIGATING, hypotheses=(*inc.hypotheses, h)),
            hypothesis_id=h.hypothesis_id, authoritative=h.authoritative)
        return h

    def _active(self, incident_id: str) -> Incident:
        inc = self.get(incident_id)
        if not inc.state.active:
            raise IncidentError("incident is not active", incident=inc.incident_id,
                                state=inc.state.value)
        return inc

    # -- bounded tests ------------------------------------------------------------------
    def _observe(self, robot_id: str, conditions: Sequence[Condition], max_age_s: float
                 ) -> tuple[ConditionResult, ...]:
        robot = self.runtime.robot(robot_id)
        obs = robot.observe()
        now = self.runtime.clock.now()
        out = []
        for c in conditions:
            r = c.evaluate(obs, {}, now)
            if r.satisfied and r.observation_age_s is not None and \
                    r.observation_age_s > max_age_s:
                r = ConditionResult(r.description, False, r.observed, r.expected,
                                    r.observation_age_s, detail="observation too old")
            out.append(r)
        return tuple(out)

    async def _submit(self, action: TestAction, robot_id: str, issued_by: str,
                      delegation_id: str, deadline: float | None) -> tuple[str, CommandState,
                                                                           str]:
        rt = self.runtime
        cmd = Command(command_id=new_id("cmd"), robot_id=action.robot_id or robot_id,
                      skill_id=action.skill_id, issued_by=issued_by,
                      delegation_id=delegation_id, authority_revision=rt.authority.revision,
                      args=dict(action.args))
        handle = rt.submit(cmd)
        timeout = action.timeout_s
        if deadline is not None:
            timeout = max(0.0, min(timeout, deadline - time.monotonic()))
        try:
            result = await handle.wait(timeout)
        except TimeoutError:
            result = await handle.cancel("incident_test_budget_exceeded")
        return cmd.command_id, result.state, result.reason or ""

    def _require_runtime(self) -> None:
        if self.runtime is None:
            raise IncidentError("bounded tests and recovery need a runtime: every robot "
                                "action goes through Runtime.submit")

    async def run_test(self, incident_id: str, test: BoundedTest, *, issued_by: str,
                       delegation_id: str) -> TestRun:
        self._require_runtime()
        inc = self._active(incident_id)
        inc.hypothesis(test.hypothesis_id)
        started = self._now()
        self._record("incident.test_started", inc, test_id=test.test_id,
                     hypothesis_id=test.hypothesis_id, test=test.to_dict())

        def finish(run: TestRun) -> TestRun:
            current = self.get(incident_id)
            hyps = current.hypotheses
            status = {TestOutcome.SUPPORTED: HypothesisStatus.SUPPORTED,
                      TestOutcome.REFUTED: HypothesisStatus.REFUTED,
                      TestOutcome.INCONCLUSIVE: HypothesisStatus.INCONCLUSIVE}.get(run.outcome)
            if status is not None:
                hyps = tuple(h if h.hypothesis_id != test.hypothesis_id else Hypothesis(
                    h.hypothesis_id, h.statement, h.proposed_by, h.authoritative,
                    h.failure_class, status, (*h.evidence, f"test:{test.test_id}"))
                    for h in hyps)
            done = dataclasses.replace(run, finished_at=self._now())
            updated = current.evolve(hypotheses=hyps, tests=(*current.tests, done))
            self._record("incident.test_finished", updated, test_id=test.test_id,
                         outcome=done.outcome.value, reason=done.reason)
            if done.outcome is TestOutcome.ABORTED:
                self.handoff(incident_id, to="role:operator", by="system:incidents",
                             reason=f"bounded test aborted: {done.reason}")
            return done

        if len(test.method) > test.budget.max_commands:
            return finish(TestRun(test.test_id, test.hypothesis_id, TestOutcome.NOT_RUN,
                                  f"method needs {len(test.method)} commands, budget allows "
                                  f"{test.budget.max_commands}", started_at=started))
        before = self._observe(inc.robot_id, test.steady_state, test.max_input_age_s)
        self._record("incident.steady_state", self.get(incident_id), test_id=test.test_id,
                     phase="before", met=all(r.satisfied for r in before))
        if not all(r.satisfied for r in before):
            return finish(TestRun(test.test_id, test.hypothesis_id, TestOutcome.NOT_RUN,
                                  "steady state not met before the method", before=before,
                                  started_at=started))

        deadline = time.monotonic() + test.budget.max_duration_s
        commands: list[str] = []
        failed = ""
        for action in test.method:
            cid, state, reason = await self._submit(action, inc.robot_id, issued_by,
                                                    delegation_id, deadline)
            commands.append(f"method:{cid}:{state.value}")
            self._record("incident.test_command", self.get(incident_id), test_id=test.test_id,
                         phase="method", command_id=cid, command_state=state.value,
                         reason=reason)
            if state is CommandState.UNRESOLVED:
                return finish(TestRun(test.test_id, test.hypothesis_id, TestOutcome.ABORTED,
                                      f"{cid} unresolved ({reason}); reconcile it, do not "
                                      "retry", before=before, commands=tuple(commands),
                                      started_at=started))
            if state is not CommandState.VERIFIED:
                failed = f"{cid} {state.value} ({reason})"
                break

        after = self._observe(inc.robot_id, test.steady_state, test.max_input_age_s)
        deviated = not all(r.satisfied for r in after)
        self._record("incident.steady_state", self.get(incident_id), test_id=test.test_id,
                     phase="after", met=not deviated)

        strategy = test.rollback_strategy
        if strategy is RollbackStrategy.ALWAYS or (
                strategy is RollbackStrategy.DEVIATED and (deviated or failed)):
            for action in test.rollbacks:
                cid, state, reason = await self._submit(action, inc.robot_id, issued_by,
                                                        delegation_id, None)
                commands.append(f"rollback:{cid}:{state.value}")
                self._record("incident.rollback", self.get(incident_id), test_id=test.test_id,
                             command_id=cid, command_state=state.value, reason=reason)
                if state is not CommandState.VERIFIED:
                    return finish(TestRun(
                        test.test_id, test.hypothesis_id, TestOutcome.ABORTED,
                        f"rollback {cid} {state.value} ({reason})", before=before,
                        after=after, deviated=deviated, commands=tuple(commands),
                        started_at=started))

        if failed:
            outcome, reason = TestOutcome.INCONCLUSIVE, f"method did not complete: {failed}"
        elif deviated == test.predicts_deviation:
            outcome, reason = TestOutcome.SUPPORTED, "observations matched the prediction"
        else:
            outcome, reason = TestOutcome.REFUTED, "observations contradicted the prediction"
        return finish(TestRun(test.test_id, test.hypothesis_id, outcome, reason, before=before,
                              after=after, deviated=deviated, commands=tuple(commands),
                              started_at=started))

    # -- recovery, handoff, closing -----------------------------------------------------
    async def recover(self, incident_id: str, action: TestAction, *, issued_by: str,
                      delegation_id: str, verify: Sequence[Condition] = (),
                      max_input_age_s: float = 1.0) -> bool:
        """Command a recovery through admission. True only if the command is VERIFIED and
        the ``verify`` conditions then hold on fresh observations."""
        self._require_runtime()
        inc = self._active(incident_id)
        robot = action.robot_id or inc.robot_id
        if action.skill_id in self.robot_owned.get(robot, frozenset()):
            self._record("incident.recovery_refused", inc, skill_id=action.skill_id,
                         reason="robot_owned_recovery")
            raise AdmissionRejected("robot_owned_recovery", "the robot owns this recovery; "
                                    "observe it with observe_recovery instead",
                                    robot_id=robot, skill_id=action.skill_id)
        cid, state, reason = await self._submit(action, inc.robot_id, issued_by, delegation_id,
                                                None)
        inc = self.get(incident_id).evolve(
            recovery=(*self.get(incident_id).recovery, f"{cid}:{state.value}"))
        if state is CommandState.UNRESOLVED:
            self._record("incident.recovery", inc, command_id=cid, command_state=state.value)
            self.handoff(incident_id, to="role:operator", by="system:incidents",
                         reason=f"recovery {cid} unresolved ({reason}); reconcile it")
            return False
        checks = self._observe(robot, verify, max_input_age_s) if state is \
            CommandState.VERIFIED else ()
        ok = state is CommandState.VERIFIED and all(r.satisfied for r in checks)
        if ok:
            inc = inc.evolve(state=IncidentState.RECOVERED,
                             resolution=f"recovery {action.skill_id} verified")
        self._record("incident.recovery", inc, command_id=cid, command_state=state.value,
                     verified=ok)
        return ok

    def observe_recovery(self, incident_id: str, conditions: Sequence[Condition], *,
                         max_input_age_s: float = 1.0) -> bool:
        """For recovery the robot owns: mark RECOVERED only when observations show it."""
        self._require_runtime()
        inc = self._active(incident_id)
        checks = self._observe(inc.robot_id, conditions, max_input_age_s)
        ok = bool(checks) and all(r.satisfied for r in checks)
        if ok:
            self._record("incident.recovery", inc.evolve(
                state=IncidentState.RECOVERED, recovery=(*inc.recovery, "observed"),
                resolution="robot-owned recovery observed"), verified=True, observed=True)
        return ok

    def handoff(self, incident_id: str, *, to: str, by: str, reason: str) -> Incident:
        if is_non_authoritative(by):
            raise AuthorityError("agents propose; they cannot hand off incidents", by=by)
        inc = self.get(incident_id)
        if inc.state is IncidentState.CLOSED:
            raise IncidentError("incident is closed", incident=inc.incident_id)
        return self._record("incident.handoff", inc.evolve(
            state=IncidentState.HANDED_OFF, handed_off_to=to), to=to, by=by, reason=reason)

    def close(self, incident_id: str, *, by: str, resolution: str,
              learned: bool = True) -> Incident:
        """Close with a resolution. ``learned`` makes the signature known, so the same
        detection is no longer novel."""
        if is_non_authoritative(by):
            raise AuthorityError("agents propose; they cannot close incidents", by=by)
        if not resolution.strip():
            raise IncidentError("closing an incident needs a resolution")
        inc = self.get(incident_id)
        if inc.state is IncidentState.CLOSED:
            raise IncidentError("incident is already closed", incident=inc.incident_id)
        if learned:
            self._learned[inc.signature] = inc.incident_id
        return self._record("incident.closed", inc.evolve(
            state=IncidentState.CLOSED, resolution=resolution), by=by, learned=learned,
            signature=inc.signature)
