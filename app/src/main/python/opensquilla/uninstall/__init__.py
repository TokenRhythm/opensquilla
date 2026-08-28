"""Inventory-driven uninstaller for OpenSquilla.

Removing OpenSquilla spans many install methods (uv-tool, pip, pipx, source,
portable, docker, desktop) and several on-disk state roots, with user data
scattered behind env/config relocation knobs. Rather than hard-coding a single
``rm -rf`` target, the uninstaller:

1. :mod:`~opensquilla.uninstall.inventory` — discovers the install method, the
   resolved state roots, the removable data buckets, services, and any install
   receipt.
2. :mod:`~opensquilla.uninstall.plan` — turns the inventory + caller options into
   a pure :class:`UninstallPlan` (remove / stop / purge / keep / warn / manual).
   This is what ``--dry-run`` / ``--json`` render; it performs no I/O.
3. :mod:`~opensquilla.uninstall.actions` — executes a plan (quiesce the gateway,
   unregister services, run the package manager, delete owned paths) behind the
   guards in :mod:`~opensquilla.uninstall.safety`.

Default posture: remove the program, keep user data. Data is deleted only behind
explicit ``--purge-*`` flags, and every deletion is contained to a resolved,
non-protected root.
"""

from __future__ import annotations

from opensquilla.uninstall.inventory import Inventory, discover
from opensquilla.uninstall.plan import PlanOptions, UninstallPlan, build_plan

__all__ = [
    "Inventory",
    "PlanOptions",
    "UninstallPlan",
    "build_plan",
    "discover",
]
