"""Settings-only legacy profile discovery contracts.

The single-candidate helper remains for CLI compatibility; the settings RPC
uses bounded multi-candidate discovery against the active target profile.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from opensquilla.migration import opensquilla_home
from opensquilla.migration.legacy_detect import (
    LegacyHomeCandidate,
    detect_legacy_home,
    detect_legacy_homes,
)


def _make_home(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    return path


@pytest.fixture()
def _no_portable_bases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("TEMP", raising=False)


def test_detects_cli_home_when_target_is_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    candidate = detect_legacy_home(tmp_path / "electron-home")

    assert candidate == LegacyHomeCandidate(path=legacy, kind="cli-home")


def test_live_cli_home_is_never_offered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    assert detect_legacy_home(legacy) is None


def test_default_target_honors_state_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Relocated install (e.g. a desktop spawn): ~/.opensquilla is a candidate.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "relocated-home"))
    relocated = detect_legacy_home()
    assert relocated == LegacyHomeCandidate(path=legacy, kind="cli-home")

    # Default install: the target IS ~/.opensquilla, so nothing is offered.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(legacy))
    assert detect_legacy_home() is None


def test_cli_home_precedes_desktop_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    desktop = _make_home(tmp_path / "desktop-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(
        opensquilla_home,
        "detect_desktop_home",
        lambda target=None: desktop,
    )

    candidate = detect_legacy_home(tmp_path / "target-home")

    assert candidate == LegacyHomeCandidate(path=legacy, kind="cli-home")


def test_discovers_all_source_kinds_in_priority_order_and_caps_at_twelve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    desktop = _make_home(tmp_path / "desktop-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(
        opensquilla_home,
        "detect_desktop_home",
        lambda target=None: desktop,
    )
    base = tmp_path / "appdata-local"
    portable_homes = [
        _make_home(base / "OpenSquilla" / "portable" / f"release-{index:02d}")
        for index in range(16)
    ]
    for index, home in enumerate(portable_homes):
        os.utime(home / "config.toml", (float(index + 1), float(index + 1)))
        os.utime(home, (float(index + 1), float(index + 1)))
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)

    candidates = detect_legacy_homes(tmp_path / "target", limit=99)

    assert len(candidates) == 12
    assert candidates[:2] == [
        LegacyHomeCandidate(path=legacy, kind="cli-home"),
        LegacyHomeCandidate(path=desktop, kind="desktop-home"),
    ]
    assert [candidate.path for candidate in candidates[2:]] == list(
        reversed(portable_homes[-10:])
    )
    assert all(candidate.kind == "windows-portable" for candidate in candidates[2:])


def test_discovery_deduplicates_the_same_directory_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = _make_home(fake_home / ".opensquilla")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(
        opensquilla_home,
        "detect_desktop_home",
        lambda target=None: legacy,
    )

    assert detect_legacy_homes(tmp_path / "target") == [
        LegacyHomeCandidate(path=legacy, kind="cli-home")
    ]


def test_desktop_home_precedes_portable_and_preserves_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()  # no ~/.opensquilla: the cli-home probe finds nothing
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    desktop = _make_home(tmp_path / "desktop-home")
    monkeypatch.setattr(
        opensquilla_home,
        "detect_desktop_home",
        lambda target=None: desktop,
    )
    base = tmp_path / "appdata-local"
    _make_home(base / "OpenSquilla" / "portable" / "dummy-release")
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)

    candidate = detect_legacy_home(tmp_path / "target-home")

    assert candidate == LegacyHomeCandidate(path=desktop, kind="desktop-home")


def test_desktop_home_matching_target_is_not_offered_but_old_marker_does_not_hide_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    desktop = _make_home(tmp_path / "desktop-home")
    desktop_probes: list[Path] = []
    monkeypatch.setattr(
        opensquilla_home,
        "detect_desktop_home",
        lambda target=None: desktop_probes.append(desktop) or desktop,
    )

    assert detect_legacy_home(desktop) is None
    assert desktop_probes == [desktop]

    marker_probes: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        opensquilla_home,
        "_source_marker_matches_target",
        lambda source, target: marker_probes.append((source, target)) or source == desktop,
    )
    target = tmp_path / "target-home"
    assert detect_legacy_home(target) == LegacyHomeCandidate(
        path=desktop,
        kind="desktop-home",
    )
    assert marker_probes == []


@pytest.mark.skipif(sys.platform == "win32", reason="uses the POSIX Desktop path")
def test_explicit_gateway_target_wins_over_ambient_default_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    desktop = _make_home(
        fake_home / ".config" / "OpenSquilla" / "opensquilla"
        if sys.platform != "darwin"
        else fake_home / "Library" / "Application Support" / "OpenSquilla" / "opensquilla"
    )
    # An ambient shell setting points at Desktop, but the running gateway's
    # config-derived target is elsewhere. Discovery must use the latter.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(desktop))

    assert detect_legacy_homes(tmp_path / "actual-gateway-target") == [
        LegacyHomeCandidate(path=desktop, kind="desktop-home")
    ]


def test_portable_fallback_offers_newest_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()  # no ~/.opensquilla: the cli-home probe finds nothing
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    base = tmp_path / "appdata-local"
    older = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-a")
    newer = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-b")
    now = time.time()
    os.utime(older / "config.toml", (now - 1000, now - 1000))
    os.utime(newer / "config.toml", (now, now))
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)

    candidate = detect_legacy_home(tmp_path / "target-home")

    assert candidate == LegacyHomeCandidate(path=newer, kind="windows-portable")


def test_portable_fallback_keeps_distinct_paths_when_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    base = tmp_path / "appdata-local"
    older = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-a")
    newer = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-b")
    now = time.time()
    os.utime(older / "config.toml", (now - 1000, now - 1000))
    os.utime(newer / "config.toml", (now, now))
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setattr(opensquilla_home, "_advisory_identity", lambda _result: None)

    candidate = detect_legacy_home(tmp_path / "target-home")

    assert candidate == LegacyHomeCandidate(path=newer, kind="windows-portable")


def test_portable_candidate_that_is_the_target_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    base = tmp_path / "appdata-local"
    live = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-a")
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)

    assert detect_legacy_home(live) is None


def test_old_portable_marker_does_not_hide_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    base = tmp_path / "appdata-local"
    imported = _make_home(base / "OpenSquilla" / "portable" / "dummy-release-a")
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setattr(
        opensquilla_home,
        "_source_marker_matches_target",
        lambda source, _target: source == imported,
    )

    candidate = detect_legacy_home(tmp_path / "target-home")

    # Receipt/marker state is a display hint only. Candidate discovery must not
    # silently hide data that the user may explicitly choose to import again.
    assert candidate == LegacyHomeCandidate(path=imported, kind="windows-portable")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only guard")
def test_portable_enumeration_is_skipped_without_base_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_portable_bases: None,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    calls: list[object] = []
    monkeypatch.setattr(
        opensquilla_home,
        "enumerate_portable_homes",
        lambda bases=None: calls.append(bases) or [],
    )

    assert detect_legacy_home(tmp_path / "target-home") is None
    assert not calls


def test_detection_swallows_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(target: Path) -> Path | None:
        raise OSError("disk unreadable")

    monkeypatch.setattr(opensquilla_home, "detect_legacy_cli_home", _explode)

    assert detect_legacy_home(tmp_path / "target-home") is None
