from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "opensquilla"
RAW_CONNECTION_OWNERS = {
    Path("session/storage.py"),
    Path("channels/websocket.py"),
}


def test_raw_connection_access_isolated_from_session_storage_consumers() -> None:
    """Keep shared SessionStorage connection ownership inside its gate."""

    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT)
        if relative in RAW_CONNECTION_OWNERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "conn":
                violations.append(f"{relative}:{node.lineno}: attribute .conn")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "conn"
            ):
                violations.append(f"{relative}:{node.lineno}: getattr(..., 'conn')")

    assert violations == [], (
        "Raw connection access bypasses SessionStorage's operation gate; "
        "add a storage API instead:\n" + "\n".join(violations)
    )
