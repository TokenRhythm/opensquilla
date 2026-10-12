"""Kill switch for session-end flush.

Session flush is disabled by default in memory configuration. When memory
configuration explicitly enables it, ``OPENSQUILLA_SESSION_FLUSH`` remains a
global kill switch. Setting the env var to ``0`` or ``false`` restores full
pre-flush behavior:

* ``sessions.reset`` skips snapshot/lock-drain and returns the original
  response shape without a ``flush_receipt`` field.
* Compaction path skips its flush task entirely.

This is the single switch for restoring both latency and payload shape
to pre-PR2.
"""

from __future__ import annotations

import os

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def is_session_flush_enabled() -> bool:
    """Return False iff OPENSQUILLA_SESSION_FLUSH is explicitly disabled."""
    raw = os.environ.get("OPENSQUILLA_SESSION_FLUSH")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES
