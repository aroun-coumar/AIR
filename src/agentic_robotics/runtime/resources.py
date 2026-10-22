"""Exclusive resource ownership with fencing tokens (baseline section 11).

Each new reservation of a resource receives a strictly larger fencing token. The
executor checks the token a command carries; a superseded owner's commands are
rejected even if they arrive late. Logical release and physical occupancy are separate.

Fail closed on physical uncertainty: a lease that runs out while the resource may still
be occupied (OCCUPIED or UNKNOWN) does not free it. It becomes EXPIRED_OCCUPIED, which
keeps blocking other holders while denying the expired holder new commands, until
vacancy is observed. Preemption likewise requires the resource to be known vacant.
(Open-RMF force-releases idle mutex groups while robots are still in them; AIR does not.)

Leases are **scoped loans** (the ros2_control ``LoanedCommandInterface`` pattern):
:meth:`ResourceManager.acquire` takes a set of resources all-or-nothing and returns a
:class:`Lease` carrying a secret *invocation token*. Only that token can release what the
lease owns; evidence records a digest of it, never the token. Before anything is
granted, pluggable :class:`ResourceModule` s may veto (an area already occupied) or
require preparation (a door not yet open, a lift not at the floor, a mode switch the
hardware has not acknowledged). Nothing is granted until every resource is ready.

:class:`Claim` adds the openTCS ``Scheduler`` pattern: *claim* declares the ordered
sequence of resource sets a holder will need; *allocate* takes the next set when it is
actually needed; steps are released incrementally once left.

:meth:`ResourceManager.reserve` remains as the unscoped, single-resource primitive
(releasable by id) for existing callers.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts.resources import Occupancy, ReservationState, ResourceReservation
from ..core.clock import Clock
from ..core.errors import ResourceConflict
from ..core.ids import new_id

__requirements__ = ("REQ06", "REQ09", "G06")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# -- modules -----------------------------------------------------------------------------
@dataclass(frozen=True)
class AllocationRequest:
    resource: str
    holder: str
    mode: str | None = None
    claim_id: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


class ResourceModule(Protocol):
    """Pluggable allocation policy (openTCS scheduler modules).

    ``may_allocate`` returns a veto reason or ``None``; ``prepare`` returns a pending
    reason (not ready yet, try again) or ``None``; ``perform`` runs once every request
    of an acquisition is prepared and returns a failure reason or ``None``; ``released``
    is told when a reservation the module applies to is released.
    """

    name: str

    def applies_to(self, resource: str) -> bool: ...

    def may_allocate(self, request: AllocationRequest,
                     manager: ResourceManager) -> str | None: ...

    def prepare(self, request: AllocationRequest, manager: ResourceManager) -> str | None: ...

    def perform(self, request: AllocationRequest, manager: ResourceManager) -> str | None: ...

    def released(self, resource: str, holder: str, manager: ResourceManager) -> None: ...


class BaseModule:
    """Convenience base: allows everything; override what you need."""

    name = "module"
    handles_mode = False

    def applies_to(self, resource: str) -> bool:
        return False

    def may_allocate(self, request: AllocationRequest,
                     manager: ResourceManager) -> str | None:
        return None

    def prepare(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        return None

    def perform(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        return None

    def released(self, resource: str, holder: str, manager: ResourceManager) -> None:
        return None


class DoorController(Protocol):
    def is_open(self, door: str) -> bool | None: ...

    def request_open(self, door: str) -> None: ...

    def request_close(self, door: str) -> None: ...


class DoorModule(BaseModule):
    """A door is granted only once it is *observed* open (unknown counts as closed); it
    is asked to close when released."""

    name = "door"

    def __init__(self, doors: Iterable[str], controller: DoorController) -> None:
        self.doors = frozenset(doors)
        self.controller = controller

    def applies_to(self, resource: str) -> bool:
        return resource in self.doors

    def prepare(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        if self.controller.is_open(request.resource) is True:
            return None
        self.controller.request_open(request.resource)
        return "door_not_open"

    def released(self, resource: str, holder: str, manager: ResourceManager) -> None:
        self.controller.request_close(resource)


class LiftController(Protocol):
    def car_floor(self, lift: str) -> str | None: ...

    def doors_open(self, lift: str) -> bool: ...

    def call(self, lift: str, floor: str) -> None: ...


class LiftModule(BaseModule):
    """A lift is granted only when its car is observed at the requested floor
    (``params={"floor": ...}``) with its doors open."""

    name = "lift"

    def __init__(self, lifts: Iterable[str], controller: LiftController) -> None:
        self.lifts = frozenset(lifts)
        self.controller = controller

    def applies_to(self, resource: str) -> bool:
        return resource in self.lifts

    def may_allocate(self, request: AllocationRequest,
                     manager: ResourceManager) -> str | None:
        return None if "floor" in request.params else "lift_floor_required"

    def prepare(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        floor = str(request.params["floor"])
        if (self.controller.car_floor(request.resource) == floor
                and self.controller.doors_open(request.resource)):
            return None
        self.controller.call(request.resource, floor)
        return "lift_not_at_floor"


class AreaModule(BaseModule):
    """Mutual-exclusion areas (openTCS block module): the members of one group can be
    held by only one holder at a time."""

    name = "area"

    def __init__(self, groups: Mapping[str, Iterable[str]]) -> None:
        self.groups = {g: frozenset(m) for g, m in groups.items()}

    def applies_to(self, resource: str) -> bool:
        return any(resource in m for m in self.groups.values())

    def may_allocate(self, request: AllocationRequest,
                     manager: ResourceManager) -> str | None:
        for group, members in self.groups.items():
            if request.resource not in members:
                continue
            for other in sorted(members - {request.resource}):
                held = manager.holder(other)
                if held is not None and held.holder != request.holder:
                    return f"area_held:{group}:{held.holder}"
        return None


@dataclass(frozen=True)
class ModeAck:
    acknowledged: bool
    reason: str = ""


class ModeSwitcher(Protocol):
    """Hardware side of a mode switch (ros2_control prepare/perform switch)."""

    def prepare(self, resource: str, mode: str, holder: str) -> ModeAck: ...

    def perform(self, resource: str, mode: str, holder: str) -> ModeAck: ...


class ModeSwitchModule(BaseModule):
    """A requested mode must be prepared *and* acknowledged by the hardware before the
    reservation is granted. A resource without a mode-switch module cannot be granted
    with a mode at all (fail closed)."""

    name = "mode_switch"
    handles_mode = True

    def __init__(self, resources: Iterable[str], switcher: ModeSwitcher) -> None:
        self.resources = frozenset(resources)
        self.switcher = switcher

    def applies_to(self, resource: str) -> bool:
        return resource in self.resources

    def prepare(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        if request.mode is None:
            return None
        ack = self.switcher.prepare(request.resource, request.mode, request.holder)
        return None if ack.acknowledged else f"mode_not_prepared:{ack.reason}"

    def perform(self, request: AllocationRequest, manager: ResourceManager) -> str | None:
        if request.mode is None:
            return None
        ack = self.switcher.perform(request.resource, request.mode, request.holder)
        return None if ack.acknowledged else f"mode_not_acknowledged:{ack.reason}"


# -- leases and claims -------------------------------------------------------------------
class Lease:
    """A scoped loan of one or more resources.

    Treat it as move-only: whoever has the lease (and so its token) may release what it
    owns; nobody else can. Use it as a context manager to release on scope exit.
    Resources the holder already held when the lease was taken are *shared* into it: the
    reservation is released only when every lease sharing it has released.
    """

    def __init__(self, manager: ResourceManager, holder: str, token: str,
                 reservation_ids: tuple[str, ...], claim_id: str | None) -> None:
        self._manager = manager
        self._token = token
        self.holder = holder
        self.loan = _digest(token)
        self.reservation_ids = reservation_ids
        self.claim_id = claim_id

    @property
    def token(self) -> str:
        """The invocation token. Keep it with the holder; never log it."""
        return self._token

    @property
    def reservations(self) -> list[ResourceReservation]:
        return [self._manager.reservation(r) for r in self.reservation_ids]

    @property
    def fencing(self) -> dict[str, int]:
        return {r.resource: r.fencing_token for r in self.reservations}

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(r.resource for r in self.reservations)

    @property
    def active(self) -> bool:
        return any(self._manager.shares(r, self.loan) for r in self.reservation_ids)

    def release(self, resources: Iterable[str] | None = None) -> list[ResourceReservation]:
        wanted = None if resources is None else set(resources)
        out = []
        for rid in self.reservation_ids:
            r = self._manager.reservation(rid)
            if wanted is not None and r.resource not in wanted:
                continue
            if self._manager.shares(rid, self.loan):
                out.append(self._manager.release(rid, token=self._token))
        return out

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __repr__(self) -> str:  # never show the token
        return f"Lease(holder={self.holder!r}, loan={self.loan!r}, resources={self.resources})"


class Claim:
    """Claim-then-allocate (openTCS ``Scheduler``): declare the ordered resource sets a
    holder will need, then allocate them one step at a time and release steps once
    left. Allocation of a step is all-or-nothing and goes through the modules."""

    def __init__(self, manager: ResourceManager, holder: str,
                 steps: tuple[tuple[str, ...], ...]) -> None:
        self._manager = manager
        self.claim_id = new_id("clm")
        self.holder = holder
        self.steps = steps
        self.next_step = 0
        self.leases: dict[int, Lease] = {}
        self.withdrawn = False
        self.last_conflict: dict[str, Any] | None = None

    @property
    def remaining(self) -> tuple[tuple[str, ...], ...]:
        return self.steps[self.next_step:]

    @property
    def claimed(self) -> frozenset[str]:
        return frozenset(r for step in self.remaining for r in step)

    def allocate_next(self, *, lease_s: float | None = None, mode: str | None = None,
                      params: Mapping[str, Any] | None = None) -> Lease:
        if self.withdrawn:
            raise ResourceConflict("claim was withdrawn", reason="claim_withdrawn",
                                   claim_id=self.claim_id)
        if self.next_step >= len(self.steps):
            raise ResourceConflict("claim has no further steps", reason="claim_exhausted",
                                   claim_id=self.claim_id)
        lease = self._manager.acquire(self.steps[self.next_step], self.holder,
                                      lease_s=lease_s, mode=mode, params=params,
                                      claim_id=self.claim_id)
        self.leases[self.next_step] = lease
        self.next_step += 1
        return lease

    def try_allocate_next(self, **kwargs: Any) -> Lease | None:
        """Allocate the next step if possible now, else ``None`` (see ``last_conflict``)."""
        try:
            lease = self.allocate_next(**kwargs)
        except ResourceConflict as exc:
            self.last_conflict = {"message": exc.message, **exc.details}
            return None
        self.last_conflict = None
        return lease

    def release_step(self, index: int) -> list[ResourceReservation]:
        """Release a step that has been left. Refused while a resource it owns is
        observed OCCUPIED (logical release never claims physical vacancy)."""
        lease = self.leases.get(index)
        if lease is None:
            return []
        occupied = [r for r in lease.resources
                    if self._manager.occupancy(r) is Occupancy.OCCUPIED]
        if occupied:
            raise ResourceConflict("resource is still physically occupied",
                                   reason="occupied", resources=occupied,
                                   claim_id=self.claim_id)
        released = lease.release()
        del self.leases[index]
        return released

    def withdraw(self) -> list[str]:
        """Drop the claim (openTCS WITHDRAWN). Steps still observed occupied stay held;
        their resources are returned."""
        self.withdrawn = True
        kept: list[str] = []
        for index in sorted(self.leases):
            try:
                self.release_step(index)
            except ResourceConflict as exc:
                kept.extend(exc.details.get("resources", []))
        self._manager.drop_claim(self.claim_id)
        return kept


# -- manager -------------------------------------------------------------------------------
class ResourceManager:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._tokens: dict[str, int] = {}
        self._current: dict[str, ResourceReservation] = {}
        self._occupancy: dict[str, Occupancy] = {}
        self._by_id: dict[str, ResourceReservation] = {}
        self._listeners: list[Callable[[ResourceReservation], object]] = []
        self._owners: dict[str, set[str]] = {}     # reservation_id -> loan digests
        self.modules: list[ResourceModule] = []
        self._claims: dict[str, Claim] = {}

    def on_change(self, listener: Callable[[ResourceReservation], object]) -> None:
        self._listeners.append(listener)

    def add_module(self, module: ResourceModule) -> None:
        self.modules.append(module)

    # -- unscoped primitive -------------------------------------------------------------
    def reserve(self, resource: str, holder: str, lease_s: float | None = None,
                preempt: bool = False) -> ResourceReservation:
        self._expire(resource)
        current = self._current.get(resource)
        if current is not None and current.holder == holder and \
                current.state is ReservationState.HELD:
            return current
        self._check(resource, holder, preempt)
        request = AllocationRequest(resource, holder)
        self._vet([request])
        self._prepare([request])
        return self._grant(resource, holder, lease_s)

    # -- scoped loans -------------------------------------------------------------------
    def acquire(self, resources: Iterable[str], holder: str, *, lease_s: float | None = None,
                mode: str | None = None, params: Mapping[str, Any] | None = None,
                claim_id: str | None = None) -> Lease:
        """Take every resource or none (sorted, so concurrent acquirers cannot form a
        circular wait). Raises :class:`ResourceConflict` without changing anything if any
        resource is held by someone else, vetoed, or not yet prepared."""
        names = sorted(set(resources))
        shared: dict[str, ResourceReservation] = {}
        for name in names:
            self._expire(name)
            current = self._current.get(name)
            if current is not None and current.holder == holder and \
                    current.state is ReservationState.HELD:
                shared[name] = current
            else:
                self._check(name, holder, preempt=False)
        requests = [AllocationRequest(n, holder, mode, claim_id, dict(params or {}))
                    for n in names
                    if n not in shared or (mode is not None and shared[n].mode != mode)]
        if mode is not None:
            unsupported = [r.resource for r in requests
                           if not any(getattr(m, "handles_mode", False)
                                      and m.applies_to(r.resource) for m in self.modules)]
            if unsupported:
                raise ResourceConflict("no module can switch this resource's mode",
                                       reason="mode_unsupported", resources=unsupported,
                                       mode=mode)
        self._vet(requests)
        self._prepare(requests)
        performed: list[str] = []
        for req in requests:
            for module in self._applicable(req.resource):
                failure = module.perform(req, self)
                if failure:
                    raise ResourceConflict("allocation could not be performed",
                                           reason="perform_failed", resource=req.resource,
                                           module=module.name, detail=failure,
                                           performed=performed)
            performed.append(req.resource)

        token = secrets.token_hex(16)
        loan = _digest(token)
        ids: list[str] = []
        for name in names:
            if name in shared:
                r = shared[name]
                if r.reservation_id in self._owners:
                    self._owners[r.reservation_id].add(loan)
                if mode is not None and r.mode != mode:
                    r = r.evolve(mode=mode)
                    self._current[name] = r
                    self._set(r)
                ids.append(r.reservation_id)
            else:
                r = self._grant(name, holder, lease_s, loan=loan, claim_id=claim_id,
                                mode=mode)
                self._owners[r.reservation_id] = {loan}
                ids.append(r.reservation_id)
        return Lease(self, holder, token, tuple(ids), claim_id)

    def claim(self, holder: str, steps: Iterable[Iterable[str]]) -> Claim:
        c = Claim(self, holder, tuple(tuple(sorted(set(s))) for s in steps))
        self._claims[c.claim_id] = c
        return c

    def claims_on(self, resource: str) -> list[Claim]:
        """Claims that declare a future need for ``resource`` (scheduler visibility)."""
        return [c for c in self._claims.values() if resource in c.claimed]

    def drop_claim(self, claim_id: str) -> None:
        self._claims.pop(claim_id, None)

    def shares(self, reservation_id: str, loan: str) -> bool:
        return loan in self._owners.get(reservation_id, set())

    def reservation(self, reservation_id: str) -> ResourceReservation:
        r = self._by_id.get(reservation_id)
        if r is None:
            raise ResourceConflict("unknown reservation", reservation_id=reservation_id)
        return r

    def release(self, reservation_id: str, token: str | None = None) -> ResourceReservation:
        """Release a reservation. A loan (from :meth:`acquire`) needs the invocation token
        of a lease that shares it; it is freed when the last sharing lease releases."""
        r = self.reservation(reservation_id)
        owners = self._owners.get(reservation_id)
        if owners is not None:
            loan = _digest(token) if token is not None else None
            if loan is None or loan not in owners:
                raise ResourceConflict("only the holder's invocation token can release a loan",
                                       reason="not_loan_holder", resource=r.resource,
                                       holder=r.holder)
            owners.discard(loan)
            if owners:
                return r
        if r.state is not ReservationState.HELD:
            return r
        released = r.evolve(state=ReservationState.RELEASED)
        if self._current.get(r.resource) == r:
            del self._current[r.resource]
        self._set(released)
        for module in self._applicable(r.resource):
            module.released(r.resource, r.holder, self)
        return released

    # -- queries ------------------------------------------------------------------------
    def held_by(self, holder: str) -> list[ResourceReservation]:
        for resource in list(self._current):
            self._expire(resource)
        return [r for r in self._current.values() if r.holder == holder]

    def holder(self, resource: str) -> ResourceReservation | None:
        self._expire(resource)
        return self._current.get(resource)

    def current_token(self, resource: str) -> int:
        return self._tokens.get(resource, 0)

    def validate_fence(self, resource: str, token: int | None, holder: str) -> str | None:
        """Return a rejection reason, or ``None`` if ``holder`` with ``token`` may act."""
        self._expire(resource)
        current = self._current.get(resource)
        if token is None:
            return "missing_fencing_token"
        if current is None:
            return "no_active_reservation"
        if token != current.fencing_token:
            return "stale_fencing_token"
        if current.holder != holder:
            return "not_reservation_holder"
        if current.state is ReservationState.EXPIRED_OCCUPIED:
            return "lease_expired"
        return None

    def set_occupancy(self, resource: str, occupancy: Occupancy) -> None:
        self._occupancy[resource] = occupancy
        current = self._current.get(resource)
        if current is None:
            return
        if current.state is ReservationState.EXPIRED_OCCUPIED and occupancy is Occupancy.FREE:
            del self._current[resource]
            self._set(current.evolve(state=ReservationState.EXPIRED,
                                     physical_occupancy=occupancy,
                                     last_confirmed_at=self.clock.now()))
            return
        updated = current.evolve(physical_occupancy=occupancy,
                                 last_confirmed_at=self.clock.now())
        self._current[resource] = updated
        self._set(updated)

    def occupancy(self, resource: str) -> Occupancy:
        return self._occupancy.get(resource, Occupancy.UNKNOWN)

    # -- internals ----------------------------------------------------------------------
    def _check(self, resource: str, holder: str, preempt: bool) -> None:
        """Raise if ``holder`` cannot take ``resource`` now. Changes nothing."""
        current = self._current.get(resource)
        if current is None or current.holder == holder:
            return
        if current.state is ReservationState.EXPIRED_OCCUPIED:
            raise ResourceConflict(
                "lease expired but the resource may still be physically occupied",
                reason="expired_occupied", resource=resource,
                holder=current.holder, occupancy=self.occupancy(resource).value)
        if not preempt:
            raise ResourceConflict("resource is held by another participant",
                                   reason="held", resource=resource, holder=current.holder)
        if self.occupancy(resource) is not Occupancy.FREE:
            raise ResourceConflict(
                "preemption requires the resource to be known vacant",
                reason="preempt_requires_vacancy", resource=resource,
                holder=current.holder, occupancy=self.occupancy(resource).value)

    def _applicable(self, resource: str) -> list[ResourceModule]:
        return [m for m in self.modules if m.applies_to(resource)]

    def _vet(self, requests: list[AllocationRequest]) -> None:
        for req in requests:
            for module in self._applicable(req.resource):
                veto = module.may_allocate(req, self)
                if veto:
                    raise ResourceConflict("allocation vetoed by a resource module",
                                           reason="vetoed", resource=req.resource,
                                           module=module.name, detail=veto)

    def _prepare(self, requests: list[AllocationRequest]) -> None:
        pending: dict[str, str] = {}
        for req in requests:
            for module in self._applicable(req.resource):
                why = module.prepare(req, self)
                if why:
                    pending[req.resource] = why
        if pending:
            raise ResourceConflict("allocation is not prepared yet", reason="prepare_pending",
                                   pending=pending)

    def _grant(self, resource: str, holder: str, lease_s: float | None, *,
               loan: str | None = None, claim_id: str | None = None,
               mode: str | None = None) -> ResourceReservation:
        current = self._current.get(resource)
        if current is not None:
            # Same holder renewing an expired-but-occupied lease, or a vacant preemption.
            self._owners.pop(current.reservation_id, None)
            self._set(current.evolve(state=ReservationState.SUPERSEDED))
        token = self._tokens.get(resource, 0) + 1
        self._tokens[resource] = token
        now = self.clock.now()
        r = ResourceReservation(
            reservation_id=new_id("rsv"), resource=resource, holder=holder,
            fencing_token=token,
            lease_expires_at=None if lease_s is None else now + lease_s,
            physical_occupancy=self._occupancy.get(resource, Occupancy.UNKNOWN),
            loan=loan, claim_id=claim_id, mode=mode,
        )
        self._current[resource] = r
        self._set(r)
        return r

    def _expire(self, resource: str) -> None:
        current = self._current.get(resource)
        if not (current and current.state is ReservationState.HELD
                and current.lease_expires_at is not None
                and self.clock.now() >= current.lease_expires_at):
            return
        if self.occupancy(resource) is Occupancy.FREE:
            del self._current[resource]
            self._set(current.evolve(state=ReservationState.EXPIRED))
        else:
            blocked = current.evolve(state=ReservationState.EXPIRED_OCCUPIED)
            self._current[resource] = blocked
            self._set(blocked)

    def _set(self, r: ResourceReservation) -> None:
        self._by_id[r.reservation_id] = r
        for listener in list(self._listeners):
            listener(r)
