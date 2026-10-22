"""Identity helpers.

Identities are plain strings with a short kind prefix (``cmd_``, ``dlg_`` ...) so that
they stay readable in traces and JSON, and remain distinct across kinds.
"""

from __future__ import annotations

import re
import uuid

__requirements__ = ("REQ01",)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def new_id(prefix: str) -> str:
    """Return a fresh, globally unique identifier such as ``cmd_3f2a...``."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def is_valid_id(value: str) -> bool:
    return bool(_ID_PATTERN.match(value))
