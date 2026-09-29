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
    config_path.write_text(
        "[privacy]\nagent_trace_enabled = true\n", encoding="utf-8"
    )
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
        (user_data / "logs" / "desktop.log.1").write_text(
            '{"at":"2026-07-06T00:00:00Z","event":"previous-launch"}\n', encoding="utf-8"
        )
        (user_data / "logs" / "desktop.log.2").write_text(
            '{"at":"2026-07-05T00:00:00Z","event":"older-launch"}\n', encoding="utf-8"
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
    (log_dir / f"turn-calls-{day}.jsonl").write_text(
        '{"kind":"llm_request","agent_trace":true}\n', encoding="utf-8"
    )
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


@pytest.mark.parametrize("include_content", [False, True])
def test_all_json_artifacts_parse_and_preserve_metadata(tmp_path, include_content) -> None:
    home, log_dir = _make_home(tmp_path)
    metadata = {
        "requiresApiKey": True,
        "REQUIRESAPIKEY": False,
        "requires_api_key": False,
        "apiKeyConfigured": False,
        "apiKeyEnv": "SYNTHETIC_API_KEY",
        "tokenCount": 17,
        "retryAfter": 1.25,
        "optional": None,
        "session_key": "agent:synthetic",
    }
    secrets = {
        "apiKey": 'dummy "quoted" \\ credential',
        "ACCESS_TOKEN": 12345,
        "clientSecret": {"value": "synthetic credential"},
        "password": ["synthetic credential", False],
    }
    payload = {"providers": [{**metadata, **secrets}], "states": [True, False, 3, None]}
    before = json.dumps(payload)
    day = datetime.now(UTC).strftime("%Y%m%d")
    (log_dir / f"turn-calls-{day}.jsonl").write_text(
        json.dumps({**payload, "agent_trace": True}) + "\n"
    )
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(
        dest, home_dir=home, log_dir=log_dir, include_content=include_content,
        extra={"doctor": payload},
    )

    entries = _read_zip(dest)
    assert {"doctor.json", "live/doctor.json", "config.redacted.json"} <= entries.keys()
    for name, data in entries.items():
        if name.endswith(".json"):
            json.loads(data)
        elif name.endswith(".jsonl"):
            for line in data.splitlines():
                json.loads(line)
    live = json.loads(entries["live/doctor.json"])
    assert live["states"] == payload["states"]
    assert live["providers"][0] == {**metadata, **dict.fromkeys(secrets, "[redacted]")}
    if include_content:
        assert json.loads(entries[f"content/turn-calls-{day}.jsonl"]) == {
            **live, "agent_trace": True,
        }
    assert result.manifest == json.loads(entries["manifest.json"])
    assert not result.manifest["collection_errors"]
    assert json.dumps(payload) == before


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_invalid_json_values_are_omitted_with_actionable_errors(tmp_path, bad_value) -> None:
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"
    result = collect_bundle(
        dest, home_dir=home, log_dir=log_dir, extra={"invalid": {"value": bad_value}},
    )
    entries = _read_zip(dest)
    assert "live/invalid.json" not in entries
    errors = result.manifest["collection_errors"]
    assert any(error["artifact"] == "live/invalid.json" and "JSON" in error["error"]
               for error in errors)


def test_invalid_serialized_json_is_never_written(tmp_path, monkeypatch) -> None:
    from opensquilla.observability import bundle

    home, log_dir = _make_home(tmp_path)
    original_dumps = json.dumps

    def corrupt_encoder(value, **kwargs):
        if value == {"synthetic_fault": True}:
            return '{"synthetic_fault": invalid}'
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(bundle.json, "dumps", corrupt_encoder)
    dest = tmp_path / "bundle.zip"
    result = collect_bundle(
        dest, home_dir=home, log_dir=log_dir, extra={"invalid": {"synthetic_fault": True}},
    )
    assert "live/invalid.json" not in _read_zip(dest)
    assert any(error["artifact"] == "live/invalid.json" and "JSON" in error["error"]
               for error in result.manifest["collection_errors"])


def test_malformed_content_jsonl_is_omitted_with_line_error(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    day = datetime.now(UTC).strftime("%Y%m%d")
    name = f"turn-calls-{day}.jsonl"
    (log_dir / name).write_text('{"kind":"llm_request","agent_trace":true}\n{"password":"synthetic',
                                encoding="utf-8")
    dest = tmp_path / "bundle.zip"
    result = collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=True)
    assert f"content/{name}" not in _read_zip(dest)
    error = next(e for e in result.manifest["collection_errors"]
                 if e["artifact"] == f"content/{name}")
    assert "JSON" in error["error"] and "line 2" in error["error"]
    assert "synthetic" not in error["error"]


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_content_jsonl_preserves_unicode_inside_string_values(tmp_path, separator, newline) -> None:
    from opensquilla.observability.turn_call_log import TurnCallLogger

    home, log_dir = _make_home(tmp_path)
    logger = TurnCallLogger(
        turn_id="synthetic-turn", session_key="agent:synthetic", agent_id="synthetic",
        provider="synthetic", model="synthetic", log_dir=log_dir,
        agent_trace_enabled=lambda: True,
    )
    message = f"synthetic{separator}message"
    path = logger.write("llm_request", {"message": message, "api_key": "dummy credential"})
    assert path is not None
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline.encode()))
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=True)

    entry_name = f"content/{path.name}"
    assert not result.manifest["collection_errors"]
    data = _read_zip(dest)[entry_name]
    records = [json.loads(line) for line in data.split(b"\n") if line]
    assert len(records) == 2
    assert records[1]["payload"] == {"message": message, "api_key": "[redacted]"}


