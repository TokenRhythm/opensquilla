"""Install receipt: round-trip, robustness, and detection fallback."""

from __future__ import annotations

from pathlib import Path

from opensquilla.uninstall import inventory
from opensquilla.uninstall.inventory import METHOD_PIP, METHOD_UNKNOWN, METHOD_UV_TOOL
from opensquilla.uninstall.receipt import build_receipt, read_receipt, write_receipt


def test_receipt_round_trip(tmp_path: Path) -> None:
    payload = build_receipt(
        install_method="uv-tool",
        installed_at="2026-06-29T00:00:00Z",
        entrypoints=["/x/bin/opensquilla"],
        owned_paths=["/x/tools/opensquilla"],
        data_root=str(tmp_path),
        source_commit_id="0123456789abcdef0123456789abcdef01234567",
    )
    write_receipt(payload, home=tmp_path)
    loaded = read_receipt(tmp_path)
    assert loaded == payload


def test_read_receipt_missing_or_malformed(tmp_path: Path) -> None:
    assert read_receipt(tmp_path) is None  # absent
    (tmp_path / "install-receipt.json").write_text("{not json", encoding="utf-8")
    assert read_receipt(tmp_path) is None  # malformed -> conservative mode
    (tmp_path / "install-receipt.json").write_text("[]", encoding="utf-8")
    assert read_receipt(tmp_path) is None  # wrong shape


def test_detect_uses_receipt_hint_only_when_unknown(monkeypatch) -> None:
    for var in (
        "OPENSQUILLA_INSTALL_METHOD",
        "OPENSQUILLA_DESKTOP",
        "OPENSQUILLA_RUNNING_IN_CONTAINER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(inventory, "_docker_image_install", lambda: False)
    monkeypatch.setattr(inventory, "_portable_venv_dir", lambda: None)
    monkeypatch.setattr(inventory, "_is_editable_install", lambda: False)
    monkeypatch.setattr(inventory, "_venv_ancestry", lambda: None)

    # No distribution → would be unknown; the receipt hint fills in.
    monkeypatch.setattr(inventory, "_has_distribution", lambda: False)
    assert inventory.detect_install_method(receipt_hint="uv-tool") == METHOD_UV_TOOL
    assert inventory.detect_install_method(receipt_hint="bogus") == METHOD_UNKNOWN

    # A concrete runtime signal is never overridden by the hint.
    monkeypatch.setattr(inventory, "_has_distribution", lambda: True)
    assert inventory.detect_install_method(receipt_hint="uv-tool") == METHOD_PIP


def test_read_receipt_tolerates_utf8_bom(tmp_path: Path) -> None:
    # Windows PowerShell 5.1 `Set-Content -Encoding utf8` writes a BOM.
    payload = '{"version": 1, "install_method": "uv-tool"}'
    (tmp_path / "install-receipt.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    loaded = read_receipt(tmp_path)
    assert loaded is not None
    assert loaded["install_method"] == "uv-tool"
