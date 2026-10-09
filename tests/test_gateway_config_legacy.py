"""Tests for legacy memory field fallback in GatewayConfig.

Verifies that deprecated memory.* fields in old config files are silently
dropped rather than causing ValidationError, and that a single aggregated
DeprecationWarning is emitted per process.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
import tomllib
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

import opensquilla.gateway.config as config_module
import opensquilla.gateway.config_migration as migration_module
from opensquilla.gateway.config import GatewayConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_DEPRECATED_MEMORY_FIELDS = {
    "memory.profile": "legacy_profile_value",
    "memory.cost.embedding_cache": "true",
    "memory.cost.rerank_cache": "false",
    "memory.cost.llm_judge_cache": "true",
    "memory.facts_enabled": "true",
    "memory.facts_top_k": "5",
    "memory.facts_max_chars": "2000",
    "memory.multi_hop_enabled": "false",
    "memory.multi_hop_max_depth": "3",
    "memory.multi_hop_score_threshold": "0.7",
    "memory.recall_frequency": "always",
    "memory.recall_top_k_default": "10",
    "memory.auto_recall_enabled": "true",
    "memory.prefetch_enabled": "true",
    "memory.prefetch_max_results": "3",
    "memory.prefetch_min_score": "0.3",
    "memory.prefetch_total_max_chars": "1500",
    "memory.semantic_chunking_enabled": "true",
    "memory.eviction_policy": "lru",
    "memory.summary_model": "gpt-4o-mini",
    "memory.summary_max_tokens": "256",
}


def _build_toml_with_deprecated(tmp_path: Path) -> Path:
    """Write a minimal config.toml that contains all deprecated fields."""
    lines = ["[memory]\n"]
    cost_lines = ["[memory.cost]\n"]

    for dotted, val in _ALL_DEPRECATED_MEMORY_FIELDS.items():
        parts = dotted.split(".")
        if parts[1] == "cost":
            leaf = parts[2]
            cost_lines.append(f'{leaf} = "{val}"\n')
        else:
            leaf = parts[1]
            lines.append(f'{leaf} = "{val}"\n')

    toml_path = tmp_path / "config.toml"
    toml_path.write_text("".join(lines) + "\n" + "".join(cost_lines))
    return toml_path


def test_prompt_mode_defaults_to_auto() -> None:
    cfg = GatewayConfig()

    assert cfg.prompt.mode == "auto"


def test_prompt_mode_accepts_headless_source_edit() -> None:
    cfg = GatewayConfig.model_validate({"prompt": {"mode": "headless_source_edit"}})

    assert cfg.prompt.mode == "headless_source_edit"


def test_prompt_mode_accepts_headless_repo_coding_scaffold() -> None:
    cfg = GatewayConfig.model_validate(
        {"prompt": {"mode": "headless_repo_coding_scaffold"}}
    )

    assert cfg.prompt.mode == "headless_repo_coding_scaffold"


# ---------------------------------------------------------------------------
# AC#1 / AC#4: loading does not raise
# ---------------------------------------------------------------------------


def test_load_with_all_deprecated_fields_does_not_raise(tmp_path: Path) -> None:
    """GatewayConfig.load() must succeed even when deprecated memory
    fields are present in the config file."""
    toml_path = _build_toml_with_deprecated(tmp_path)
    cfg = GatewayConfig.load(toml_path)
    assert isinstance(cfg, GatewayConfig)
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    backup_text = backups[-1].read_text(encoding="utf-8")
    assert "prefetch_enabled" in backup_text
    assert "embedding_cache" in backup_text
    text = toml_path.read_text(encoding="utf-8")
    assert "prefetch_enabled" not in text
    assert "embedding_cache" not in text


def test_load_migrates_010_turn_capture_fields(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[memory]",
                'capture_mode = "archive_turn_pair"',
                "index_captured_turns = true",
                "prefetch_enabled = true",
                "prefetch_max_results = 3",
                "prefetch_min_score = 0.3",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load(toml_path)

    assert cfg.memory.capture_mode == "turn_pair"
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    backup_text = backups[-1].read_text(encoding="utf-8")
    assert 'capture_mode = "archive_turn_pair"' in backup_text
    assert "index_captured_turns = true" in backup_text
    data = toml_path.read_text(encoding="utf-8")
    assert 'capture_mode = "turn_pair"' in data
    assert "archive_turn_pair" not in data
    assert "index_captured_turns" not in data
    assert "prefetch_enabled" not in data
    assert "prefetch_max_results" not in data
    assert "prefetch_min_score" not in data


def test_load_from_toml_migrates_010_turn_capture_fields(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[memory]",
                'capture_mode = "archive_turn_pair"',
                "index_captured_turns = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load_from_toml(toml_path)

    assert cfg.memory.capture_mode == "turn_pair"
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    backup_text = backups[-1].read_text(encoding="utf-8")
    assert 'capture_mode = "archive_turn_pair"' in backup_text
    data = toml_path.read_text(encoding="utf-8")
    assert 'capture_mode = "turn_pair"' in data
    assert "index_captured_turns" not in data


def test_load_migrates_legacy_agent_token_saving_fields(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[agent_token_saving]",
                "tool_result_compression_enabled = false",
                'tool_result_compression_mode = "off"',
                "tool_result_compression_max_share = 0.25",
                'tool_result_compression_summary_model = "z-ai/glm-4.5-air"',
                "tool_result_compression_summary_max_tokens = 512",
                "tool_result_compression_summary_timeout_seconds = 12.5",
                "tool_result_compression_summary_input_max_chars = 43210",
                "tool_result_store_max_bytes = 1234",
                "tool_result_store_disk_budget_bytes = 5678",
                "tool_result_store_retention_seconds = 90",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load(toml_path)

    assert cfg.agent_token_saving.tool_result_projection_max_inline_chars == 43210
    assert cfg.agent_token_saving.tool_result_store_max_bytes == 1234
    assert cfg.agent_token_saving.tool_result_store_disk_budget_bytes == 5678
    assert cfg.agent_token_saving.tool_result_store_retention_seconds == 90
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    backup_text = backups[-1].read_text(encoding="utf-8")
    assert "tool_result_compression_enabled = false" in backup_text
    migrated = toml_path.read_text(encoding="utf-8")
    assert "tool_result_compression_" not in migrated
    assert "tool_result_projection_max_inline_chars = 43210" in migrated
    assert "tool_result_store_max_bytes = 1234" in migrated


def test_legacy_agent_token_saving_migration_preserves_new_projection_setting(
    tmp_path: Path,
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[agent_token_saving]",
                "tool_result_projection_max_inline_chars = 22222",
                "tool_result_compression_summary_input_max_chars = 60000",
                "tool_result_compression_enabled = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load_from_toml(toml_path)

    assert cfg.agent_token_saving.tool_result_projection_max_inline_chars == 22222
    migrated = toml_path.read_text(encoding="utf-8")
    assert "tool_result_projection_max_inline_chars = 22222" in migrated
    assert "tool_result_compression_" not in migrated


def test_legacy_agent_token_saving_migration_keeps_runtime_schema_strict(
    tmp_path: Path,
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[agent_token_saving]",
                "tool_result_compression_enabled = true",
                "tool_result_compression_summary_input_max_chars = 60000",
                "typo_field = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        GatewayConfig.load(toml_path)

    assert "agent_token_saving.typo_field" in str(exc_info.value)


@pytest.mark.parametrize("legacy_timeout", [120.0, 300.0])
def test_load_migrates_legacy_llm_ensemble_timeout_defaults(
    tmp_path: Path, legacy_timeout: float
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "\n".join(
            [
                "[llm_ensemble]",
                "enabled = true",
                f"proposer_timeout_seconds = {legacy_timeout:.1f}",
                f"aggregator_timeout_seconds = {legacy_timeout:.1f}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load(toml_path)

    assert cfg.llm_ensemble.proposer_timeout_seconds == 3600.0
    assert cfg.llm_ensemble.aggregator_timeout_seconds == 3600.0
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups
    migrated = toml_path.read_text(encoding="utf-8")
    assert "proposer_timeout_seconds = 3600.0" in migrated
    assert "aggregator_timeout_seconds = 3600.0" in migrated


def test_llm_ensemble_timeout_migration_preserves_custom_values() -> None:
    result = migration_module.migrate_config_payload(
        {
            "llm_ensemble": {
                "enabled": True,
                "proposer_timeout_seconds": 180.0,
                "aggregator_timeout_seconds": 240.0,
            }
        }
    )

    assert result.changed is False
    assert result.payload["llm_ensemble"]["proposer_timeout_seconds"] == 180.0
    assert result.payload["llm_ensemble"]["aggregator_timeout_seconds"] == 240.0


def test_llm_ensemble_timeout_migration_preserves_mixed_legacy_and_custom_values() -> None:
    result = migration_module.migrate_config_payload(
        {
            "llm_ensemble": {
                "enabled": True,
                "proposer_timeout_seconds": 120.0,
                "aggregator_timeout_seconds": 900.0,
            }
        }
    )

    assert result.changed is False
    assert result.payload["llm_ensemble"]["proposer_timeout_seconds"] == 120.0
    assert result.payload["llm_ensemble"]["aggregator_timeout_seconds"] == 900.0


@pytest.mark.parametrize(
    "legacy_schema",
    ["fixed-four-tier-v2-mock-v1", "fixed-four-tier-v2-mock-v2"],
)
def test_fixed_four_tier_compat_migration_is_always_run_and_idempotent(
    legacy_schema: str,
) -> None:
    payload = {
        "config_version": migration_module.LATEST_CONFIG_VERSION,
        "llm_ensemble": {
            "four_tier_mapping": {
                "schema_version": legacy_schema,
                "mock_seed": 17,
                "max_session_states": 128,
            }
        },
    }
    original = copy.deepcopy(payload)

    result = migration_module.migrate_config_payload(payload)

    assert payload == original
    assert result.changed is True
    route = result.payload["llm_ensemble"]["four_tier_mapping"]
    assert route == {
        "schema_version": "fixed-four-tier-v2-v4",
        "classifier": {"backend": "random_mock", "seed": 17},
    }
    assert (
        "llm_ensemble.four_tier_mapping.mock_seed"
        in result.removed_fields
    )
    assert (
        "llm_ensemble.four_tier_mapping.max_session_states"
        in result.removed_fields
    )
    assert any("schema_version" in change for change in result.changes)
    assert any("classifier.seed" in change for change in result.changes)

    second = migration_module.migrate_config_payload(result.payload)
    assert second.payload == result.payload
    assert second.changed is False
    assert second.changes == ()
    assert second.removed_fields == ()


def test_fixed_four_tier_top_level_seed_normalizes_in_current_schema() -> None:
    result = migration_module.migrate_config_payload(
        {
            "config_version": migration_module.LATEST_CONFIG_VERSION,
            "llm_ensemble": {
                "four_tier_mapping": {
                    "schema_version": "fixed-four-tier-v2-v4",
                    "mock_seed": 19,
                }
            },
        }
    )

    route = result.payload["llm_ensemble"]["four_tier_mapping"]
    assert route["classifier"] == {"backend": "random_mock", "seed": 19}
    assert "mock_seed" not in route
    assert result.changed is True


def test_v1_selection_only_four_tier_config_materializes_explicit_legacy_mock() -> None:
    payload = {
        "config_version": 1,
        "llm_ensemble": {
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
        },
    }

    result = migration_module.migrate_config_payload(payload)

    assert result.changed is True
    assert result.payload["config_version"] == 2
    assert result.payload["llm_ensemble"]["four_tier_mapping"] == {
        "schema_version": "fixed-four-tier-v2-v4",
        "classifier": {"backend": "random_mock"},
    }
    assert any("legacy random_mock" in change for change in result.changes)

    second = migration_module.migrate_config_payload(result.payload)
    assert second.payload == result.payload
    assert second.changed is False


def test_load_from_toml_persists_fixed_four_tier_compat_migration(
    tmp_path: Path,
) -> None:
    toml_path = tmp_path / "config.toml"
    original = "\n".join(
        [
            f"config_version = {migration_module.LATEST_CONFIG_VERSION}",
            "",
            "[llm_ensemble]",
            "enabled = true",
            'mode = "single"',
            'selection_mode = "four_tier_mapping"',
            "",
            "[llm_ensemble.four_tier_mapping]",
            'schema_version = "fixed-four-tier-v2-mock-v2"',
            "mock_seed = 23",
            "max_session_states = 128",
            "",
        ]
    )
    toml_path.write_text(original, encoding="utf-8")

    cfg = GatewayConfig.load_from_toml(toml_path)

    route = cfg.llm_ensemble.four_tier_mapping
    assert route is not None
    assert route.schema_version == "fixed-four-tier-v2-v4"
    assert route.mock_seed == 23
    [backup] = list(tmp_path.glob("config.toml.backup.*"))
    assert backup.read_text(encoding="utf-8") == original

    migrated = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    migrated_route = migrated["llm_ensemble"]["four_tier_mapping"]
    assert migrated_route == {
        "schema_version": "fixed-four-tier-v2-v4",
        "classifier": {"backend": "random_mock", "seed": 23},
    }

    persisted = toml_path.read_text(encoding="utf-8")
    GatewayConfig.load_from_toml(toml_path)
    assert toml_path.read_text(encoding="utf-8") == persisted
    assert len(list(tmp_path.glob("config.toml.backup.*"))) == 1


# ---------------------------------------------------------------------------
# AC#5: aggregate DeprecationWarning emitted once per process
# ---------------------------------------------------------------------------


def test_aggregate_deprecation_warning_emitted_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single DeprecationWarning is emitted the first time deprecated memory
    fields are encountered; subsequent loads with the same fields are silent."""
    # Reset the process-level sentinels so this test is not affected by order.
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_WARNED", False)
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_SEEN", set())

    toml_path = _build_toml_with_deprecated(tmp_path)

    with pytest.warns(DeprecationWarning) as record:
        GatewayConfig.load(toml_path)
        # Second load — sentinel is now True, should not add another warning.
        GatewayConfig.load(toml_path)

    deprecation_warnings = [
        w for w in record.list if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecation_warnings) == 1, (
        f"Expected exactly 1 DeprecationWarning, got {len(deprecation_warnings)}: "
        f"{[str(w.message) for w in deprecation_warnings]}"
    )
    msg = str(deprecation_warnings[0].message)
    assert "memory" in msg.lower()
    assert f"{len(_ALL_DEPRECATED_MEMORY_FIELDS)} legacy memory.* config field(s) ignored" in msg
    assert "0.2.0" in msg