@pytest.mark.parametrize("suffix", ["", "\n", "\r\n"])
def test_jsonl_write_validation_uses_lf_record_boundaries(tmp_path, suffix) -> None:
    from opensquilla.observability.bundle import _write_entry

    text = json.dumps(
        {"message": "synthetic\u0085\u2028\u2029message"}, ensure_ascii=False,
    ) + suffix
    dest = tmp_path / "bundle.zip"
    with zipfile.ZipFile(dest, "w") as archive:
        _write_entry(archive, "content/synthetic.jsonl", text)
    assert _read_zip(dest)["content/synthetic.jsonl"].decode("utf-8") == text


@pytest.mark.parametrize("text", ['{}\n\n', '{}\n\n{}', '{}\r{}'])
def test_invalid_jsonl_record_boundaries_still_fail(tmp_path, text) -> None:
    from opensquilla.observability.bundle import _write_entry

    with zipfile.ZipFile(tmp_path / "bundle.zip", "w") as archive:
        with pytest.raises(ValueError, match="Invalid JSONL"):
            _write_entry(archive, "content/synthetic.jsonl", text)
        assert not archive.namelist()


def test_offline_doctor_uses_the_structured_json_boundary(tmp_path, monkeypatch) -> None:
    payload = {"checks": [{"requiresApiKey": True, "apiKey": "synthetic credential"}]}
    monkeypatch.setattr(
        "opensquilla.diagnostics_sources.offline_doctor_report", lambda *args, **kwargs: payload,
    )
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"
    collect_bundle(dest, home_dir=home, log_dir=log_dir)
    assert json.loads(_read_zip(dest)["doctor.json"]) == {
        "checks": [{"requiresApiKey": True, "apiKey": "[redacted]"}],
    }


def test_config_secret_and_metadata_fields_use_bundle_policy(tmp_path, _hermetic_config) -> None:
    _hermetic_config.write_text(
        '[synthetic]\nrequires_api_key = true\napi_key_env = "SYNTHETIC_API_KEY"\n'
        'api_key = "dummy credential"\nencrypt_key = "dummy channel credential"\n',
        encoding="utf-8",
    )
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"
    collect_bundle(dest, home_dir=home, log_dir=log_dir)
    assert json.loads(_read_zip(dest)["config.redacted.json"]) == {"synthetic": {
        "requires_api_key": True,
        "api_key_env": "SYNTHETIC_API_KEY",
        "api_key": "[redacted]",
        "encrypt_key": "[redacted]",
    }}


def test_bundle_masks_custom_header_and_cli_credentials(tmp_path, _hermetic_config) -> None:
    _hermetic_config.write_text(
        '[memory.embedding.remote.headers]\n'
        '"X.Provider-Token" = "synthetic-header-credential"\n'
        '"定制_api_key" = "synthetic-unicode-credential"\n'
        '"apiKeyEnv" = "SYNTHETIC_API_KEY"\n',
        encoding="utf-8",
    )
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"
    result = collect_bundle(
        dest, home_dir=home, log_dir=log_dir,
        extra={"diagnostics": {
            "headers": {"Vendor.Key-Api-Key": "synthetic-live-credential"},
            "message": 'helper --api-key="synthetic-command-credential"',
            "requiresApiKey": True,
        }},
    )
    entries = _read_zip(dest)
    assert not result.manifest["collection_errors"]
    assert json.loads(entries["config.redacted.json"])["memory"]["embedding"]["remote"] == {
        "headers": {
            "X.Provider-Token": "[redacted]",
            "定制_api_key": "[redacted]",
            "apiKeyEnv": "SYNTHETIC_API_KEY",
        },
    }
    assert json.loads(entries["live/diagnostics.json"]) == {
        "headers": {"Vendor.Key-Api-Key": "[redacted]"},
        "message": 'helper --api-key="[redacted]"',
        "requiresApiKey": True,
    }
    assert b"-credential" not in b"".join(entries.values())


