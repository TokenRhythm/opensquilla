"""Boot-time advisory hint for importable legacy OpenSquilla homes.

``_warn_legacy_home_detected`` must log exactly once on the fresh-home +
candidate combination and stay silent (without even running detection) on an
established home. Migration itself stays settings- and CLI-only: the hint is
the single log line pointing headless operators at ``opensquilla migrate
opensquilla`` and Settings → Advanced → Data maintenance. Structured warnings
are captured by monkeypatching the boot module's ``log.warning``, the same
technique as the workspace/state mismatch test in ``test_router_boot.py``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway import boot as boot_module
from opensquilla.gateway.boot import _warn_legacy_home_detected
from opensquilla.gateway.config import GatewayConfig
from opensquilla.migration import legacy_detect
from opensquilla.migration.legacy_detect import LegacyHomeCandidate


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    real_log = boot_module.log

    class _WarningLog:
        def warning(self, event: str, **kwargs: Any) -> None:
            warnings.append({"event": event, **kwargs})

        def __getattr__(self, name: str) -> Any:
            return getattr(real_log, name)

    monkeypatch.setattr(boot_module, "log", _WarningLog())
    return warnings


def _config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        state_dir=str(tmp_path / "home" / "state"),
        config_path=str(tmp_path / "home" / "config.toml"),
    )


def test_fresh_home_with_candidate_logs_hint_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings = _capture_warnings(monkeypatch)
    legacy = tmp_path / "legacy-home"
    candidate = LegacyHomeCandidate(path=legacy, kind="cli-home")
    seen_targets: list[Path | None] = []

    def _detect(target: Path | None = None) -> LegacyHomeCandidate:
        seen_targets.append(target)
        return candidate

    monkeypatch.setattr(legacy_detect, "detect_legacy_home", _detect)

    _warn_legacy_home_detected(_config(tmp_path))

    assert len(warnings) == 1
    assert warnings[0]["event"] == "build_services.legacy_home_detected"
    assert warnings[0]["legacy_home"] == str(legacy)
    assert warnings[0]["kind"] == "cli-home"
    # The hint must name both import surfaces without executing either.
    assert "opensquilla migrate opensquilla" in warnings[0]["detail"]
    assert "Data maintenance" in warnings[0]["detail"]
    # Detection ran once, against the home the gateway actually booted from.
    assert seen_targets == [(tmp_path / "home").resolve()]


def test_established_home_is_silent_and_skips_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings = _capture_warnings(monkeypatch)
    state_dir = tmp_path / "home" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "sessions.db").write_bytes(b"")
    calls: list[Path | None] = []
    monkeypatch.setattr(
        legacy_detect,
        "detect_legacy_home",
        lambda target=None: calls.append(target),
    )

    _warn_legacy_home_detected(_config(tmp_path))

    assert warnings == []
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_established_home_skips_legacy_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.persistence.migrator import _native_sqlite_path

    warnings = _capture_warnings(monkeypatch)
    long_root = tmp_path / "long-home"
    state_dir = long_root
    index = 0
    while len(str(state_dir / "sessions.db")) < 280:
        state_dir /= f"state-segment-{index:02d}-0123456789"
        index += 1
    native_state = _native_sqlite_path(state_dir)
    os.makedirs(native_state)
    with open(_native_sqlite_path(state_dir / "sessions.db"), "wb") as handle:
        handle.write(b"")
    calls: list[Path | None] = []
    monkeypatch.setattr(
        legacy_detect,
        "detect_legacy_home",
        lambda target=None: calls.append(target),
    )
    config = GatewayConfig(
        state_dir=str(state_dir),
        config_path=str(tmp_path / "home" / "config.toml"),
    )
    try:
        _warn_legacy_home_detected(config)

        assert warnings == []
        assert calls == []
    finally:
        native_root = _native_sqlite_path(long_root)
        if os.path.exists(native_root):
            shutil.rmtree(native_root)


def test_fresh_home_without_candidate_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings = _capture_warnings(monkeypatch)
    monkeypatch.setattr(legacy_detect, "detect_legacy_home", lambda target=None: None)

    _warn_legacy_home_detected(_config(tmp_path))

    assert warnings == []
