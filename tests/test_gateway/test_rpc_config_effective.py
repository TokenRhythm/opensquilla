"""RPC handler tests for ``config.effective`` (provenance view).

Follows the rpc-handler pattern from test_rpc_doctor.py: handlers are
invoked directly with a bare ``RpcContext``. Scope mechanics are asserted
against the central table because the ``config.`` prefix defaults to admin
and the boot audit hard-fails on declared-vs-table drift.
"""

from __future__ import annotations

from typing import Any

import pytest

import opensquilla.gateway.rpc_config as rpc_config
import opensquilla.provider.resolution as resolution
from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.rpc import RpcContext, get_registry, validate_classification
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, resolve_required_scope
from opensquilla.provider.resolution import ResolvedField


@pytest.fixture()
def cfg(tmp_path, monkeypatch: pytest.MonkeyPatch) -> GatewayConfig:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    return GatewayConfig(config_path=str(tmp_path / "opensquilla.toml"))


def _ctx(config: Any) -> RpcContext:
    return RpcContext(conn_id="test", config=config)


# --- scope registration -------------------------------------------------------


def test_config_effective_is_explicitly_read_scoped() -> None:
    # An explicit METHOD_SCOPES entry is mandatory: without it the `config.`
    # admin prefix would apply and the boot audit would reject the handler's
    # declared operator.read scope.
    assert METHOD_SCOPES["config.effective"] == READ_SCOPE
    assert resolve_required_scope("config.effective") == READ_SCOPE


def test_config_effective_registered_and_boot_audit_passes() -> None:
    registry = get_registry()
    entry = registry.get_entry("config.effective")
    assert entry is not None
    assert entry.required_scope == READ_SCOPE
    # The boot audit (declared-vs-table drift check) must accept the full
    # registered surface including the new method. Passing the registry
    # explicitly audits without re-locking registration.
    validate_classification(registry)


# --- handler behavior ----------------------------------------------------------


async def test_effective_envelope_and_provenance(cfg: GatewayConfig) -> None:
    result = await rpc_config._handle_config_effective(None, _ctx(cfg))
    assert set(result) == {"fields"}
    fields = result["fields"]
    assert fields["llm.provider"] == {"value": "tokenrhythm", "source": "default"}
    assert fields["llm.model"] == {"value": "deepseek-v4-pro", "source": "default"}
    assert fields["squilla_router.tiers.c1.model"]["source"] == "preset"
    for record in fields.values():
        assert set(record) == {"value", "source"}


async def test_effective_reflects_config_overrides(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(model="synthetic/custom-model", max_tokens=2_048),
    )
    result = await rpc_config._handle_config_effective(None, _ctx(cfg))
    fields = result["fields"]
    assert fields["llm.model"] == {"value": "synthetic/custom-model", "source": "config"}
    assert fields["llm.max_tokens"] == {"value": 2_048, "source": "config"}


async def test_effective_reports_tokenrhythm_qwen_output_limit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    config = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm={"provider": "tokenrhythm", "model": "qwen3.7-max"},
    )

    result = await rpc_config._handle_config_effective(None, _ctx(config))

    assert result["fields"]["llm.max_tokens"] == {
        "value": 131_072,
        "source": "catalog",
    }


async def test_effective_requires_config() -> None:
    with pytest.raises(ValueError, match="No config available"):
        await rpc_config._handle_config_effective(None, _ctx(None))


# --- belt-and-braces redaction --------------------------------------------------


async def test_secret_named_path_segments_are_dropped_entirely(
    cfg: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A flat {path, value, source} record defeats key-name redaction (the
    # dict key on the wire is "value", not the config key), so the handler
    # must drop any field whose dotted path carries a secret-named segment —
    # even if a future resolver change leaks one past the allowlist.
    def poisoned(config: Any, catalog: Any) -> dict[str, ResolvedField]:
        return {
            "llm.api_key": ResolvedField("sk-test-000", "config"),
            "channels.feishu.encrypt_key": ResolvedField("sk-test-000", "config"),
            "llm.provider": ResolvedField("openrouter", "default"),
        }

    monkeypatch.setattr(resolution, "resolve_effective_llm", poisoned)
    result = await rpc_config._handle_config_effective(None, _ctx(cfg))
    assert set(result["fields"]) == {"llm.provider"}
    assert "sk-test-000" not in repr(result)


async def test_container_values_are_redacted_before_wrapping(
    cfg: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Raw values run through redact_public_config BEFORE provenance
    # wrapping, so secret-named members inside a container value are masked
    # even when the field path itself is innocuous.
    def container_valued(config: Any, catalog: Any) -> dict[str, ResolvedField]:
        return {
            "llm.provider_routing": ResolvedField(
                {"api_key": "sk-test-000", "synthetic/model": "openrouter"}, "config"
            ),
        }

    monkeypatch.setattr(resolution, "resolve_effective_llm", container_valued)
    result = await rpc_config._handle_config_effective(None, _ctx(cfg))
    value = result["fields"]["llm.provider_routing"]["value"]
    assert value["api_key"] == "[redacted]"
    assert value["synthetic/model"] == "openrouter"
    assert "sk-test-000" not in repr(result)
