"""Leases as scoped loans, all-or-nothing acquisition, claim-then-allocate with door, lift
and area modules, and acknowledged mode switches (ADR 0018)."""

from __future__ import annotations

import pytest

from agentic_robotics import ManualClock, ResourceManager
from agentic_robotics.contracts.resources import Occupancy, ResourceReservation
from agentic_robotics.core import ResourceConflict
from agentic_robotics.runtime.resources import (
    AreaModule,
    DoorModule,
    LiftModule,
    ModeAck,
    ModeSwitchModule,
)

pytestmark = pytest.mark.req("REQ06", "REQ09", "G06")


def manager() -> ResourceManager:
    return ResourceManager(ManualClock())


def test_acquisition_is_all_or_nothing() -> None:
    rm = manager()
    rm.reserve("b", "agent:other")
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["c", "a", "b"], "agent:me")
    assert exc.value.details["reason"] == "held"
    assert rm.holder("a") is None and rm.holder("c") is None   # nothing partially taken
    lease = rm.acquire(["c", "a"], "agent:me")
    assert lease.resources == ("a", "c")                        # sorted acquisition order
    assert lease.fencing == {"a": 1, "c": 1}


def test_only_the_invocation_token_releases_a_loan() -> None:
    rm = manager()
    lease = rm.acquire(["station"], "agent:me")
    rid = lease.reservation_ids[0]
    with pytest.raises(ResourceConflict) as exc:
        rm.release(rid)                                 # by id alone: refused
    assert exc.value.details["reason"] == "not_loan_holder"
    other = rm.acquire(["elsewhere"], "agent:me")
    with pytest.raises(ResourceConflict):
        rm.release(rid, token=other.token)             # another lease's token: refused
    assert rm.holder("station") is not None
    released = lease.release()
    assert [r.state.value for r in released] == ["released"]
    assert rm.holder("station") is None and not lease.active


def test_lease_evidence_never_contains_the_token() -> None:
    rm = manager()
    lease = rm.acquire(["station"], "agent:me")
    r = lease.reservations[0]
    assert r.loan == lease.loan and lease.token not in r.to_json()
    assert lease.token not in repr(lease)
    assert ResourceReservation.from_dict(r.to_dict()) == r
    old = {"$schema": "ar.resource_reservation/1.1", "reservation_id": "r", "resource": "x",
           "holder": "h", "fencing_token": 1}
    assert ResourceReservation.from_dict(old).loan is None    # 1.1 documents still load


def test_scoped_lease_releases_on_exit_and_shared_holds_are_counted() -> None:
    rm = manager()
    with rm.acquire(["a"], "agent:me") as first:
        second = rm.acquire(["a", "b"], "agent:me")             # shares "a"
        assert second.fencing["a"] == first.fencing["a"]
    assert rm.holder("a") is not None                           # still shared by second
    second.release()
    assert rm.holder("a") is None and rm.holder("b") is None


class Door:
    def __init__(self) -> None:
        self.open: bool | None = None
        self.calls: list[str] = []

    def is_open(self, door: str) -> bool | None:
        return self.open

    def request_open(self, door: str) -> None:
        self.calls.append(f"open {door}")

    def request_close(self, door: str) -> None:
        self.calls.append(f"close {door}")
        self.open = False


def test_claim_then_allocate_with_a_door_module() -> None:
    rm = manager()
    door = Door()
    rm.add_module(DoorModule(["door-1"], door))
    claim = rm.claim("agent:amr", [["door-1", "corridor"], ["dock"]])
    assert claim in rm.claims_on("dock") and claim.claimed == {"door-1", "corridor", "dock"}
    assert claim.try_allocate_next() is None                    # door state unknown
    assert claim.last_conflict is not None
    assert claim.last_conflict["reason"] == "prepare_pending"
    assert rm.holder("corridor") is None and door.calls == ["open door-1"]
    door.open = True
    first = claim.allocate_next()
    assert set(first.resources) == {"door-1", "corridor"}
    assert claim.remaining == (("dock",),)
    second = claim.allocate_next()
    assert second.resources == ("dock",)
    with pytest.raises(ResourceConflict):
        claim.allocate_next()                                    # exhausted
    rm.set_occupancy("corridor", Occupancy.OCCUPIED)
    with pytest.raises(ResourceConflict) as exc:
        claim.release_step(0)                                    # still in the corridor
    assert exc.value.details["reason"] == "occupied"
    rm.set_occupancy("corridor", Occupancy.FREE)
    claim.release_step(0)
    assert rm.holder("door-1") is None and door.calls[-1] == "close door-1"
    assert claim.withdraw() == [] and rm.claims_on("dock") == []
    assert rm.holder("dock") is None


