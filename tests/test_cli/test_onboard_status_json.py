"""Shape freeze for ``opensquilla onboard status --json``.

The CLI JSON payload must stay a strict superset of the RPC
``onboarding.status`` payload (contract-frozen in
tests/test_contracts/test_onboarding_status.py): every RPC key appears here
under the same name, and ``sectionAliases`` (plus the ``provider`` alias
entries inside ``sections``/``sectionDetails``) is the only CLI-side
addition. Renaming or dropping a key is a contract break and must fail here;
adding one requires consciously extending the frozen set below.
"""

from __future__ import annotations

import json as _json

from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()

# RPC onboarding.status keys (mirrors STATUS_TOP_LEVEL_KEYS in
# tests/test_contracts/test_onboarding_status.py) — every one of these must
# also appear in the CLI JSON payload.
RPC_STATUS_KEYS = frozenset(
    {
        "configPath",
        "hasConfig",
        "llmConfigured",
        "llmSource",
        "llmEnvKey",
        "llmCredentialStatus",
        "llmProfileStatus",
        "imageGenerationConfigured",
        "imageGenerationEnabled",
        "imageGenerationSource",
        "imageGenerationProvider",
        "imageGenerationPrimary",
        "imageGenerationEnvKey",
        "audioConfigured",
        "audioEnabled",
        "audioSource",
        "audioProvider",
        "audioEnvKey",
        "searchConfigured",
        "searchProvider",
        "searchSource",
        "searchEnvKey",
        "memoryEmbeddingConfigured",
        "memoryEmbeddingProvider",
        "memoryEmbeddingSource",
        "memoryEmbeddingEnvKey",
        "channelCount",
        "channelsConfigured",
        "ensembleCredentialStatus",
        "needsOnboarding",
        "sections",
        "sectionDetails",
        "envRecoveryCommands",
        "warnings",
        # Nullable legacy-data advisory block (see the deliberate additive
        # extension in tests/test_contracts/test_onboarding_status.py).
        "legacyData",
    }
)

# Exact CLI JSON shape: the RPC keys plus the CLI-only alias map.
CLI_STATUS_KEYS = RPC_STATUS_KEYS | {"sectionAliases"}


def _write_config(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "DUMMY_UNSET_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DUMMY_UNSET_LLM_KEY", raising=False)
    return target


def _status_json(target):
    result = runner.invoke(app, ["onboard", "status", "--json", "--config", str(target)])
    assert result.exit_code == 0, result.output
    return _json.loads(result.stdout)


def test_status_json_key_set_is_frozen(tmp_path, monkeypatch):
    payload = _status_json(_write_config(tmp_path, monkeypatch))

    assert set(payload) == CLI_STATUS_KEYS


def test_status_json_is_a_superset_of_the_rpc_payload(tmp_path, monkeypatch):
    """Anti-drift: compare against the live RPC payload, not a copied list."""
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_onboarding import _status_payload as rpc_status_payload
    from opensquilla.onboarding.config_store import load_config

    target = _write_config(tmp_path, monkeypatch)
    cli_payload = _status_json(target)
    rpc_payload = rpc_status_payload(
        RpcContext(conn_id="cli-freeze", config=load_config(target))
    )

    missing = set(rpc_payload) - set(cli_payload)
    assert not missing, f"CLI status --json lost RPC keys: {sorted(missing)}"
    assert set(cli_payload) - set(rpc_payload) == {"sectionAliases"}


def test_status_json_new_keys_carry_the_expected_values(tmp_path, monkeypatch):
    payload = _status_json(_write_config(tmp_path, monkeypatch))

    assert payload["llmConfigured"] is False
    assert payload["llmSource"] == "missing_env"
    credential = payload["llmCredentialStatus"]
    assert credential["provider"] == "openrouter"
    assert credential["available"] is False
    assert credential["source"] == "missing_env"
    assert credential["envKey"] == "DUMMY_UNSET_LLM_KEY"
    profiles = payload["llmProfileStatus"]
    assert len(profiles) == 1
    assert profiles[0]["provider"] == "openrouter"
    assert profiles[0]["ready"] is False
    assert profiles[0]["credentialSource"] == "none"
    assert "apiKey" not in profiles[0]
    assert payload["audioConfigured"] is False
    assert payload["audioEnabled"] is False
    # Default search provider (duckduckgo) needs no key, so the section is
    # already configured on a fresh config.
    assert payload["searchConfigured"] is True
    assert payload["channelsConfigured"] is False
    assert isinstance(payload["ensembleCredentialStatus"], list)
    assert isinstance(payload["warnings"], list)
    assert payload["legacyData"] is None


def test_status_json_command_field_is_bare_on_posix(tmp_path, monkeypatch):
    from opensquilla.onboarding import next_steps

    monkeypatch.setattr(next_steps.platform, "system", lambda: "Linux")
    payload = _status_json(_write_config(tmp_path, monkeypatch))

    commands = payload["envRecoveryCommands"]
    assert commands
    assert commands[0]["command"] == 'export DUMMY_UNSET_LLM_KEY="<your-key>"'


def test_status_json_command_field_has_no_shell_label_on_windows(tmp_path, monkeypatch):
    """Machine-readable command fields must contain only the command."""
    from opensquilla.onboarding import next_steps

    monkeypatch.setattr(next_steps.platform, "system", lambda: "Windows")
    payload = _status_json(_write_config(tmp_path, monkeypatch))

    commands = payload["envRecoveryCommands"]
    assert commands
    assert commands[0]["command"] == '$env:DUMMY_UNSET_LLM_KEY = "<your-key>"'
    assert all("PowerShell" not in entry["command"] for entry in commands)