@pytest.mark.parametrize("header", [
    "X-AuthToken", "x-authtoken", "X-AUTHTOKEN", "x-accesstoken", "X-ACCESSTOKEN",
    "X-CSRFToken", "x-csrftoken", "X-CSRFTOKEN",
    "X-SecurityToken", "x-securitytoken", "X-SECURITYTOKEN",
    "X-ProviderApiKey", "x-providerapikey", "X-PROVIDERAPIKEY",
    "X.hasH_token", "X.IsLandToken",
    "X!Password", "X$Token", "X+ApiKey", "X%ClientSecret", "X'Authorization",
    "X`PrivateKey", "X|CSRFToken",
    "x!csrftoken", "x|securitytoken", "x+providerapikey", "x&securitytoken",
    "x'providerapikey",
])
def test_bundle_masks_compound_credentials_with_case_insensitive_headers(
    tmp_path, _hermetic_config, header,
) -> None:
    _hermetic_config.write_text(
        '[memory.embedding.remote.headers]\n'
        f'"{header}" = "synthetic-opaque-credential"\n',
        encoding="utf-8",
    )
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"
    result = collect_bundle(
        dest, home_dir=home, log_dir=log_dir,
        extra={
            "headers": {header: "synthetic-opaque-credential"},
            "details": {"message": f"{header}: synthetic-opaque-credential"},
        },
    )
    entries = _read_zip(dest)
    assert not result.manifest["collection_errors"]
    assert json.loads(entries["config.redacted.json"])["memory"]["embedding"]["remote"] == {
        "headers": {header: "[redacted]"},
    }
    assert json.loads(entries["live/headers.json"]) == {header: "[redacted]"}
    assert json.loads(entries["live/details.json"]) == {"message": f"{header}: [redacted]"}
    assert b"synthetic-opaque-credential" not in b"".join(entries.values())


def test_manifest_encoding_failure_fails_the_bundle(tmp_path, monkeypatch) -> None:
    from opensquilla.observability import bundle

    home, log_dir = _make_home(tmp_path)
    original_dumps = json.dumps

    def corrupt_manifest(value, **kwargs):
        if isinstance(value, dict) and "bundle_schema" in value:
            return '{"bundle_schema": invalid}'
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(bundle.json, "dumps", corrupt_manifest)
    dest = tmp_path / "bundle.zip"
    with pytest.raises(ValueError, match="Invalid JSON"):
        collect_bundle(dest, home_dir=home, log_dir=log_dir)
    assert "manifest.json" not in _read_zip(dest)


