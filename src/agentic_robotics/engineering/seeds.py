"""Seeds: one root, derived per-episode and per-env seeds, and an honest source.

Seeds are derived by hashing, not by drawing from a shared generator, so episode ``i``
has the same seed however many episodes run before it (ManiSkill ``_main_seed`` ->
``_episode_seed``). A missing seed is drawn from OS entropy and recorded as ``drawn``;
a backend that seeds no generator yields ``unset``.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence

from ..provenance.model import SeedRecord, SeedSource

__requirements__ = ("F05", "G04")

DERIVATION = "sha256(root/kind/index)[:4] big-endian"


def derive_seed(root: int, kind: str, index: int) -> int:
    """A 32-bit seed for ``kind`` ``index`` under ``root`` (stable across platforms)."""
    digest = hashlib.sha256(f"{root}/{kind}/{index}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def draw_seed() -> int:
    return secrets.randbits(32)


def seed_record(root: int | None, *, episodes: int, envs: int = 1,
                seeded_rngs: Sequence[str] = (), user_supplied: bool = True,
                sample_states: str = "") -> SeedRecord:
    """Build the :class:`SeedRecord` for a run. ``root=None`` draws a root seed."""
    if not seeded_rngs:
        return SeedRecord(root=root, source=SeedSource.UNSET, seeded_rngs=(),
                          sample_states=sample_states)
    source = SeedSource.USER if (root is not None and user_supplied) else (
        SeedSource.DERIVED if root is not None else SeedSource.DRAWN)
    actual = root if root is not None else draw_seed()
    return SeedRecord(
        root=actual, source=source,
        per_episode=tuple(derive_seed(actual, "episode", i) for i in range(episodes)),
        per_env=tuple(derive_seed(actual, "env", i) for i in range(envs)) if envs > 1 else (),
        seeded_rngs=tuple(seeded_rngs), derivation=DERIVATION, sample_states=sample_states)