# ---------------------------------------------------------------------------
# AC#6: log file written with per-field detail
# ---------------------------------------------------------------------------


def test_log_file_written_with_per_field_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After loading a config with deprecated fields, a .log file must exist
    under ~/.opensquilla/logs/ containing one JSON line per deprecated field."""
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_WARNED", False)
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_SEEN", set())

    # Redirect opensquilla home to tmp_path so the log lands there.
    monkeypatch.setattr(migration_module, "default_opensquilla_home", lambda: tmp_path)

    toml_path = _build_toml_with_deprecated(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        GatewayConfig.load(toml_path)

    logs_dir = tmp_path / "logs"
    assert logs_dir.exists(), "logs/ directory was not created"

    log_files = sorted(logs_dir.glob("legacy_config_*.log"))
    assert len(log_files) >= 1, f"No legacy_config_*.log found in {logs_dir}"

    log_file = log_files[-1]
    log_text = log_file.read_text(encoding="utf-8")
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    expected_count = len(_ALL_DEPRECATED_MEMORY_FIELDS)
    assert len(lines) == expected_count, f"Expected {expected_count} log lines, got {len(lines)}"

    entries_by_field: dict[str, dict[str, object]] = {}
    for line in lines:
        entry = json.loads(line)
        assert "field" in entry
        assert "timestamp" in entry
        assert "source" in entry
        assert "value_repr" not in entry
        entries_by_field[str(entry["field"])] = entry

    profile_entry = entries_by_field["memory.profile"]
    assert profile_entry["value_type"] == "string"
    assert profile_entry["value_shape"] == {"length": len("legacy_profile_value")}
    assert "legacy_profile_value" not in log_text
    if os.name != "nt":
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_legacy_log_falls_back_when_fchmod_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without ``os.fchmod`` still write a redacted private log."""
    monkeypatch.setattr(migration_module, "default_opensquilla_home", lambda: tmp_path)
    monkeypatch.delattr(migration_module.os, "fchmod", raising=False)
    real_chmod = migration_module.os.chmod
    chmod_calls: list[tuple[Path, int]] = []

    def recording_chmod(path: str | Path, mode: int) -> None:
        chmod_calls.append((Path(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(migration_module.os, "chmod", recording_chmod)

    migration_module._write_legacy_field_log(
        {"memory.profile": "synthetic-private-value"},
        "config_migration",
    )

    [log_file] = list((tmp_path / "logs").glob("legacy_config_*.log"))
    log_text = log_file.read_text(encoding="utf-8")
    assert "synthetic-private-value" not in log_text
    assert chmod_calls == [(log_file, 0o600)]


def test_migrate_config_payload_can_disable_process_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dry-run can migrate without logs, warnings, or sentinel mutation."""
    memory_seen = {"memory.preexisting"}
    token_saving_seen = {"agent_token_saving.preexisting"}
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_WARNED", False)
    monkeypatch.setattr(migration_module, "_LEGACY_MEMORY_FIELDS_SEEN", memory_seen)
    monkeypatch.setattr(
        migration_module,
        "_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED",
        False,
    )
    monkeypatch.setattr(
        migration_module,
        "_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN",
        token_saving_seen,
    )
    monkeypatch.setattr(migration_module, "default_opensquilla_home", lambda: tmp_path)
    caplog.set_level(logging.WARNING, logger=migration_module.__name__)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = migration_module.migrate_config_payload(
            {
                "memory": {"profile": "synthetic-private-memory-setting"},
                "agent_token_saving": {
                    "tool_result_compression_enabled": False,
                },
                "channels": {
                    "channels": [
                        {
                            "type": "synthetic-removed-channel",
                            "name": "synthetic-private-channel-name",
                        }
                    ]
                },
            },
            emit_diagnostics=False,
        )

    assert result.changed is True
    assert "memory.profile" in result.removed_fields
    assert (
        "agent_token_saving.tool_result_compression_enabled"
        in result.removed_fields
    )
    assert caught == []
    assert not [record for record in caplog.records if record.name == migration_module.__name__]
    assert migration_module._LEGACY_MEMORY_FIELDS_WARNED is False
    assert migration_module._LEGACY_MEMORY_FIELDS_SEEN == {"memory.preexisting"}
    assert migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED is False
    assert migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN == {
        "agent_token_saving.preexisting"
    }
    assert not (tmp_path / "logs").exists()


def test_legacy_log_redacts_parked_channel_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field detail for nested values uses an index and shape, never contents."""
    monkeypatch.setattr(migration_module, "default_opensquilla_home", lambda: tmp_path)

    result = migration_module.migrate_config_payload(
        {
            "channels": {
                "channels": [
                    {
                        "type": "synthetic-private-channel-type",
                        "name": "synthetic-private-channel-name",
                        "token": "synthetic-private-channel-token",
                    }
                ]
            }
        }
    )

    assert result.changed is True
    [log_file] = list((tmp_path / "logs").glob("legacy_config_*.log"))
    log_text = log_file.read_text(encoding="utf-8")
    entry = json.loads(log_text)
    assert entry["field"] == "channels.channels[0]"
    assert entry["value_type"] == "mapping"
    assert entry["value_shape"] == {"entries": 3}
    assert "synthetic-private-channel-type" not in log_text
    assert "synthetic-private-channel-name" not in log_text
    assert "synthetic-private-channel-token" not in log_text


# ---------------------------------------------------------------------------
# AC#8 guard: no OPENSQUILLA_LEGACY_FALLBACK env switch
# ---------------------------------------------------------------------------


def test_no_legacy_fallback_env_var() -> None:
    """The source must not contain an OPENSQUILLA_LEGACY_FALLBACK env switch
    (ADR-3 prohibits runtime opt-out of the fallback)."""
    import inspect
    source = inspect.getsource(config_module)
    assert "OPENSQUILLA_LEGACY_FALLBACK" not in source


class TestMetaSkillConfig:
    """C4: GatewayConfig must accept [meta_skill.persistence] section."""

    def test_default_meta_skill_config(self) -> None:
        from opensquilla.gateway.config import GatewayConfig

        cfg = GatewayConfig()
        assert cfg.meta_skill.enabled is True
        assert cfg.meta_skill.persistence.enabled is True
        assert cfg.meta_skill.persistence.orphan_cleanup_age_seconds == 3600

    def test_meta_skill_can_be_disabled_globally(self) -> None:
        from opensquilla.gateway.config import GatewayConfig

        cfg = GatewayConfig(
            meta_skill={"enabled": False},
        )
        assert cfg.meta_skill.enabled is False

    def test_meta_skill_persistence_disabled(self) -> None:
        from opensquilla.gateway.config import GatewayConfig

        cfg = GatewayConfig(
            meta_skill={"persistence": {"enabled": False}},
        )
        assert cfg.meta_skill.persistence.enabled is False

    def test_meta_skill_env_override(self, monkeypatch) -> None:
        from opensquilla.gateway.config import MetaSkillConfig

        monkeypatch.setenv("OPENSQUILLA_META_SKILL_ENABLED", "false")
        cfg = MetaSkillConfig()
        assert cfg.enabled is False

    def test_meta_skill_persistence_env_override(self, monkeypatch) -> None:
        from opensquilla.gateway.config import MetaSkillPersistenceConfig

        monkeypatch.setenv("OPENSQUILLA_META_SKILL_PERSISTENCE_ENABLED", "false")
        cfg = MetaSkillPersistenceConfig()
        assert cfg.enabled is False

    def test_example_toml_parses_clean(self) -> None:
        """Copying opensquilla.toml.example to ~/.opensquilla/config.toml must work."""
        import tomllib
        from pathlib import Path

        from opensquilla.gateway.config import GatewayConfig

        example_path = Path(__file__).resolve().parents[1] / "opensquilla.toml.example"
        with example_path.open("rb") as f:
            data = tomllib.load(f)

        # No exceptions during validation
        GatewayConfig(**data)


def test_gateway_config_accepts_repo_coding_source_edit_tool_profile() -> None:
    cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_source_edit"}}
    )

    assert cfg.tools.profile == "repo_coding_source_edit"


def test_gateway_config_accepts_repo_coding_source_edit_strict_tool_profile() -> None:
    cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_source_edit_strict"}}
    )

    assert cfg.tools.profile == "repo_coding_source_edit_strict"


def test_gateway_config_accepts_repo_coding_source_edit_v2_tool_profile() -> None:
    cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_source_edit_v2"}}
    )

    assert cfg.tools.profile == "repo_coding_source_edit_v2"


