"""Resource reservations with fencing (baseline sections 6 and 11).

Logical ownership (who holds the reservation) and physical occupancy (whether the
resource is actually occupied) are tracked separately: lease expiry or release does
not prove a robot has vacated a station.

Version 1.2 adds the scoped-loan fields: ``loan`` (a digest of the invocation token of
the lease that owns the reservation; the token itself never appears in evidence),
``claim_id`` (the claim sequence it was allocated from) and ``mode`` (the operating mode
the resource acknowledged before the reservation was granted).
"""

from __future__ import annotations

from enum import StrEnum

from ..core.schema import Contract, contract

__requirements__ = ("REQ01", "REQ06", "REQ09")


class Occupancy(StrEnum):
    UNKNOWN = "unknown"
    FREE = "free"
    OCCUPIED = "occupied"


class ReservationState(StrEnum):
    HELD = "held"
    RELEASED = "released"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    # Lease ran out while the resource may still be occupied: blocks others, grants nothing.
    EXPIRED_OCCUPIED = "expired_occupied"


@contract("resource_reservation", version="1.2")
class ResourceReservation(Contract):
    reservation_id: str
    resource: str
    holder: str
    fencing_token: int
    state: ReservationState = ReservationState.HELD
    exclusive: bool = True
    lease_expires_at: float | None = None
    physical_occupancy: Occupancy = Occupancy.UNKNOWN
    last_confirmed_at: float | None = None
    reconciliation_policy: str = "observe_before_reassign"
    # -- 1.2 ------------------------------------------------------------------------------
    loan: str | None = None
    claim_id: str | None = None
    mode: str | None = None
