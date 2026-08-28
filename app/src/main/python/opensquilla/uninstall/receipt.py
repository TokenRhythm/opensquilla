"""Install receipt — an optional manifest installers write to aid uninstall.

The receipt records how OpenSquilla was installed (method, entrypoints, owned
paths) so the uninstaller can act precisely instead of inferring everything at
runtime. It is a *hint*, never sole deletion authority: the uninstaller still
re-verifies containment and protected-root rules before deleting anything a
receipt lists, so a stale or hand-edited receipt cannot widen the blast radius.

Installs that predate receipts (every install in the field today) simply have no
receipt; the uninstaller falls back to runtime inventory ("conservative mode").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home

RECEIPT_SCHEMA_VERSION = 1
RECEIPT_FILENAME = "install-receipt.json"


def receipt_path(home: Path | None = None) -> Path:
    """Location of the install receipt under the OpenSquilla home."""
    return (home or default_opensquilla_home()) / RECEIPT_FILENAME


def read_receipt(home: Path | None = None) -> dict[str, Any] | None:
    """Return the parsed receipt, or ``None`` when absent/unreadable/invalid.

    Robust by design: a malformed receipt is treated as "no receipt" so a
    corrupt file degrades to conservative mode rather than crashing uninstall.
    """
    path = receipt_path(home)
    try:
        # utf-8-sig tolerates a BOM (Windows PowerShell 5.1 Set-Content -Encoding
        # utf8 writes one) as well as plain UTF-8.
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("version"), int):
        return None
    return data


def build_receipt(
    *,
    install_method: str,
    installed_at: str,
    entrypoints: list[str],
    owned_paths: list[str],
    data_root: str,
) -> dict[str, Any]:
    """Construct a receipt payload (pure; caller serializes/writes it)."""
    return {
        "version": RECEIPT_SCHEMA_VERSION,
        "install_method": install_method,
        "installed_at": installed_at,
        "entrypoints": list(entrypoints),
        "owned_paths": list(owned_paths),
        "data_root": data_root,
    }


def write_receipt(payload: dict[str, Any], home: Path | None = None) -> Path:
    """Write ``payload`` as the install receipt (0600), returning its path."""
    path = receipt_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