class Lift:
    def __init__(self) -> None:
        self.floor = "1"
        self.doors = False
        self.called: list[str] = []

    def car_floor(self, lift: str) -> str | None:
        return self.floor

    def doors_open(self, lift: str) -> bool:
        return self.doors

    def call(self, lift: str, floor: str) -> None:
        self.called.append(floor)


def test_lift_is_granted_only_at_the_requested_floor() -> None:
    rm = manager()
    lift = Lift()
    rm.add_module(LiftModule(["lift-1"], lift))
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["lift-1"], "agent:amr")
    assert exc.value.details["detail"] == "lift_floor_required"
    with pytest.raises(ResourceConflict):
        rm.acquire(["lift-1"], "agent:amr", params={"floor": "2"})
    assert lift.called == ["2"] and rm.holder("lift-1") is None
    lift.floor, lift.doors = "2", True
    assert rm.acquire(["lift-1"], "agent:amr", params={"floor": "2"}).resources == ("lift-1",)


def test_area_module_excludes_other_holders() -> None:
    rm = manager()
    rm.add_module(AreaModule({"aisle-3": ["cell-a", "cell-b"]}))
    rm.acquire(["cell-a"], "agent:x")
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["cell-b", "dock"], "agent:y")
    assert exc.value.details["detail"] == "area_held:aisle-3:agent:x"
    assert rm.holder("dock") is None                             # all or nothing
    with pytest.raises(ResourceConflict):
        rm.reserve("cell-b", "agent:y")                          # unscoped path too
    assert rm.acquire(["cell-b"], "agent:x").resources == ("cell-b",)


class Switcher:
    def __init__(self, prepare: bool = True, perform: bool = True) -> None:
        self.ok_prepare, self.ok_perform = prepare, perform
        self.performed: list[tuple[str, str]] = []

    def prepare(self, resource: str, mode: str, holder: str) -> ModeAck:
        return ModeAck(self.ok_prepare, "" if self.ok_prepare else "interface busy")

    def perform(self, resource: str, mode: str, holder: str) -> ModeAck:
        if self.ok_perform:
            self.performed.append((resource, mode))
        return ModeAck(self.ok_perform, "" if self.ok_perform else "no hardware ack")


def test_mode_switch_must_be_acknowledged_before_the_claim_is_granted() -> None:
    rm = manager()
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["arm.joints"], "agent:a", mode="velocity")
    assert exc.value.details["reason"] == "mode_unsupported"     # fail closed
    switcher = Switcher(prepare=False)
    rm.add_module(ModeSwitchModule(["arm.joints"], switcher))
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["arm.joints"], "agent:a", mode="velocity")
    assert exc.value.details["reason"] == "prepare_pending"
    switcher.ok_prepare, switcher.ok_perform = True, False
    with pytest.raises(ResourceConflict) as exc:
        rm.acquire(["arm.joints"], "agent:a", mode="velocity")
    assert exc.value.details["reason"] == "perform_failed"
    assert rm.holder("arm.joints") is None
    switcher.ok_perform = True
    lease = rm.acquire(["arm.joints"], "agent:a", mode="velocity")
    assert lease.reservations[0].mode == "velocity"
    assert switcher.performed == [("arm.joints", "velocity")]
    assert rm.acquire(["arm.joints"], "agent:a", mode="velocity").reservations[0].mode == \
        "velocity"                                               # same mode: no new switch
    assert switcher.performed == [("arm.joints", "velocity")]
