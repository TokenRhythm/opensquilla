"""Layer-neutral access to the hardened profile operation lock.

The recovery package owns the platform-specific lock implementation because it
also coordinates profile moves and legacy gateway leases. Runtime subsystems
that only need writer exclusion import this narrow facade instead of depending
on the recovery package directly.
"""

from __future__ import annotations

from opensquilla.recovery.locking import (
    ProfileOperationLock,
    profile_operation_lock_held_by_current_thread,
)

__all__ = [
    "ProfileOperationLock",
    "profile_operation_lock_held_by_current_thread",
]