def test_gateway_config_accepts_repo_coding_source_edit_balanced_tool_profile() -> None:
    cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_source_edit_balanced"}}
    )

    assert cfg.tools.profile == "repo_coding_source_edit_balanced"


def test_gateway_config_accepts_repo_coding_source_edit_patch_fallback_tool_profile() -> None:
    cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_source_edit_patch_fallback"}}
    )

    assert cfg.tools.profile == "repo_coding_source_edit_patch_fallback"


def test_gateway_config_accepts_repo_coding_scaffold_tool_profiles() -> None:
    edit_cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_scaffold_edit"}}
    )
    patch_cfg = GatewayConfig.model_validate(
        {"tools": {"profile": "repo_coding_scaffold_patch"}}
    )

    assert edit_cfg.tools.profile == "repo_coding_scaffold_edit"
    assert patch_cfg.tools.profile == "repo_coding_scaffold_patch"


def test_gateway_config_accepts_llm_sampling_controls() -> None:
    cfg = GatewayConfig.model_validate({"llm": {"temperature": 1.0, "top_p": 0.95}})

    assert cfg.llm.temperature == 1.0
    assert cfg.llm.top_p == 0.95


def test_gateway_config_accepts_source_diff_preservation_mode() -> None:
    cfg = GatewayConfig.model_validate({"source_diff_preservation_mode": "block"})

    assert cfg.source_diff_preservation_mode == "block"


