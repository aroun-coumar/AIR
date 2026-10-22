"""Authority service: grants, checks and revokes delegations (baseline section 17).

Every change bumps a monotonic ``revision``. Commands cite the revision they were
issued under; a command issued before a relevant change is rejected as stale.
Revocation cascades to dependent delegations and notifies listeners so the runtime
can stop work that no longer has authority.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..contracts.authority import Delegation
from ..core.clock import Clock
from ..core.errors import AuthorityError

RevocationListener = Callable[[frozenset[str]], None]


class AuthorityService:
    def __init__(self, clock: Clock, root_principals: Iterable[str]) -> None:
        self.clock = clock
        self.root_principals = frozenset(root_principals)
        self._delegations: dict[str, Delegation] = {}
        self._revoked: set[str] = set()
        self._changed_at: dict[str, int] = {}
        self._revision = 0
        self._listeners: list[RevocationListener] = []

    @property
    def revision(self) -> int:
        return self._revision

    def on_revocation(self, listener: RevocationListener) -> None:
        self._listeners.append(listener)

    # -- grant / revoke ------------------------------------------------------------
    def grant(self, delegation: Delegation) -> Delegation:
        d = delegation
        if d.delegation_id in self._delegations:
            raise AuthorityError("delegation id already used", delegation_id=d.delegation_id)
        if d.parent_id is None:
            if d.grantor not in self.root_principals:
                raise AuthorityError("grantor is not a root principal", grantor=d.grantor)
        else:
            parent = self._active(d.parent_id)
            if parent.grantee != d.grantor:
                raise AuthorityError("only the parent's grantee may redelegate",
                                     parent=parent.delegation_id, grantor=d.grantor)
            if not parent.may_redelegate:
                raise AuthorityError("parent delegation forbids redelegation",
                                     parent=parent.delegation_id)
            problems = d.within(parent)
            if problems:
                raise AuthorityError("delegation exceeds its parent",
                                     delegation_id=d.delegation_id, problems=problems)
        self._revision += 1
        self._delegations[d.delegation_id] = d
        self._changed_at[d.delegation_id] = self._revision
        return d

    def revoke(self, delegation_id: str, reason: str = "") -> frozenset[str]:
        """Revoke a delegation and everything derived from it. Returns revoked ids."""
        if delegation_id not in self._delegations:
            raise AuthorityError("unknown delegation", delegation_id=delegation_id)
        revoked = set()
        frontier = [delegation_id]
        while frontier:
            current = frontier.pop()
            if current in self._revoked:
                continue
            revoked.add(current)
            frontier.extend(d.delegation_id for d in self._delegations.values()
                            if d.parent_id == current)
        if not revoked:
            return frozenset()
        self._revision += 1
        for r in revoked:
            self._revoked.add(r)
            self._changed_at[r] = self._revision
        result = frozenset(revoked)
        for listener in list(self._listeners):
            listener(result)
        return result

    # -- queries -------------------------------------------------------------------
    def get(self, delegation_id: str) -> Delegation:
        try:
            return self._delegations[delegation_id]
        except KeyError:
            raise AuthorityError("unknown delegation", delegation_id=delegation_id) from None

    def is_active(self, delegation_id: str) -> bool:
        try:
            self._active(delegation_id)
        except AuthorityError:
            return False
        return True

    def chain(self, delegation_id: str) -> list[Delegation]:
        out = []
        current: str | None = delegation_id
        while current is not None:
            d = self.get(current)
            out.append(d)
            current = d.parent_id
        return out

    def check(self, delegation_id: str, principal: str, robot_id: str, skill_id: str,
              operation: str = "execute", issued_under_revision: int | None = None) -> None:
        """Raise :class:`AuthorityError` unless ``principal`` may perform the operation."""
        now = self.clock.now()
        chain = self.chain(delegation_id)
        leaf = chain[0]
        if leaf.grantee != principal:
            raise AuthorityError("delegation was granted to a different principal",
                                 reason="wrong_principal", grantee=leaf.grantee,
                                 principal=principal)
        for d in chain:
            if d.delegation_id in self._revoked:
                raise AuthorityError("delegation has been revoked", reason="revoked",
                                     delegation_id=d.delegation_id)
            if not d.valid_at(now):
                raise AuthorityError("delegation is not valid now", reason="expired",
                                     delegation_id=d.delegation_id)
            if not d.covers(robot_id, skill_id, operation):
                raise AuthorityError("operation is outside delegated scope",
                                     reason="out_of_scope", delegation_id=d.delegation_id,
                                     robot_id=robot_id, skill_id=skill_id)
        if issued_under_revision is not None:
            latest = max(self._changed_at[d.delegation_id] for d in chain)
            if latest > issued_under_revision:
                raise AuthorityError("command was issued under a stale authority revision",
                                     reason="stale_revision", issued=issued_under_revision,
                                     current=latest)

    def _active(self, delegation_id: str) -> Delegation:
        d = self.get(delegation_id)
        if delegation_id in self._revoked:
            raise AuthorityError("delegation has been revoked", delegation_id=delegation_id)
        if not d.valid_at(self.clock.now()):
            raise AuthorityError("delegation is not valid now", delegation_id=delegation_id)
        if d.parent_id is not None:
            self._active(d.parent_id)
        return d
