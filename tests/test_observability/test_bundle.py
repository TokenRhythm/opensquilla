"""collect_bundle: zip contents, redaction bar, desktop derivation, best-effort.

All fixture data is synthetic. The fixture builds a fake OpenSquilla home +
log dir, and an autouse fixture pins OPENSQUILLA_GATEWAY_CONFIG_PATH to a
synthetic TOML so no real config is ever read or rewritten.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opensquilla.observability.bundle import _TAIL_CAP, collect_bundle
from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

FAKE_KEY = "sk-FAKE1234567890abcdef"

# A payload the always-run migration normalizations rewrite (capture_mode
# rename), so loading it via config_store.load_config would rewrite the file
# in place and drop a *.backup.* sibling next to it.
OUTDATED_TOML = '[memory]\ncapture_mode = "archive_turn_pair"\n'


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin config resolution to a synthetic file for every bundle test.

    Without this, resolve_config_path(None) falls back to ./opensquilla.toml
    and then the developer's real home config — which the doctor collector's
    migration path could rewrite.
    """
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic bundle-test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    return config_path


def _make_home(tmp_path: Path, *, desktop: bool = False) -> tuple[Path, Path]:
    """Return (home_dir, log_dir) with synthetic state."""
    if desktop:
        user_data = tmp_path / "user-data"
        home = user_data / "opensquilla" / "state"
        (user_data / "logs").mkdir(parents=True)
        (user_data / "logs" / "desktop.log").write_text(
            '{"at":"2026-07-07T00:00:00Z","event":"launch"}\n', encoding="utf-8"
        )
        (user_data / "logs" / "gateway.log").write_text("gateway child out\n", encoding="utf-8")
        (user_data / "desktop-credential.json").write_text("{}", encoding="utf-8")
    else:
        home = tmp_path / "home"
    log_dir = home / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "debug.log").write_text(
        f"2026-07-07 [ERROR] opensquilla: boom api_key={FAKE_KEY}\n", encoding="utf-8"
    )
    day = datetime.now(UTC).strftime("%Y%m%d")
    (log_dir / f"decisions-{day}.jsonl").write_text('{"model":"fake"}\n', encoding="utf-8")
    (log_dir / f"traces-{day}.jsonl").write_text('{"kind":"turn_start"}\n', encoding="utf-8")
    (log_dir / f"turn-calls-{day}.jsonl").write_text('{"kind":"llm_request"}\n', encoding="utf-8")
    # Hard-excluded material: .env files and the raw decision debug mirror
    # must never make it into any bundle tier.
    (home / ".env").write_text(f"OPENSQUILLA_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    (log_dir / ".env").write_text(f"OPENSQUILLA_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    debug_dir = log_dir / "debug"
    debug_dir.mkdir()
    (debug_dir / f"decisions-{day}-raw.jsonl").write_text(
        '{"turn_id":"t1","entry":{"prompt":"raw"}}\n', encoding="utf-8"
    )
    # Also directly in log_dir, where the decisions-*.jsonl glob would see it:
    # only the day-stamp regex + write-time exclusion guard keep it out.
    (log_dir / f"decisions-{day}-raw.jsonl").write_text(
        '{"turn_id":"t1","entry":{"prompt":"raw"}}\n', encoding="utf-8"
    )

    db = home / "sessions.db"
    apply_pending(str(db), MIGRATIONS_DIR)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO turn_errors (error_id, session_key, ts_ms, message) VALUES (?, ?, ?, ?)",
        ("abcd1234", "agent:main:test", int(datetime.now(UTC).timestamp() * 1000), "boom"),
    )
    conn.commit()
    conn.close()
    return home, log_dir


