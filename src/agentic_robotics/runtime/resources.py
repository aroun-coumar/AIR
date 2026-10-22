"""Exclusive resource ownership with fencing tokens (baseline section 11).

Each new reservation of a resource receives a strictly larger fencing token. The
executor checks the token a command carries; a superseded owner's commands are
rejected even if they arrive late. Logical release and physical occupancy are separate.

Fail closed on physical uncertainty: a lease that runs out while the resource may still
be occupied (OCCUPIED or UNKNOWN) does not free it. It becomes EXPIRED_OCCUPIED, which
keeps blocking other holders while denying the expired holder new commands, until
vacancy is observed. Preemption likewise requires the resource to be known vacant.
(Open-RMF force-releases idle mutex groups while robots are still in them; AIR does not.)
"""

from __future__ import annotations

from collections.abc import Callable

from ..contracts.resources import Occupancy, ReservationState, ResourceReservation
from ..core.clock import Clock
from ..core.errors import ResourceConflict
from ..core.ids import new_id

__requirements__ = ("REQ06", "REQ09")


class ResourceManager:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._tokens: dict[str, int] = {}
        self._current: dict[str, ResourceReservation] = {}
        self._occupancy: dict[str, Occupancy] = {}
        self._by_id: dict[str, ResourceReservation] = {}
        self._listeners: list[Callable[[ResourceReservation], object]] = []

    def on_change(self, listener: Callable[[ResourceReservation], object]) -> None:
        self._listeners.append(listener)

    def reserve(self, resource: str, holder: str, lease_s: float | None = None,
                preempt: bool = False) -> ResourceReservation:
        self._expire(resource)
        current = self._current.get(resource)
        if current is not None:
            if current.holder == holder and current.state is ReservationState.HELD:
                return current
            if current.holder != holder:
                if current.state is ReservationState.EXPIRED_OCCUPIED:
                    raise ResourceConflict(
                        "lease expired but the resource may still be physically occupied",
                        reason="expired_occupied", resource=resource,
                        holder=current.holder, occupancy=self.occupancy(resource).value)
                if not preempt:
                    raise ResourceConflict("resource is held by another participant",
                                           reason="held", resource=resource,
                                           holder=current.holder)
                if self.occupancy(resource) is not Occupancy.FREE:
                    raise ResourceConflict(
                        "preemption requires the resource to be known vacant",
                        reason="preempt_requires_vacancy", resource=resource,
                        holder=current.holder, occupancy=self.occupancy(resource).value)
            # Same holder renewing an expired-but-occupied lease, or a vacant preemption.
            self._set(current.evolve(state=ReservationState.SUPERSEDED))
        token = self._tokens.get(resource, 0) + 1
        self._tokens[resource] = token
        now = self.clock.now()
        r = ResourceReservation(
            reservation_id=new_id("rsv"), resource=resource, holder=holder,
            fencing_token=token,
            lease_expires_at=None if lease_s is None else now + lease_s,
            physical_occupancy=self._occupancy.get(resource, Occupancy.UNKNOWN),
        )
        self._current[resource] = r
        self._set(r)
        return r

    def release(self, reservation_id: str) -> ResourceReservation:
        r = self._by_id.get(reservation_id)
        if r is None:
            raise ResourceConflict("unknown reservation", reservation_id=reservation_id)
        if r.state is not ReservationState.HELD:
            return r
        released = r.evolve(state=ReservationState.RELEASED)
        if self._current.get(r.resource) == r:
            del self._current[r.resource]
        self._set(released)
        return released

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
