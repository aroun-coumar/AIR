"""Command state machine with explicit legal transitions."""

from __future__ import annotations

from ..contracts.execution import CommandState as S
from ..core.errors import InvalidTransition

__requirements__ = ("REQ07",)

TRANSITIONS: dict[S, frozenset[S]] = {
    S.SUBMITTED: frozenset({S.ACCEPTED, S.REJECTED, S.AWAITING_APPROVAL}),
    # A human decision is bound to the command id; nothing runs while it is pending.
    S.AWAITING_APPROVAL: frozenset({S.ACCEPTED, S.REJECTED, S.CANCELLED}),
    S.ACCEPTED: frozenset({S.EXECUTING, S.CANCELLED, S.REJECTED, S.SKIPPED}),
    S.EXECUTING: frozenset({S.REPORTED_COMPLETE, S.FAILED, S.CANCELLING, S.UNRESOLVED,
                            S.PAUSED, S.KILLED}),
    S.PAUSED: frozenset({S.EXECUTING, S.CANCELLING, S.KILLED, S.UNRESOLVED, S.FAILED,
                         S.REPORTED_COMPLETE}),
    S.REPORTED_COMPLETE: frozenset({S.VERIFIED, S.APPROXIMATE, S.FAILED, S.UNRESOLVED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.UNRESOLVED, S.REPORTED_COMPLETE, S.FAILED,
                             S.KILLED}),
    # An unresolved outcome is settled only by reconciliation against observations.
    S.UNRESOLVED: frozenset({S.VERIFIED, S.APPROXIMATE, S.FAILED, S.CANCELLED}),
    S.VERIFIED: frozenset(),
    S.APPROXIMATE: frozenset(),
    S.SKIPPED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.KILLED: frozenset(),
    S.REJECTED: frozenset(),
}


def check_transition(current: S, new: S) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition("illegal command state transition",
                                current=current.value, new=new.value)
