"""Old feature settings are ignored without changing ordinary permissions."""

from __future__ import annotations

import copy

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_migration import migrate_config_payload


@pytest.mark.parametrize("value", [True, False, "obsolete", None, {"enabled": True}])
def test_retired_settings_are_discarded_before_validation(value):
    payload = {"meta_skill": value, "skills": {"coding_mode": value, "disabled": ["sample"]}}
    original = copy.deepcopy(payload)
    migrated = migrate_config_payload(payload, emit_diagnostics=False)
    assert payload == original
    assert set(migrated.removed_fields) >= {"meta_skill", "skills.coding_mode"}
    config = GatewayConfig(**payload)
    assert "meta_skill" not in config.model_dump()
    assert "coding_mode" not in config.skills.model_dump()
    assert config.skills.disabled == ["sample"]
    assert not migrate_config_payload(migrated.payload, emit_diagnostics=False).changed


def test_legacy_toml_loads_and_read_only_does_not_rewrite(tmp_path):
    path = tmp_path / "config.toml"
    original = '[skills]\ncoding_mode = true\ndisabled = ["sample"]\n[meta_skill]\nenabled = true\n'
    path.write_text(original)
    config = GatewayConfig.load(path, read_only=True)
    assert config.skills.disabled == ["sample"]
    assert path.read_text() == original
    GatewayConfig.load(path)
    rewritten = path.read_text()
    assert "coding_mode" not in rewritten
    assert "meta_skill" not in rewritten
