"""Release activation by trial-then-confirm (pattern from mcuboot's test/confirm swap).

States of one robot slot::

    IDLE/CONFIRMED --stage--> STAGED --boot--> TRIAL --confirm(observed checks)--> CONFIRMED
                                                 |
                                                 +--boot again / deadline passed--> revert

- A staged release becomes active only on the next boot, **provisionally**.
- It confirms itself only from *observed* results of every check the release names.
  A missing or failed check is not a confirmation; there is no "confirm anyway".
- If the robot boots again while still in trial, or the confirm deadline passes, the
  previous release is restored automatically (revert), and that is recorded.
- A monotonic **security counter** blocks downgrades: a release whose counter is below
  the stored one is refused at stage time. The stored counter rises only when a release
  confirms (as in mcuboot, so an unconfirmed image cannot lock out the old one).

Every transition is a trace event, and with a project also a ledger entry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..contracts.release import (
    ActivationEvent,
    ActivationStateKind,
    Release,
    ReleaseActivation,
)
from ..core.errors import FrameworkError

__requirements__ = ("REQ16", "F04", "G14", "G15", "D08", "D17")


class ActivationError(FrameworkError):
    code = "activation_error"

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message, reason=reason, **details)
        self.reason = reason


class ActivationManager:
    """Activation state for one ``(robot_id, slot)``, optionally persisted to a file."""

    def __init__(self, robot_id: str, slot: str, *, clock: Callable[[], float],
                 path: str | Path | None = None, trace: Any | None = None,
                 project: Any | None = None) -> None:
        self.clock = clock
        self.path = Path(path) if path else None
        self.trace = trace
        self.project = project
        if self.path and self.path.exists():
            self.state = ReleaseActivation.load(self.path)
            if (self.state.robot_id, self.state.slot) != (robot_id, slot):
                raise ActivationError("wrong_slot", "state file belongs to another slot",
                                      robot_id=self.state.robot_id, slot=self.state.slot)
        else:
            self.state = ReleaseActivation(robot_id=robot_id, slot=slot)

    # -- transitions ---------------------------------------------------------------------
    def stage(self, release: Release) -> None:
        s = self.state
        if s.state is ActivationStateKind.TRIAL:
            raise ActivationError("in_trial", "a release is on trial; confirm or revert it "
                                  "first", release=s.active.ref if s.active else None)
        if release.security_counter < s.security_counter:
            self._event("refused", release, f"security counter {release.security_counter} "
                        f"< stored {s.security_counter} (downgrade)")
            raise ActivationError("downgrade", "release security counter is below the "
                                  "stored counter", release=release.ref,
                                  counter=release.security_counter, stored=s.security_counter)
        self._set(state=ActivationStateKind.STAGED, pending=release)
        self._event("staged", release)

    def boot(self) -> ActivationStateKind:
        """Called at (re)start. Moves a staged release into trial; reverts an unconfirmed
        trial. Returns the resulting state."""
        s = self.state
        if s.state is ActivationStateKind.TRIAL:
            self._revert("rebooted before the release confirmed itself")
        elif s.state is ActivationStateKind.STAGED and s.pending is not None:
            self._set(state=ActivationStateKind.TRIAL, previous=s.active, active=s.pending,
                      pending=None, trial_started_at=self.clock(), trial_boots=1)
            self._event("trial", s.pending)
        return self.state.state

    def confirm(self, observed: Mapping[str, bool]) -> bool:
        """Confirm the trial release if every check it names was observed passing.

        Returns False (and leaves the trial running) when a check is missing or failed;
        raises when there is no trial or the deadline has already passed."""
        s = self.state
        if s.state is not ActivationStateKind.TRIAL or s.active is None:
            raise ActivationError("no_trial", "there is no release on trial")
        if self.check_deadline():
            raise ActivationError("deadline", "the confirm deadline passed; the release "
                                  "was reverted")
        rel = s.active
        missing = [c for c in rel.confirm_checks if c not in observed]
        failed = [c for c in rel.confirm_checks if c in observed and observed[c] is not True]
        if missing or failed:
            self._event("confirm_refused", rel,
                        f"missing={missing} failed={failed}")
            return False
        self._set(state=ActivationStateKind.CONFIRMED,
                  security_counter=max(s.security_counter, rel.security_counter),
                  trial_started_at=None, trial_boots=0)
        self._event("confirmed", rel, ",".join(sorted(rel.confirm_checks)))
        return True

    def check_deadline(self) -> bool:
        """Revert if the trial outlived ``confirm_within_s``. Returns True if reverted."""
        s = self.state
        if (s.state is ActivationStateKind.TRIAL and s.active is not None
                and s.active.confirm_within_s is not None and s.trial_started_at is not None
                and self.clock() - s.trial_started_at > s.active.confirm_within_s):
            self._revert("confirm deadline passed")
            return True
        return False

    def revert(self, reason: str) -> None:
        if self.state.state is not ActivationStateKind.TRIAL:
            raise ActivationError("no_trial", "only a release on trial can be reverted")
        self._revert(reason)

    # -- internals -----------------------------------------------------------------------
    def _revert(self, reason: str) -> None:
        s = self.state
        failed = s.active
        restored = s.previous
        self._set(state=ActivationStateKind.CONFIRMED if restored else
                  ActivationStateKind.IDLE, active=restored, previous=None,
                  trial_started_at=None, trial_boots=0)
        self._event("reverted", failed, f"{reason}; restored "
                    f"{restored.ref if restored else 'nothing'}")

    def _set(self, **changes: Any) -> None:
        self.state = self.state.evolve(**changes)
        self._save()

    def _event(self, kind: str, release: Release | None, detail: str = "") -> None:
        ref = release.ref if release else ""
        ev = ActivationEvent(t=self.clock(), kind=kind, release=ref, detail=detail)
        self.state = self.state.evolve(history=(*self.state.history, ev))
        self._save()
        subject = f"{self.state.robot_id}/{self.state.slot}"
        data = {"release": ref, "detail": detail, "state": self.state.state.value,
                "security_counter": self.state.security_counter}
        if self.trace is not None:
            self.trace.record(f"release_{kind}", subject, **data)
        if self.project is not None:
            self.project.ledger.append(f"release.{kind}", subject, data)

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.state.save(self.path)