def test_gateway_config_accepts_source_diff_candidate_mode() -> None:
    cfg = GatewayConfig.model_validate({"source_diff_candidate_mode": "warn_model"})

    assert cfg.source_diff_candidate_mode == "warn_model"


def test_gateway_config_accepts_runtime_state_capsule_mode() -> None:
    cfg = GatewayConfig.model_validate({"runtime_state_capsule_mode": "inject"})

    assert cfg.runtime_state_capsule_mode == "inject"


def test_gateway_config_accepts_text_only_tool_recovery_mode() -> None:
    cfg = GatewayConfig.model_validate({"text_only_tool_recovery_mode": "warn_model"})

    assert cfg.text_only_tool_recovery_mode == "warn_model"



def test_four_tier_v3_migration_preserves_registered_classifier(tmp_path: Path) -> None:
    classifier = {
        "backend": "registered_model",
        "artifact_root": str(tmp_path / "artifacts"),
        "metadata_db": str(tmp_path / "models.sqlite"),
        "model_set_id": "registered-test-router",
        "expected_manifest_hash": "sha256:" + "a" * 64,
        "allow_candidate": True,
    }
    payload = {
        "config_version": migration_module.LATEST_CONFIG_VERSION,
        "llm_ensemble": {
            "four_tier_mapping": {
                "schema_version": "fixed-four-tier-v2-v3",
                "classifier": classifier,
                "tier_min_confidence": 0.8,
            },
        },
    }
    original = copy.deepcopy(payload)
    result = migration_module.migrate_config_payload(payload)
    assert payload == original
    route = result.payload["llm_ensemble"]["four_tier_mapping"]
    assert route == {
        "schema_version": "fixed-four-tier-v2-v4",
        "classifier": classifier,
        "tier_min_confidence": 0.8,
    }
    assert result.changed is True
    assert result.removed_fields == ()
    assert migration_module.migrate_config_payload(result.payload).changed is False


