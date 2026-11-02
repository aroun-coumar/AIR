"""Service registry: providers register the interfaces they implement; callers bind to
an interface and a version range, never to an implementation.

Adopted from robonix's Atlas (register, declare, heartbeat, lifecycle, query) and from
rosclaw's provider routes (primary plus fallbacks, timeout, prefer local). AIR adds the
checks those leave out:

- **Who may provide.** With ``allowed_principals`` set, only those principals can
  register providers. (Atlas accepts any client that can reach its port.)
- **What may be provided.** A provider can only claim interface versions that exist in
  the :class:`~agentic_robotics.library.interfaces.InterfaceCatalog`.
- **What may be called directly.** ``physical`` interfaces are refused: moving the
  robot goes through skills and runtime admission, never through a service call.
- **What may be retried.** ``none`` and ``compute`` interfaces fall back to the next
  provider on failure. A ``state`` interface whose call failed with an unknown outcome is
  *not* retried elsewhere; the caller gets :class:`ServiceOutcomeUnknown`.
- **What comes back.** Arguments are checked against the contract's inputs before the
  call, and declared outputs are checked after it; a provider that breaks its contract
  is marked ERROR.
- A provider that misses heartbeats is STALE and is not selected.

Every registration, state change and call is a trace event.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import FrameworkError
from ..core.versions import satisfies, version_key
from ..runtime.trace import Trace
from .contracts import InterfaceContract, InterfaceEffect, Provide
from .interfaces import InterfaceCatalog

__requirements__ = ("REQ05", "REQ06", "REQ15", "G02")

Handler = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class ServiceError(FrameworkError):
    code = "service_error"


class ServiceOutcomeUnknown(ServiceError):
    """A state-changing call failed in a way that leaves its effect unknown."""

    code = "service_outcome_unknown"


class ProviderState(StrEnum):
    REGISTERED = "registered"
    ACTIVE = "active"
    INACTIVE = "inactive"
    STALE = "stale"           # missed its heartbeat deadline
    ERROR = "error"           # broke its contract or failed repeatedly
    TERMINATED = "terminated"


@dataclass
class Provider:
    provider_id: str
    principal: str
    provides: tuple[Provide, ...]
    handler: Handler
    local: bool = True
    priority: int = 0
    heartbeat_s: float | None = None     # None: no heartbeat expected
    state: ProviderState = ProviderState.REGISTERED
    last_heartbeat: float = 0.0
    failures: int = 0
    calls: int = 0

    def version_of(self, interface: str) -> str | None:
        return next((p.version for p in self.provides if p.interface == interface), None)


@dataclass(frozen=True)
class Binding:
    provider_id: str
    interface: str
    version: str
    contract: InterfaceContract


@dataclass
class CallResult:
    interface: str
    provider_id: str
    version: str
    outputs: dict[str, Any]
    duration_s: float
    attempts: list[dict[str, Any]] = field(default_factory=list)


class ServiceRegistry:
    def __init__(self, interfaces: InterfaceCatalog, *, trace: Trace | None = None,
                 allowed_principals: Iterable[str] | None = None,
                 clock: Callable[[], float] = time.monotonic, max_failures: int = 3,
                 default_timeout_s: float = 5.0) -> None:
        self.interfaces = interfaces
        self.trace = trace
        self.allowed = None if allowed_principals is None else frozenset(allowed_principals)
        self.clock = clock
        self.max_failures = max_failures
        self.default_timeout_s = default_timeout_s
        self._providers: dict[str, Provider] = {}

    def _record(self, kind: str, subject: str, **data: Any) -> None:
        if self.trace is not None:
            self.trace.record(kind, subject, **data)

    # -- lifecycle ------------------------------------------------------------------------
    def register(self, provider_id: str, principal: str, provides: Iterable[Provide],
                 handler: Handler, *, local: bool = True, priority: int = 0,
                 heartbeat_s: float | None = None, activate: bool = True) -> Provider:
        if self.allowed is not None and principal not in self.allowed:
            self._record("service_registration_refused", provider_id, principal=principal,
                         reason="principal_not_allowed")
            raise ServiceError("principal may not register providers", principal=principal,
                               provider=provider_id)
        if provider_id in self._providers and \
                self._providers[provider_id].state is not ProviderState.TERMINATED:
            raise ServiceError("provider already registered", provider=provider_id)
        offered = tuple(provides)
        for p in offered:
            if self.interfaces.get(p.interface, p.version) is None:
                raise ServiceError("unknown interface version", provider=provider_id,
                                   interface=p.interface, version=p.version,
                                   known=self.interfaces.versions(p.interface))
        prov = Provider(provider_id, principal, offered, handler, local, priority, heartbeat_s,
                        last_heartbeat=self.clock())
        self._providers[provider_id] = prov
        self._record("service_registered", provider_id, principal=principal,
                     provides=[f"{p.interface}@{p.version}" for p in offered], local=local)
        if activate:
            self.set_state(provider_id, ProviderState.ACTIVE)
        return prov

    def heartbeat(self, provider_id: str) -> None:
        prov = self._get(provider_id)
        prov.last_heartbeat = self.clock()
        if prov.state is ProviderState.STALE:
            self.set_state(provider_id, ProviderState.ACTIVE, reason="heartbeat resumed")

    def set_state(self, provider_id: str, state: ProviderState, reason: str = "") -> None:
        prov = self._get(provider_id)
        if prov.state is not state:
            self._record("service_state", provider_id, frm=prov.state.value, to=state.value,
                         reason=reason)
            prov.state = state

    def deregister(self, provider_id: str) -> None:
        self.set_state(provider_id, ProviderState.TERMINATED, reason="deregistered")

    def _get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ServiceError("unknown provider", provider=provider_id) from None

    def _refresh(self) -> None:
        now = self.clock()
        for prov in self._providers.values():
            if prov.state is ProviderState.ACTIVE and prov.heartbeat_s is not None and \
                    now - prov.last_heartbeat > prov.heartbeat_s:
                self.set_state(prov.provider_id, ProviderState.STALE,
                               reason=f"no heartbeat for {now - prov.last_heartbeat:.1f} s")

    def providers(self) -> list[Provider]:
        self._refresh()
        return sorted(self._providers.values(), key=lambda p: p.provider_id)

    # -- resolution ---------------------------------------------------------------------
    def candidates(self, interface: str, spec: str = "*",
                   prefer: Iterable[str] = ()) -> list[Binding]:
        """Usable providers in routing order: preferred ids, local, priority, version."""
        self._refresh()
        preferred = list(prefer)
        out = []
        for prov in self._providers.values():
            version = prov.version_of(interface)
            if version is None or prov.state is not ProviderState.ACTIVE or \
                    not satisfies(version, spec):
                continue
            contract = self.interfaces.get(interface, version)
            assert contract is not None   # checked at registration
            out.append((prov, Binding(prov.provider_id, interface, version, contract)))

        def rank(item: tuple[Provider, Binding]) -> tuple[Any, ...]:
            prov, _ = item
            pref = preferred.index(prov.provider_id) if prov.provider_id in preferred \
                else len(preferred)
            return (pref, not prov.local, -prov.priority)

        # Stable two-pass sort: newest version first within equal rank, then by id.
        out.sort(key=lambda item: item[0].provider_id)
        out.sort(key=lambda item: version_key(item[1].version), reverse=True)
        out.sort(key=rank)
        return [b for _, b in out]

    def resolve(self, interface: str, spec: str = "*", prefer: Iterable[str] = ()) -> Binding:
        found = self.candidates(interface, spec, prefer)
        if not found:
            raise ServiceError("no active provider", interface=interface, range=spec)
        return found[0]

    # -- calls --------------------------------------------------------------------------
    async def call(self, interface: str, args: Mapping[str, Any], *, spec: str = "*",
                   principal: str = "unspecified", prefer: Iterable[str] = (),
                   timeout_s: float | None = None) -> CallResult:
        bindings = self.candidates(interface, spec, prefer)
        if not bindings:
            raise ServiceError("no active provider", interface=interface, range=spec)
        contract = bindings[0].contract
        if contract.effect is InterfaceEffect.PHYSICAL:
            self._record("service_call_refused", interface, principal=principal,
                         reason="physical_effect")
            raise ServiceError("physical interfaces are reached through runtime admission, "
                               "not direct service calls", interface=interface)
        problems = _check_params(contract.inputs, args, "input")
        if problems:
            raise ServiceError("arguments do not match the interface", interface=interface,
                               problems=problems)
        retry_ok = contract.effect in (InterfaceEffect.NONE, InterfaceEffect.COMPUTE)
        attempts: list[dict[str, Any]] = []
        for b in bindings:
            prov = self._providers[b.provider_id]
            started = self.clock()
            try:
                outputs = dict(await asyncio.wait_for(
                    prov.handler(interface, dict(args)),
                    timeout=timeout_s or self.default_timeout_s))
            except Exception as exc:  # the provider failed; decide whether to try another
                prov.failures += 1
                outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
                attempts.append({"provider": b.provider_id, "outcome": outcome,
                                 "error": repr(exc)})
                self._record("service_call", interface, provider=b.provider_id,
                             version=b.version, principal=principal, outcome=outcome)
                if prov.failures >= self.max_failures:
                    self.set_state(b.provider_id, ProviderState.ERROR,
                                   reason=f"{prov.failures} consecutive failures")
                if not retry_ok:
                    raise ServiceOutcomeUnknown(
                        "a state-changing call failed; its effect is unknown and it is not "
                        "retried on another provider", interface=interface,
                        provider=b.provider_id, error=repr(exc)) from exc
                continue
            prov.failures = 0
            prov.calls += 1
            broken = _check_params(contract.outputs, outputs, "output")
            duration = self.clock() - started
            if broken:
                self.set_state(b.provider_id, ProviderState.ERROR,
                               reason="returned outputs that break the interface")
                attempts.append({"provider": b.provider_id, "outcome": "contract_violation",
                                 "problems": broken})
                self._record("service_call", interface, provider=b.provider_id,
                             version=b.version, principal=principal,
                             outcome="contract_violation", problems=broken)
                if not retry_ok:
                    raise ServiceOutcomeUnknown("provider broke the interface after a "
                                                "state-changing call", interface=interface,
                                                provider=b.provider_id, problems=broken)
                continue
            attempts.append({"provider": b.provider_id, "outcome": "ok"})
            self._record("service_call", interface, provider=b.provider_id,
                         version=b.version, principal=principal, outcome="ok",
                         duration_s=round(duration, 4), fallbacks=len(attempts) - 1)
            return CallResult(interface, b.provider_id, b.version, outputs, duration, attempts)
        raise ServiceError("every provider failed", interface=interface, attempts=attempts)


def _check_params(specs: Iterable[Any], values: Mapping[str, Any], what: str) -> list[str]:
    out = []
    specs = tuple(specs)
    names = {s.name for s in specs}
    for s in specs:
        if s.name not in values:
            if s.required:
                out.append(f"missing {what} {s.name}")
            continue
        problem = s.check(values[s.name])
        if problem:
            out.append(problem)
    if what == "input":
        out.extend(f"unknown input {k}" for k in values if k not in names)
    return out
