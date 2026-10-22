"""Resource reservations with fencing (baseline sections 6 and 11).

Logical ownership (who holds the reservation) and physical occupancy (whether the
resource is actually occupied) are tracked separately: lease expiry or release does
not prove a robot has vacated a station.
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


@contract("resource_reservation", version="1.0")
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
