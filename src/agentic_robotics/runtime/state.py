"""Command state machine with explicit legal transitions."""

from __future__ import annotations

from ..contracts.execution import CommandState as S
from ..core.errors import InvalidTransition

TRANSITIONS: dict[S, frozenset[S]] = {
    S.SUBMITTED: frozenset({S.ACCEPTED, S.REJECTED}),
    S.ACCEPTED: frozenset({S.EXECUTING, S.CANCELLED, S.REJECTED}),
    S.EXECUTING: frozenset({S.REPORTED_COMPLETE, S.FAILED, S.CANCELLING, S.UNRESOLVED}),
    S.REPORTED_COMPLETE: frozenset({S.VERIFIED, S.FAILED, S.UNRESOLVED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.UNRESOLVED, S.REPORTED_COMPLETE, S.FAILED}),
    # An unresolved outcome is settled only by reconciliation against observations.
    S.UNRESOLVED: frozenset({S.VERIFIED, S.FAILED, S.CANCELLED}),
    S.VERIFIED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.REJECTED: frozenset(),
}


def check_transition(current: S, new: S) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition("illegal command state transition",
                                current=current.value, new=new.value)
