from __future__ import annotations

import pytest

from agentic_robotics import AuthorityService, Delegation, ManualClock, ResourceManager
from agentic_robotics.contracts import Occupancy, ReservationState
from agentic_robotics.core import AuthorityError, ResourceConflict


def service() -> tuple[ManualClock, AuthorityService]:
    clock = ManualClock()
    return clock, AuthorityService(clock, ["operator:root"])


def root_grant(**kw: object) -> Delegation:
    fields: dict[str, object] = {"delegation_id": "lead", "grantor": "operator:root",
                                 "grantee": "agent:lead", "robots": ("r1", "r2"),
                                 "may_redelegate": True}
    fields.update(kw)
    return Delegation(**fields)  # type: ignore[arg-type]


def test_root_grant_requires_root_principal() -> None:
    _, auth = service()
    with pytest.raises(AuthorityError, match="root principal"):
        auth.grant(root_grant(grantor="agent:rogue"))


def test_redelegation_must_narrow_and_be_allowed() -> None:
    _, auth = service()
    auth.grant(root_grant())
    auth.grant(Delegation(delegation_id="child", grantor="agent:lead", grantee="agent:task",
                          robots=("r1",), parent_id="lead"))
    with pytest.raises(AuthorityError, match="exceeds its parent"):
        auth.grant(Delegation(delegation_id="wide", grantor="agent:lead", grantee="agent:x",
                              parent_id="lead"))
    with pytest.raises(AuthorityError, match="forbids redelegation"):
        auth.grant(Delegation(delegation_id="grandchild", grantor="agent:task",
                              grantee="agent:y", robots=("r1",), parent_id="child"))
    with pytest.raises(AuthorityError, match="parent's grantee"):
        auth.grant(Delegation(delegation_id="forged", grantor="agent:other",
                              grantee="agent:y", robots=("r1",), parent_id="lead"))


def test_revocation_cascades_and_notifies() -> None:
    _, auth = service()
    seen: list[frozenset[str]] = []
    auth.on_revocation(seen.append)
    auth.grant(root_grant())
    auth.grant(Delegation(delegation_id="child", grantor="agent:lead", grantee="agent:task",
                          robots=("r1",), parent_id="lead"))
    revoked = auth.revoke("lead")
    assert revoked == {"lead", "child"}
    assert seen == [revoked]
    with pytest.raises(AuthorityError, match="revoked"):
        auth.check("child", "agent:task", "r1", "navigate_to")


def test_expiry_is_enforced() -> None:
    clock, auth = service()
    auth.grant(root_grant(valid_until=10.0))
    auth.check("lead", "agent:lead", "r1", "any")
    clock.advance(10.0)
    with pytest.raises(AuthorityError, match="not valid now"):
        auth.check("lead", "agent:lead", "r1", "any")


def test_fencing_tokens_increase_and_supersede() -> None:
    rm = ResourceManager(ManualClock())
    a = rm.reserve("station", "agent:a")
    assert rm.reserve("station", "agent:a") == a  # idempotent for the holder
    with pytest.raises(ResourceConflict):
        rm.reserve("station", "agent:b")
    b = rm.reserve("station", "agent:b", preempt=True)
    assert b.fencing_token > a.fencing_token
    assert rm.validate_fence("station", a.fencing_token, "agent:a") == "stale_fencing_token"
    assert rm.validate_fence("station", b.fencing_token, "agent:b") is None
    assert rm.validate_fence("station", b.fencing_token, "agent:a") == "not_reservation_holder"


def test_logical_release_does_not_imply_physical_vacancy() -> None:
    rm = ResourceManager(ManualClock())
    r = rm.reserve("station", "agent:a")
    rm.set_occupancy("station", Occupancy.OCCUPIED)
    released = rm.release(r.reservation_id)
    assert released.state is ReservationState.RELEASED
    assert rm.occupancy("station") is Occupancy.OCCUPIED


def test_lease_expiry_frees_logical_ownership() -> None:
    clock = ManualClock()
    rm = ResourceManager(clock)
    r = rm.reserve("station", "agent:a", lease_s=5.0)
    clock.advance(5.0)
    assert rm.holder("station") is None
    assert rm.validate_fence("station", r.fencing_token, "agent:a") == "no_active_reservation"
    assert rm.reserve("station", "agent:b").fencing_token == r.fencing_token + 1