@pytest.mark.parametrize("folder", ["content", "decisions"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_capped_jsonl_has_parseable_truncation_record(tmp_path, folder, newline) -> None:
    from opensquilla.observability.bundle import _add_tail

    source = tmp_path / "synthetic.jsonl"
    tail = json.dumps({"kind": "llm_request", "apiKey": "dummy credential"}) + newline
    source.write_bytes((json.dumps({"password": "x" * 256}) + newline + tail).encode())
    dest = tmp_path / "bundle.zip"
    truncations = []
    entry = f"{folder}/synthetic.jsonl"
    with zipfile.ZipFile(dest, "w") as archive:
        _add_tail(archive, entry, source, truncations, cap=len(tail.encode()) + 8)
    data = _read_zip(dest)[entry]
    records = [json.loads(line) for line in data.splitlines()]
    assert records[0]["truncated"] is True
    assert records[1]["kind"] == "llm_request"
    assert b"dummy credential" not in data
    assert truncations[0]["entry"] == entry


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


def test_content_tier_filters_legacy_raw_rows_from_mixed_file(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    day = datetime.now(UTC).strftime("%Y%m%d")
    name = f"turn-calls-{day}.jsonl"
    records = [
        {"kind": "legacy_missing", "payload": {"text": "legacy-missing"}},
        {"agent_trace": False, "kind": "legacy_disabled", "payload": {"text": "legacy-false"}},
        {"agent_trace": "true", "kind": "legacy_string", "payload": {"text": "legacy-string"}},
        {
            "agent_trace": True,
            "kind": "llm_request",
            "payload": {"text": "trace-visible", "api_key": "synthetic credential"},
        },
    ]
    (log_dir / name).write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=True)

    data = _read_zip(dest)[f"content/{name}"]
    assert [json.loads(line) for line in data.splitlines()] == [
        {
            "agent_trace": True,
            "kind": "llm_request",
            "payload": {"text": "trace-visible", "api_key": "[redacted]"},
        }
    ]
    assert b"legacy-" not in data
    assert not result.manifest["collection_errors"]


def test_content_tier_omits_file_with_only_legacy_raw_rows(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path)
    day = datetime.now(UTC).strftime("%Y%m%d")
    name = f"turn-calls-{day}.jsonl"
    (log_dir / name).write_text(
        '{"kind":"legacy_missing","payload":{"text":"legacy-only"}}\n'
        '{"agent_trace":false,"kind":"legacy_disabled"}\n',
        encoding="utf-8",
    )
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=True)

    entries = _read_zip(dest)
    assert f"content/{name}" not in entries
    assert f"content/{name}" not in result.manifest["entries"]
    assert not result.manifest["collection_errors"]


def test_capped_agent_trace_content_has_no_unmarked_truncation_row(tmp_path) -> None:
    from opensquilla.observability.bundle import _add_agent_trace_turn_calls

    source = tmp_path / "turn-calls-synthetic.jsonl"
    legacy_line = json.dumps({"kind": "legacy", "payload": {"text": "x" * 256}}) + "\n"
    trace_line = json.dumps({"agent_trace": True, "kind": "llm_response"}) + "\n"
    source.write_text(legacy_line + trace_line, encoding="utf-8")
    dest = tmp_path / "bundle.zip"
    truncations = []
    entry_name = f"content/{source.name}"

    with zipfile.ZipFile(dest, "w") as archive:
        _add_agent_trace_turn_calls(
            archive, entry_name, source, truncations, cap=len(trace_line.encode()) + 8
        )

    rows = [json.loads(line) for line in _read_zip(dest)[entry_name].splitlines()]
    assert rows == [{"agent_trace": True, "kind": "llm_response"}]
    assert truncations[0]["entry"] == entry_name


@pytest.mark.parametrize("include_content", [False, True])
@pytest.mark.parametrize("configured", [None, False, True])
def test_bundle_trace_files_require_agent_trace_opt_in(
    tmp_path, _hermetic_config, include_content, configured
) -> None:
    config_text = (
        "# pre-trace config\n" if configured is None else
        f"[privacy]\nagent_trace_enabled = {str(configured).lower()}\n"
    )
    _hermetic_config.write_text(config_text, encoding="utf-8")
    home, log_dir = _make_home(tmp_path)
    day = datetime.now(UTC).strftime("%Y%m%d")
    dest = tmp_path / "bundle.zip"

    result = collect_bundle(dest, home_dir=home, log_dir=log_dir, include_content=include_content)

    entries = _read_zip(dest)
    assert f"decisions/decisions-{day}.jsonl" in entries
    assert (f"traces/traces-{day}.jsonl" in entries) is (configured is True)
    assert (f"content/turn-calls-{day}.jsonl" in entries) is (
        configured is True and include_content
    )
    assert set(result.manifest["entries"]) == set(entries)


def test_desktop_logs_are_derived_and_credential_excluded(tmp_path) -> None:
    home, log_dir = _make_home(tmp_path, desktop=True)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    entries = _read_zip(dest)
    assert "desktop/desktop.log" in entries
    assert "desktop/desktop.log.1" in entries
    assert "desktop/desktop.log.2" in entries
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


@pytest.mark.parametrize("old_config", [
    OUTDATED_TOML,
    '[control_ui]\nfrontend = "legacy"\n',
])
def test_doctor_collection_never_rewrites_outdated_config(
    tmp_path, _hermetic_config: Path, old_config: str,
) -> None:
    """collect_bundle must be byte-identical read-only, even on an outdated config.

    The doctor collector's config loader migrates outdated payloads in place
    (rewrite + *.backup.* sibling); the bundle must never let that reach the
    user's real file.
    """
    config_path = _hermetic_config
    config_path.write_text(old_config, encoding="utf-8")
    before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    home, log_dir = _make_home(tmp_path)
    dest = tmp_path / "bundle.zip"

    collect_bundle(dest, home_dir=home, log_dir=log_dir)

    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == before
    assert not list(config_path.parent.glob(f"{config_path.name}.backup*"))
    # The doctor artifact itself is still collected (from a throwaway copy).
    entries = _read_zip(dest)
    assert isinstance(json.loads(entries["doctor.json"]), dict)
    assert json.loads(entries["manifest.json"])["bundle_schema"] == 1


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