def test_four_tier_v3_migration_without_classifier_stays_invalid() -> None:
    result = migration_module.migrate_config_payload({
        "llm_ensemble": {
            "four_tier_mapping": {"schema_version": "fixed-four-tier-v2-v3"},
        },
    })
    route = result.payload["llm_ensemble"]["four_tier_mapping"]
    assert route == {"schema_version": "fixed-four-tier-v2-v4"}
    with pytest.raises(ValidationError, match="classifier"):
        GatewayConfig.model_validate(result.payload)



def test_load_from_toml_persists_v3_upgrade_without_changing_registered_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registered.toml"
    original = "\n".join([
        f"config_version = {migration_module.LATEST_CONFIG_VERSION}",
        "[llm_ensemble]",
        "enabled = true",
        'mode = "single"',
        'selection_mode = "four_tier_mapping"',
        "[llm_ensemble.four_tier_mapping]",
        'schema_version = "fixed-four-tier-v2-v3"',
        "tier_min_confidence = 0.8",
        "[llm_ensemble.four_tier_mapping.classifier]",
        'backend = "registered_model"',
        f'artifact_root = "{tmp_path / "artifacts"}"',
        f'metadata_db = "{tmp_path / "models.sqlite"}"',
        'model_set_id = "registered-test-router"',
        'expected_manifest_hash = "sha256:' + "a" * 64 + '"',
        "allow_candidate = true",
        "",
    ])
    path.write_text(original)
    cfg = GatewayConfig.load_from_toml(path)
    route = cfg.llm_ensemble.four_tier_mapping
    assert route is not None
    assert route.schema_version == "fixed-four-tier-v2-v4"
    assert route.classifier.backend == "registered_model"
    assert route.classifier.model_set_id == "registered-test-router"
    assert route.classifier.allow_candidate is True
    assert route.tier_min_confidence == 0.8
    migrated = tomllib.loads(path.read_text())["llm_ensemble"]["four_tier_mapping"]
    assert migrated["schema_version"] == "fixed-four-tier-v2-v4"
    assert migrated["classifier"]["backend"] == "registered_model"
    [backup] = list(tmp_path.glob("registered.toml.backup.*"))
    assert backup.read_text() == original
    persisted = path.read_text()
    GatewayConfig.load_from_toml(path)
    assert path.read_text() == persisted
    assert len(list(tmp_path.glob("registered.toml.backup.*"))) == 1
