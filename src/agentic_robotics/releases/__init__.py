"""Policies, releases and signed update metadata (ADR 0016).

- :mod:`.policy_slot`: load gate, origin authority, two-step start, checked inference,
  stale-chunk refusal and declared exit mode for learned policies.
- :mod:`.activation`: trial-then-confirm release activation with a security counter.
- :mod:`.tuf`: TUF-style threshold-signed metadata over registry assets, plus an
  Uptane-like director.
- :mod:`.signing`: HMAC (stdlib) and optional Ed25519 (``[signing]`` extra) keys.
- :mod:`.records`: ledger entities for parameter snapshots, episodes, training runs,
  policies and asset cards.
"""

from .activation import ActivationError, ActivationManager
from .policy_slot import LoadOutcome, PolicyError, PolicySlot, SlotState
from .signing import HmacSigner, KeyStore, SigningError, ed25519_available
from .tuf import TrustedSet, TufError

__all__ = [
    "ActivationError",
    "ActivationManager",
    "HmacSigner",
    "KeyStore",
    "LoadOutcome",
    "PolicyError",
    "PolicySlot",
    "SigningError",
    "SlotState",
    "TrustedSet",
    "TufError",
    "ed25519_available",
]

__requirements__ = ("REQ10", "REQ16", "G14", "G15")
