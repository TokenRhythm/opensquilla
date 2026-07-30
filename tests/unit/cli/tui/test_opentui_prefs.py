"""Persistence contract for the CLI-TUI preferences file.

The theme choice and the fallback-notice bookkeeping live in one
schema-versioned JSON under the state root. The failure posture matters more
than the happy path: corruption degrades to defaults, unknown names are
ignored, and write failures never raise — a read-only state dir must never
break chat launch.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui import prefs
from opensquilla.cli.tui.opentui.messages import HostInputSubmit, HostThemeSelected
from opensquilla.cli.tui.opentui.themes import DEFAULT_THEME, handle_theme_command


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    return tmp_path


def test_theme_preference_round_trips() -> None:
    assert prefs.load_theme_preference() is None
    prefs.save_theme_preference("nord")
    assert prefs.load_theme_preference() == "nord"
    # Whitespace and case are normalized on save.
    prefs.save_theme_preference("  EMBER ")
    assert prefs.load_theme_preference() == "ember"


def test_prefs_file_lives_under_the_state_root(tmp_path: Path) -> None:
    # Guard against the path drifting away from what these tests hand-write:
    # every fixture below builds files at exactly the module's own location.
    assert prefs._prefs_path() == tmp_path / "state" / "tui" / "prefs.json"


def test_unknown_theme_names_are_ignored_on_save_and_load() -> None:
    prefs.save_theme_preference("solarized-nope")
    assert prefs.load_theme_preference() is None

    # A stale file naming a theme this binary does not know (downgrade after a
    # rename) silently falls back instead of failing launch.
    path = prefs._prefs_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schemaVersion": 1, "theme": "retired-theme"}))
    assert prefs.load_theme_preference() is None


def test_corrupt_prefs_file_degrades_to_defaults() -> None:
    path = prefs._prefs_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert prefs.load_theme_preference() is None
    assert prefs.fallback_notice_due("0.5.0", "missing") is True
    # A save over the corrupt file recovers it.
    prefs.save_theme_preference("mono")
    assert prefs.load_theme_preference() == "mono"


def test_write_failures_are_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _deny(*args: object, **kwargs: object) -> None:
        raise OSError("read-only state dir")

    monkeypatch.setattr(Path, "mkdir", _deny)
    prefs.save_theme_preference("nord")  # must not raise
    prefs.record_fallback_notice("0.5.0", "missing")  # must not raise
    assert prefs.load_theme_preference() is None


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="root and Windows bypass file modes",
)
def test_unreadable_file_blocks_writes_instead_of_clobbering() -> None:
    # A file that exists but cannot be READ may still hold good data: writers
    # must skip rather than rewrite it from the empty fallback.
    prefs.save_theme_preference("nord")
    path = prefs._prefs_path()
    path.chmod(0o000)
    try:
        prefs.record_fallback_notice("0.5.0", "missing")  # must not clobber
        prefs.save_theme_preference("ember")  # must not clobber
    finally:
        path.chmod(0o600)
    assert prefs.load_theme_preference() == "nord"
    assert prefs.fallback_notice_due("0.5.0", "missing") is True


def test_transient_read_error_blocks_writes_on_any_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic version of the chmod test: inject the read failure
    # directly so the writable=False branch is exercised as root, on
    # Windows, and on permission-ignoring filesystems alike.
    prefs.save_theme_preference("nord")
    real_read_text = Path.read_text

    def _flaky_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == prefs._prefs_path():
            raise PermissionError("transient EACCES")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read)
    prefs.record_fallback_notice("0.5.0", "missing")  # must not clobber
    prefs.save_theme_preference("ember")  # must not clobber
    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert prefs.load_theme_preference() == "nord"
    assert prefs.fallback_notice_due("0.5.0", "missing") is True


def test_saving_theme_preserves_the_notice_record_and_vice_versa() -> None:
    prefs.save_theme_preference("nord")
    prefs.record_fallback_notice("0.5.0", "missing")
    assert prefs.load_theme_preference() == "nord"
    assert prefs.fallback_notice_due("0.5.0", "missing") is False
    prefs.save_theme_preference("slate")
    assert prefs.fallback_notice_due("0.5.0", "missing") is False


def test_fallback_notice_is_due_once_per_version_and_reason() -> None:
    assert prefs.fallback_notice_due("0.5.0", "missing") is True
    prefs.record_fallback_notice("0.5.0", "missing")
    assert prefs.fallback_notice_due("0.5.0", "missing") is False
    # A cause change within the same version is new information.
    assert prefs.fallback_notice_due("0.5.0", "version_mismatch") is True
    # So is the same cause after an upgrade.
    assert prefs.fallback_notice_due("0.6.0", "missing") is True


def test_alternating_reasons_never_renag() -> None:
    prefs.record_fallback_notice("0.5.0", "missing")
    prefs.record_fallback_notice("0.5.0", "version_mismatch")
    # Both pairs were shown once; flip-flopping between them stays quiet.
    assert prefs.fallback_notice_due("0.5.0", "missing") is False
    assert prefs.fallback_notice_due("0.5.0", "version_mismatch") is False
    # The record is bounded and duplicate pairs are not re-appended.
    prefs.record_fallback_notice("0.5.0", "missing")
    for build in range(20):
        prefs.record_fallback_notice(f"0.{build}.0", "missing")
    records = json.loads(prefs._prefs_path().read_text())["fallbackNotices"]
    assert len(records) <= 8
    # Eviction must drop the OLDEST pairs: keeping the oldest instead would
    # evict each just-recorded pair and turn the notice into a per-launch nag.
    assert prefs.fallback_notice_due("0.19.0", "missing") is False
    assert prefs.fallback_notice_due("0.0.0", "missing") is True


def test_legacy_singular_notice_record_still_counts() -> None:
    path = prefs._prefs_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "fallbackNotice": {"productVersion": "0.5.0", "reasonCode": "missing"},
            }
        )
    )
    assert prefs.fallback_notice_due("0.5.0", "missing") is False
    assert prefs.fallback_notice_due("0.5.0", "version_mismatch") is True


async def test_theme_command_persists_direct_choice() -> None:
    class _Output:
        supports_send_message = True

        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _Output()
    await handle_theme_command("/theme nord", output)
    assert output.sent == [("theme.set", {"name": "nord"})]
    assert prefs.load_theme_preference() == "nord"

    # Unknown names open the picker and persist nothing.
    await handle_theme_command("/theme not-a-theme", output)
    assert output.sent[-1][0] == "theme.pick"
    assert prefs.load_theme_preference() == "nord"


async def test_surface_persists_picker_confirmation() -> None:
    from opensquilla.cli.tui.opentui.surface import OpenTuiSurface
    from opensquilla.engine.commands import Surface
    from tests.unit.cli.tui.test_opentui_surface import FakeOpenTuiBridge

    bridge = FakeOpenTuiBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostThemeSelected(name="ember"))
    bridge.messages.put_nowait(HostInputSubmit(text="next"))

    assert await asyncio.wait_for(surface.next_line(), timeout=5) == "next"
    # Persistence is fire-and-forget so the pump never stalls on prefs IO;
    # drain the background task before asserting the durable effect.
    await asyncio.gather(*surface._persist_tasks)
    assert prefs.load_theme_preference() == "ember"
    assert DEFAULT_THEME != "ember"  # the test would be vacuous otherwise


def test_bridge_env_injection_prefers_explicit_env_over_pref() -> None:
    from opensquilla.cli.tui.opentui.bridge import apply_theme_preference_env
    from opensquilla.cli.tui.opentui.themes import THEME_ENV_VAR

    prefs.save_theme_preference("nord")

    # Unset: the persisted preference fills the gap.
    env: dict[str, str] = {}
    apply_theme_preference_env(env)
    assert env[THEME_ENV_VAR] == "nord"

    # A non-empty explicit value wins.
    env = {THEME_ENV_VAR: "ember"}
    apply_theme_preference_env(env)
    assert env[THEME_ENV_VAR] == "ember"

    # An exported-but-empty value counts as unset — the host would map "" to
    # the default theme, silently masking the preference forever.
    env = {THEME_ENV_VAR: ""}
    apply_theme_preference_env(env)
    assert env[THEME_ENV_VAR] == "nord"


def test_bridge_env_injection_without_preference_is_a_no_op() -> None:
    from opensquilla.cli.tui.opentui.bridge import apply_theme_preference_env
    from opensquilla.cli.tui.opentui.themes import THEME_ENV_VAR

    env: dict[str, str] = {}
    apply_theme_preference_env(env)
    assert THEME_ENV_VAR not in env