def _read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_default_bundle_contents_and_redaction(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    active = home / "state/toolchains/v1/active"
    active.mkdir(parents=True)
    (active / "paper-tex.json").write_text(
        json.dumps(
            {
                "component_id": "paper-tex",
                "version": "2026.05",
                "platform_key": "test-x64",
                "install_backend": "archive",
                "package_relpath": "/private/machine/path",
                "secret": FAKE_KEY,
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(dest, home_dir=home, log_dir=log_dir)

    assert result.path == dest
    entries = _read_zip(dest)
    assert "manifest.json" in entries
    assert "logs/debug.log" in entries
    assert "errors.jsonl" in entries
    assert "toolchains.json" in entries
    assert json.loads(entries["toolchains.json"]) == [
        {
            "component_id": "paper-tex",
            "active": True,
            "version": "2026.05",
            "platform_key": "test-x64",
            "install_backend": "archive",
        }
    ]
    assert b"/private/machine/path" not in entries["toolchains.json"]
    assert any(name.startswith("decisions/") for name in entries)
    assert any(name.startswith("traces/") for name in entries)
    # Default tier: no raw turn-call capture, no transcript content.
    assert not any("turn-calls" in name for name in entries)
    # Redaction bar: the fake key must not appear anywhere in the zip.
    blob = b"".join(entries.values())
    assert FAKE_KEY.encode() not in blob

    manifest = json.loads(entries["manifest.json"])
    assert manifest["bundle_schema"] == 1
    assert manifest["content_tier"] is False
    assert "opensquilla_version" in manifest
    errors = json.loads(b"[" + entries["errors.jsonl"].replace(b"\n", b",").rstrip(b",") + b"]")
    assert errors[0]["error_id"] == "abcd1234"


def test_content_tier_includes_turn_calls(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=True)

    entries = _read_zip(dest)
    assert any("turn-calls" in name for name in entries)
    manifest = json.loads(entries["manifest.json"])
    assert manifest["content_tier"] is True


def test_desktop_logs_are_derived_and_credential_excluded(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path, desktop=True)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    entries = _read_zip(dest)
    assert "desktop/desktop.log" in entries
    assert "desktop/gateway.log" in entries
    assert not any("desktop-credential" in name for name in entries)


def test_missing_artifacts_become_collection_errors(tmp_path) -> None:
    home = tmp_path / "empty-home"
    log_dir = home / "logs"
    home.mkdir()
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    assert dest.exists()  # bundle always succeeds
    entries = _read_zip(dest)
    manifest = json.loads(entries["manifest.json"])
    assert isinstance(manifest["collection_errors"], list)


def test_tail_cap_truncates_large_files(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    (home / "logs" / "gateway.log").parent.mkdir(parents=True, exist_ok=True)
    (home / "logs" / "gateway.log").write_bytes(b"x" * 6_000_000)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    entries = _read_zip(dest)
    assert len(entries["logs/gateway.log"]) < 5_100_000
    manifest = json.loads(entries["manifest.json"])
    assert any("gateway.log" in str(item) for item in manifest["truncations"])


def test_doctor_collection_never_rewrites_outdated_config(
    tmp_path, _hermetic_config: Path
) -> None:
    """collect_bundle must be byte-identical read-only, even on an outdated config.

    The doctor collector's config loader migrates outdated payloads in place
    (rewrite + *.backup.* sibling); the bundle must never let that reach the
    user's real file.
    """
    config_path = _hermetic_config
    config_path.write_text(OUTDATED_TOML, encoding="utf-8")
    before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == before
    assert not list(config_path.parent.glob(f"{config_path.name}.backup*"))
    # The doctor artifact itself is still collected (from a throwaway copy).
    entries = _read_zip(dest)
    assert "doctor.json" in entries


def test_tail_truncation_never_bisects_a_secret_line(tmp_path) -> None:
    """A tail-cap seek boundary that bisects a secret line must not leak its tail.

    scrub_text matches key=value shapes per line; a decapitated first line has
    lost its ``api_key=`` prefix, so the surviving value fragment would pass
    through unmasked unless the partial line is dropped.
    """
    home, log_dir = _make_home(tmp_path)
    secret_value = "sk-TAILBOUNDARY0123456789abcdef"
    secret_line = f"api_key={secret_value}\n".encode()
    cut = len(b"api_key=sk-TAILB")  # seek boundary lands mid-value
    leaked_fragment = secret_line[cut:].rstrip(b"\n")  # b"OUNDARY0123456789abcdef"
    filler = b"z" * (_TAIL_CAP - (len(secret_line) - cut))
    head = b"head line\n" * 64
    (home / "logs" / "gateway.log").write_bytes(head + secret_line + filler)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    entry = _read_zip(dest)["logs/gateway.log"]
    assert leaked_fragment not in entry
    assert secret_value.encode() not in entry


def test_errors_collected_from_config_state_dir(
    tmp_path, _hermetic_config: Path
) -> None:
    """A config-declared state_dir wins over home_dir when probing sessions.db."""
    home, log_dir = _make_home(tmp_path)
    # Point the DB probe elsewhere: config state_dir holds the only row that
    # distinguishes the two databases.
    state = tmp_path / "custom-state"
    state.mkdir()
    _hermetic_config.write_text(
        f"state_dir = {json.dumps(str(state), ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    db = state / "sessions.db"
    apply_pending(str(db), MIGRATIONS_DIR)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO turn_errors (error_id, session_key, ts_ms, message) VALUES (?, ?, ?, ?)",
        ("feed5678", "agent:main:test", int(datetime.now(UTC).timestamp() * 1000), "custom"),
    )
    conn.commit()
    conn.close()
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    entries = _read_zip(dest)
    errors = entries["errors.jsonl"].decode("utf-8")
    assert "feed5678" in errors  # row from the configured state_dir
    assert "abcd1234" not in errors  # home_dir fallback DB was not consulted


@pytest.mark.parametrize("include_content", [False, True])
def test_env_files_and_raw_mirrors_never_bundled(tmp_path, include_content: bool) -> None:
    """Hard exclusions hold at both tiers: no .env, no raw decision mirrors."""
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=include_content)

    for name in _read_zip(dest):
        base = name.rsplit("/", 1)[-1]
        assert base != ".env"
        assert not base.startswith(".env.")
        assert not name.endswith("-raw.jsonl")
