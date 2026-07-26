"""Durable probe-history records behind the configured-provider rows."""

from __future__ import annotations

import json

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.probe_history import (
    last_probe_payload,
    load_probe_history,
    record_probe,
    saved_deployment_fingerprint,
)
from opensquilla.onboarding.status import get_onboarding_status


def _config(tmp_path, **overrides) -> GatewayConfig:
    values = {
        "state_dir": str(tmp_path / "state"),
        "llm": {"provider": "openai", "model": "gpt-test", "api_key": "sk-active"},
        **overrides,
    }
    return GatewayConfig(**values)


def test_record_then_load_roundtrip(tmp_path) -> None:
    cfg = _config(tmp_path)

    record_probe(cfg, "OpenAI", ok=True)
    history = load_probe_history(cfg)

    record = history["openai"]
    assert record["ok"] is True
    assert record["at"]
    assert record["fingerprint"] == saved_deployment_fingerprint(cfg, "openai")
    assert record["failureKind"] == ""


def test_record_failure_outcome_keeps_failure_kind(tmp_path) -> None:
    cfg = _config(tmp_path)

    record_probe(cfg, "openai", ok=False, failure_kind="auth_invalid")

    record = load_probe_history(cfg)["openai"]
    assert record["ok"] is False
    assert record["failureKind"] == "auth_invalid"


def test_history_file_never_contains_key_material(tmp_path) -> None:
    cfg = _config(tmp_path)

    record_probe(cfg, "openai", ok=True)

    raw = (tmp_path / "state" / "onboarding" / "probe_history.json").read_text()
    assert "sk-active" not in raw


def test_fingerprint_tracks_credentials_and_endpoint_not_model(tmp_path) -> None:
    cfg = _config(tmp_path)
    baseline = saved_deployment_fingerprint(cfg, "openai")

    same_model_changed = _config(
        tmp_path, llm={"provider": "openai", "model": "gpt-other", "api_key": "sk-active"}
    )
    key_changed = _config(
        tmp_path, llm={"provider": "openai", "model": "gpt-test", "api_key": "sk-rotated"}
    )
    url_changed = _config(
        tmp_path,
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "sk-active",
            "base_url": "https://alt.example/v1",
        },
    )

    assert saved_deployment_fingerprint(same_model_changed, "openai") == baseline
    assert saved_deployment_fingerprint(key_changed, "openai") != baseline
    assert saved_deployment_fingerprint(url_changed, "openai") != baseline


def test_fingerprint_reads_profile_identity_for_non_active_provider(tmp_path) -> None:
    cfg = _config(
        tmp_path,
        llm_profiles={"deepseek": {"api_key": "sk-profile", "base_url": "https://a.example"}},
    )
    baseline = saved_deployment_fingerprint(cfg, "deepseek")

    rotated = _config(
        tmp_path,
        llm_profiles={"deepseek": {"api_key": "sk-other", "base_url": "https://a.example"}},
    )
    pool_changed = _config(
        tmp_path,
        llm_profiles={
            "deepseek": {
                "api_key": "sk-profile",
                "base_url": "https://a.example",
                "api_key_env_pool": ["DEEPSEEK_A"],
            }
        },
    )

    assert saved_deployment_fingerprint(rotated, "deepseek") != baseline
    assert saved_deployment_fingerprint(pool_changed, "deepseek") != baseline


def test_last_probe_payload_flags_config_change() -> None:
    record = {"ok": True, "at": "2026-07-23T00:00:00+00:00", "fingerprint": "abc"}

    fresh = last_probe_payload(record, "abc")
    stale = last_probe_payload(record, "def")

    assert fresh == {
        "ok": True,
        "at": "2026-07-23T00:00:00+00:00",
        "configChanged": False,
        "failureKind": "",
    }
    assert stale is not None and stale["configChanged"] is True
    assert last_probe_payload(None, "abc") is None
    assert last_probe_payload({"ok": True, "at": ""}, "abc") is None


def test_corrupt_history_file_degrades_to_empty(tmp_path) -> None:
    cfg = _config(tmp_path)
    path = tmp_path / "state" / "onboarding" / "probe_history.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert load_probe_history(cfg) == {}
    record_probe(cfg, "openai", ok=True)
    assert load_probe_history(cfg)["openai"]["ok"] is True


def test_status_rows_surface_last_probe_and_config_change(tmp_path) -> None:
    cfg = _config(
        tmp_path,
        llm_profiles={"deepseek": {"model": "deepseek-chat", "api_key": "sk-profile"}},
    )
    record_probe(cfg, "openai", ok=True)
    record_probe(cfg, "deepseek", ok=False, failure_kind="auth_invalid")

    status = get_onboarding_status(cfg, probe_history=load_probe_history(cfg))
    rows = {str(row["provider"]): row for row in status.llm_profile_status}

    active = rows["openai"]["lastProbe"]
    assert active == {
        "ok": True,
        "at": active["at"],
        "configChanged": False,
        "failureKind": "",
    }
    profile = rows["deepseek"]["lastProbe"]
    assert profile["ok"] is False
    assert profile["failureKind"] == "auth_invalid"

    rotated = _config(
        tmp_path,
        llm={"provider": "openai", "model": "gpt-test", "api_key": "sk-rotated"},
        llm_profiles={"deepseek": {"model": "deepseek-chat", "api_key": "sk-profile"}},
    )
    rotated_status = get_onboarding_status(
        rotated, probe_history=load_probe_history(rotated)
    )
    rotated_rows = {str(row["provider"]): row for row in rotated_status.llm_profile_status}
    assert rotated_rows["openai"]["lastProbe"]["configChanged"] is True
    assert rotated_rows["deepseek"]["lastProbe"]["configChanged"] is False


def test_status_without_history_keeps_rows_unchanged(tmp_path) -> None:
    cfg = _config(tmp_path)
    record_probe(cfg, "openai", ok=True)

    status = get_onboarding_status(cfg)

    assert all("lastProbe" not in row for row in status.llm_profile_status)


def test_schema_version_written(tmp_path) -> None:
    cfg = _config(tmp_path)
    record_probe(cfg, "openai", ok=True)

    raw = json.loads(
        (tmp_path / "state" / "onboarding" / "probe_history.json").read_text()
    )
    assert raw["schemaVersion"] == 1
