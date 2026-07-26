from __future__ import annotations

from typing import Any

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE


async def _ready_memory(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}


async def _ready_channels(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {
        "channels": [
            {
                "name": "slack-main",
                "type": "slack",
                "enabled": True,
                "status": "connected",
            }
        ]
    }


async def _ready_search(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    return {
        "provider": "duckduckgo",
        "activeProvider": "duckduckgo",
        "configured": True,
        "runtimeSupported": True,
        "requiresApiKey": False,
        "apiKeyConfigured": False,
        "buildable": True,
    }


def _ready_logs(ctx: RpcContext) -> dict[str, Any]:
    return {
        "gateway_file_log": {
            "enabled": True,
            "path": "/tmp/opensquilla-debug.log",
            "exists": True,
            "active_tail_path_exists": True,
        },
        "raw_turn_call_log": {"enabled": False},
        "diagnostics_enabled": {"effective": False},
    }


def _disabled_file_logs(ctx: RpcContext) -> dict[str, Any]:
    return {
        "gateway_file_log": {
            "enabled": False,
            "path": "/tmp/opensquilla-debug.log",
            "exists": False,
        }
    }


def _optional_image_generation(ctx: RpcContext) -> dict[str, Any]:
    return {
        "enabled": False,
        "configured": False,
        "status": "optional",
        "provider": "",
        "primary": "openai/gpt-image-1",
        "source": "none",
    }


def _ready_router(ctx: RpcContext, *, deep: bool = False) -> dict[str, Any]:
    return {
        "enabled": True,
        "rolloutPhase": "full",
        "strategy": "v4_phase3",
        "tierProfile": "openrouter",
        "defaultTier": "c1",
        "runtimeValid": True,
        "requireRouterRuntime": True,
        "runtimeErrorKind": None,
    }


def _inactive_llm_ensemble(ctx: RpcContext) -> dict[str, Any]:
    return {"enabled": False, "selectionMode": ""}


def _patch_ready_support_surfaces(monkeypatch: pytest.MonkeyPatch, rpc_doctor: Any) -> None:
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", _ready_search)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(rpc_doctor, "_router_payload", _ready_router)
    monkeypatch.setattr(rpc_doctor, "_llm_ensemble_payload", _inactive_llm_ensemble)
    monkeypatch.setattr(
        rpc_doctor,
        "_image_generation_payload",
        _optional_image_generation,
    )


@pytest.fixture(autouse=True)
def _reset_router_strategy_cache():
    # The squilla_router doctor surface reads the turn loop's strategy cache;
    # keep these tests independent of whatever other tests left in it.
    from opensquilla.engine.steps import squilla_router as squilla_router_step

    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    yield
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None


@pytest.mark.asyncio
async def test_doctor_status_is_read_scoped() -> None:
    assert METHOD_SCOPES["doctor.status"] == READ_SCOPE


@pytest.mark.asyncio
async def test_doctor_status_combines_runtime_findings(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": False,
                    "buildable": True,
                    "apiKeyConfigured": False,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-opensquilla.toml"
    ctx = RpcContext(conn_id="test", config=cfg)
    response = await get_dispatcher().dispatch("req-1", "doctor.status", {}, ctx)

    assert response.ok is True
    assert response.payload["configPath"] == "/tmp/custom-opensquilla.toml"
    assert response.payload["status"] == "action_required"
    assert response.payload["ready"] is False
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "gateway.rpc.ready" in ids
    assert "provider.active.not_configured" in ids
    provider_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in provider_finding["fixSteps"] if "command" in step]
    assert (
        "opensquilla providers configure openrouter --api-key YOUR_API_KEY "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands
    assert "opensquilla gateway restart --config /tmp/custom-opensquilla.toml" in commands
    assert response.payload["agentId"] == "main"


@pytest.mark.asyncio
async def test_doctor_status_scopes_config_set_recovery_commands(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _disabled_file_logs)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-opensquilla.toml"
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "logs.gateway_file_log.disabled"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert (
        "opensquilla config set log_file_enabled true "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands
    assert "opensquilla gateway restart --config /tmp/custom-opensquilla.toml" in commands


@pytest.mark.asyncio
async def test_doctor_status_includes_search_and_image_generation_findings(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def search_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "provider": "brave",
            "activeProvider": "brave",
            "configured": False,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "buildable": False,
            "fallbackPolicy": "off",
        }

    def image_generation_payload(ctx: RpcContext) -> dict[str, Any]:
        return {
            "enabled": True,
            "configured": False,
            "status": "missing",
            "provider": "openai",
            "primary": "openai/gpt-image-1",
            "source": "none",
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", search_status)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(rpc_doctor, "_router_payload", _ready_router)
    monkeypatch.setattr(rpc_doctor, "_image_generation_payload", image_generation_payload)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(
            conn_id="test",
            config=GatewayConfig(
                search_provider="brave",
                search_api_key_env="CUSTOM_SEARCH_KEY",
            ),
        ),
    )

    assert response.ok is True
    assert response.payload["status"] == "degraded"
    findings = response.payload["findings"]
    ids = [finding["id"] for finding in findings]
    assert "search.provider.not_configured" in ids
    assert "image_generation.credentials.missing" in ids
    search_finding = next(
        finding for finding in findings if finding["id"] == "search.provider.not_configured"
    )
    assert search_finding["evidence"]["apiKeyEnv"] == "CUSTOM_SEARCH_KEY"
    assert "CUSTOM_SEARCH_KEY" in search_finding["detail"]


@pytest.mark.asyncio
async def test_doctor_status_explains_missing_image_generation_env_key(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", _ready_search)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(
            conn_id="test",
            config=GatewayConfig(
                image_generation={
                    "enabled": True,
                    "primary": "openrouter/google/gemini-3.1-flash-image-preview",
                    "providers": {
                        "openrouter": {
                            "api_key": "",
                            "api_key_env": "CUSTOM_IMAGE_KEY",
                        }
                    },
                }
            ),
        ),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "image_generation.credentials.missing"
    )
    assert finding["evidence"]["apiKeyEnv"] == "CUSTOM_IMAGE_KEY"
    assert "CUSTOM_IMAGE_KEY" in finding["detail"]


@pytest.mark.asyncio
async def test_doctor_status_reports_unknown_search_provider_as_reconfigurable(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def search_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise ValueError("Unknown search provider 'serpapi'. Available: brave, duckduckgo")

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", search_status)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(
        rpc_doctor,
        "_image_generation_payload",
        _optional_image_generation,
    )

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-opensquilla.toml"

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    search_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "search"
    )
    assert search_finding["id"] == "search.provider.unknown"
    commands = [step["command"] for step in search_finding["fixSteps"]]
    assert "opensquilla search list --json" in commands
    assert (
        "opensquilla configure search --search-provider duckduckgo "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands


@pytest.mark.asyncio
async def test_doctor_status_includes_router_and_memory_embedding_findings(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    def router_payload(ctx: RpcContext, *, deep: bool = False) -> dict[str, Any]:
        return {
            "enabled": True,
            "rolloutPhase": "full",
            "strategy": "v4_phase3",
            "tierProfile": "openrouter",
            "runtimeValid": False,
            "requireRouterRuntime": False,
            "runtimeErrorKind": "router_assets_missing",
            "error": "missing V4 bundle files",
        }

    def memory_embedding_payload(ctx: RpcContext) -> dict[str, Any]:
        return {
            "status": "error",
            "requestedProvider": "openai",
            "effectiveProvider": "none",
            "model": "text-embedding-3-small",
            "retrievalMode": "hybrid",
            "error": "memory.embedding.remote.api_key is required",
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_router_payload", router_payload)
    monkeypatch.setattr(rpc_doctor, "_memory_embedding_payload", memory_embedding_payload)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["status"] == "degraded"
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "router.runtime.missing" in ids
    assert "memory_embedding.config.error" in ids


def test_router_payload_deep_mode_loads_runtime_and_classifies_native_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.boot as boot
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    calls: list[str] = []

    def light_validation(config: GatewayConfig) -> None:
        calls.append("light")

    def deep_validation(config: GatewayConfig) -> None:
        calls.append("deep")
        raise RuntimeError(
            "dlopen(.../lightgbm/lib/lib_lightgbm.dylib, 0x0006): "
            "Library not loaded: @rpath/libomp.dylib"
        )

    monkeypatch.setattr(boot, "validate_squilla_router_runtime", light_validation)
    monkeypatch.setattr(boot, "validate_squilla_router_runtime_deep", deep_validation)

    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    quick = rpc_doctor._router_payload(ctx, deep=False)
    deep = rpc_doctor._router_payload(ctx, deep=True)

    assert calls == ["light", "deep"]
    assert quick["runtimeValid"] is True
    assert quick["runtimeErrorKind"] is None
    assert deep["runtimeValid"] is False
    assert deep["runtimeErrorKind"] == "macos_libomp_missing"
    assert "libomp.dylib" in deep["error"]


@pytest.mark.asyncio
async def test_doctor_status_accepts_deep_memory_flag(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {"agentId": "main", "deep": True},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["agentId"] == "main"
    assert seen_memory_params == {"agentId": "main", "deep": True}


@pytest.mark.asyncio
async def test_doctor_status_defaults_to_deep_memory_diagnostics(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert seen_memory_params == {"agentId": "main", "deep": True}


@pytest.mark.asyncio
async def test_doctor_status_can_skip_deep_memory_diagnostics(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    seen_memory_params: dict[str, Any] = {}

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        seen_memory_params.update(params)
        return {"backend": "sqlite", "status": "ok", "pendingRepairCount": 0}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {"deep": False},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert seen_memory_params == {"agentId": "main", "deep": False}


@pytest.mark.asyncio
async def test_doctor_status_explains_recovery_when_collection_fails(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise RuntimeError("provider status crashed")

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is False
    assert response.payload["status"] == "action_required"
    provider_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "provider.diagnostic.unavailable"
    )
    assert provider_finding["severity"] == "error"
    commands = [step["command"] for step in provider_finding["fixSteps"]]
    assert commands == [
        "opensquilla providers status --json",
        "opensquilla diagnostics status",
        "opensquilla gateway restart",
    ]


@pytest.mark.asyncio
async def test_doctor_status_degrades_when_noncritical_collection_fails(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def memory_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        raise RuntimeError("memory diagnostics crashed")

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", memory_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "degraded"
    assert response.payload["impactCounts"]["degrades"] == 1
    memory_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "memory.diagnostic.unavailable"
    )
    assert memory_finding["severity"] == "warn"
    assert memory_finding["readinessImpact"] == "degrades"


@pytest.mark.asyncio
async def test_doctor_status_treats_dead_channel_as_surface_degradation(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def channels_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "channels": [
                {
                    "name": "feishu",
                    "type": "feishu",
                    "enabled": True,
                    "status": "dead",
                }
            ]
        }

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", channels_status)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-opensquilla.toml"

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "degraded"
    channel_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "channel.feishu.dead"
    )
    assert channel_finding["severity"] == "error"
    assert channel_finding["readinessImpact"] == "degrades"
    commands = [step["command"] for step in channel_finding["fixSteps"] if "command" in step]
    assert (
        "opensquilla channels restart feishu --yes "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands
    assert (
        "opensquilla channels status feishu --json "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands


@pytest.mark.asyncio
async def test_doctor_status_reports_dingtalk_auth_invalid_without_stopped_duplicate(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def channels_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "channels": [
                {
                    "name": "dingtalk",
                    "type": "dingtalk",
                    "enabled": True,
                    "status": "stopped",
                    "diagnostics": {
                        "last_error": {
                            "error_class": "auth_invalid",
                            "provider_code": "authFailed",
                            "message": "凭证无效：检查 DingTalk AppKey/AppSecret",
                            "retryable": False,
                        }
                    },
                }
            ]
        }

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", channels_status)

    cfg = GatewayConfig()
    cfg.config_path = "/tmp/custom-opensquilla.toml"

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=cfg),
    )

    assert response.ok is True
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "channel.dingtalk.auth_invalid" in ids
    assert "channel.dingtalk.stopped" not in ids
    channel_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "channel.dingtalk.auth_invalid"
    )
    assert channel_finding["severity"] == "error"
    assert channel_finding["readinessImpact"] == "degrades"
    assert "AppKey/AppSecret" in channel_finding["detail"]
    commands = [step["command"] for step in channel_finding["fixSteps"] if "command" in step]
    assert (
        "opensquilla channels status dingtalk --json "
        "--config /tmp/custom-opensquilla.toml"
    ) in commands
    assert "opensquilla gateway restart --config /tmp/custom-opensquilla.toml" in commands


@pytest.mark.asyncio
async def test_doctor_status_treats_no_channels_as_optional_setup(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    async def channels_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {"channels": []}

    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)
    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", channels_status)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["ready"] is True
    assert response.payload["status"] == "ready"
    channel_finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "channels.none_configured"
    )
    assert channel_finding["severity"] == "info"
    assert channel_finding["readinessImpact"] == "optional"
    assert channel_finding["fixSteps"][0]["command"] == (
        "opensquilla configure --section channels"
    )


def _patch_all_but_llm_ensemble(monkeypatch: pytest.MonkeyPatch, rpc_doctor: Any) -> None:
    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "groq",
            "providers": [
                {
                    "providerId": "groq",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    monkeypatch.setattr(rpc_doctor, "_handle_doctor_memory_status", _ready_memory)
    monkeypatch.setattr(rpc_doctor, "_handle_channels_status", _ready_channels)
    monkeypatch.setattr(rpc_doctor, "_handle_search_status", _ready_search)
    monkeypatch.setattr(rpc_doctor, "_build_logs_status", _ready_logs)
    monkeypatch.setattr(rpc_doctor, "_router_payload", _ready_router)
    monkeypatch.setattr(
        rpc_doctor,
        "_image_generation_payload",
        _optional_image_generation,
    )


@pytest.mark.asyncio
async def test_doctor_status_warns_when_static_b5_ensemble_has_no_credential(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": True, "selection_mode": "static_openrouter_b5"},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "llm_ensemble.static_openrouter_b5.credentials.missing"
    )
    assert finding["severity"] == "warn"
    assert finding["readinessImpact"] == "degrades"
    assert "OPENROUTER_API_KEY" in finding["detail"]
    assert finding["evidence"]["activeProvider"] == "groq"
    assert finding["evidence"]["credentialAvailable"] is False
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert "opensquilla config set llm_ensemble.enabled false" in commands
    assert "opensquilla gateway restart" in commands


@pytest.mark.asyncio
async def test_doctor_status_reports_static_b5_ensemble_ready_when_keyed(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": True, "selection_mode": "static_openrouter_b5"},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "llm_ensemble.static_openrouter_b5.credentials.missing" not in ids
    assert "llm_ensemble.static_openrouter_b5.ready" in ids
    assert response.payload["ready"] is True
    assert response.payload["status"] == "ready"


@pytest.mark.asyncio
async def test_doctor_status_warns_when_static_tokenrhythm_b5_has_no_credential(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    # An OpenRouter key must NOT satisfy the tokenrhythm profile.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": True, "selection_mode": "static_tokenrhythm_b5"},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "llm_ensemble.static_tokenrhythm_b5.credentials.missing"
    )
    assert finding["severity"] == "warn"
    assert "TOKENRHYTHM_API_KEY" in finding["detail"]
    assert "TokenRhythm" in finding["detail"]
    assert finding["evidence"]["credentialAvailable"] is False


@pytest.mark.asyncio
async def test_doctor_status_reports_static_tokenrhythm_b5_ready_when_keyed(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "sk-tr-synthetic")
    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": True, "selection_mode": "static_tokenrhythm_b5"},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "llm_ensemble.static_tokenrhythm_b5.credentials.missing" not in ids
    assert "llm_ensemble.static_tokenrhythm_b5.ready" in ids


@pytest.mark.asyncio
async def test_doctor_status_skips_ensemble_finding_when_ensemble_disabled(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": False},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    assert not [
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "llm_ensemble"
    ]


def _fake_runtime_status(status: dict[str, Any]):
    return lambda: dict(status)


def _patch_runtime_status(monkeypatch: pytest.MonkeyPatch, status: dict[str, Any]) -> None:
    from opensquilla.engine.steps import squilla_router as squilla_router_step

    monkeypatch.setattr(
        squilla_router_step, "router_runtime_status", _fake_runtime_status(status)
    )


@pytest.mark.asyncio
async def test_doctor_status_warns_persistently_when_required_router_runtime_failed(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)
    _patch_runtime_status(
        monkeypatch,
        {
            "initialized": True,
            "loaded": False,
            "code": "macos_libomp_missing",
            "strategy": "heuristic",
            "error": "Library not loaded: @rpath/libomp.dylib",
        },
    )

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": False},
    )
    assert config.squilla_router.require_router_runtime is True
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "squilla_router.runtime.unavailable"
    )
    assert finding["severity"] == "warn"
    assert finding["readinessImpact"] == "degrades"
    assert finding["evidence"]["strategy"] == "heuristic"
    assert finding["evidence"]["runtimeErrorKind"] == "macos_libomp_missing"
    assert "brew install libomp" in finding["detail"]
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert "brew install libomp" in commands
    assert "opensquilla gateway restart" in commands
    # OQ#11: the flag surfaces loudly but never blocks startup/readiness.
    assert response.payload["ready"] is True
    assert response.payload["status"] == "degraded"


@pytest.mark.asyncio
async def test_doctor_status_reports_router_runtime_ready_when_loaded(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)
    _patch_runtime_status(
        monkeypatch,
        {
            "initialized": True,
            "loaded": True,
            "code": None,
            "strategy": "v4_phase3",
            "error": None,
        },
    )

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": False},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    ids = [finding["id"] for finding in response.payload["findings"]]
    assert "squilla_router.runtime.unavailable" not in ids
    assert "squilla_router.runtime.ready" in ids
    assert response.payload["ready"] is True
    assert response.payload["status"] == "ready"


@pytest.mark.asyncio
async def test_doctor_status_softens_router_runtime_finding_when_flag_false(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)
    _patch_runtime_status(
        monkeypatch,
        {
            "initialized": True,
            "loaded": False,
            "code": "router_python_dependency_missing",
            "strategy": "heuristic",
            "error": "No module named 'onnxruntime'",
        },
    )

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={"enabled": False},
        squilla_router={"require_router_runtime": False},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    finding = next(
        finding
        for finding in response.payload["findings"]
        if finding["id"] == "squilla_router.runtime.unavailable"
    )
    # Operator opted out of requiring the runtime: visible but optional.
    assert finding["severity"] == "info"
    assert finding["readinessImpact"] == "optional"
    assert response.payload["ready"] is True
    assert response.payload["status"] == "ready"


@pytest.mark.asyncio
async def test_doctor_status_stays_silent_before_router_strategy_initializes(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)
    # No _patch_runtime_status: the autouse fixture guarantees an
    # uninitialized strategy cache, mirroring a gateway whose boot preload
    # has not landed yet.
    config = GatewayConfig(llm={"provider": "groq", "api_key": "sk-groq-synthetic"})
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    assert not [
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "squilla_router"
    ]


@pytest.mark.asyncio
async def test_doctor_status_skips_router_runtime_surface_when_router_disabled(
    monkeypatch,
) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    _patch_all_but_llm_ensemble(monkeypatch, rpc_doctor)
    _patch_runtime_status(
        monkeypatch,
        {
            "initialized": True,
            "loaded": False,
            "code": "router_runtime_unavailable",
            "strategy": "heuristic",
            "error": "synthetic failure",
        },
    )

    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        squilla_router={"enabled": False},
    )
    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=config),
    )

    assert response.ok is True
    assert not [
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "squilla_router"
    ]


@pytest.mark.asyncio
async def test_doctor_status_has_no_migration_discovery_surface(monkeypatch) -> None:
    import opensquilla.gateway.rpc_doctor as rpc_doctor

    async def provider_status(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
        return {
            "activeProvider": "openrouter",
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                }
            ],
        }

    monkeypatch.setattr(rpc_doctor, "_handle_providers_status", provider_status)
    _patch_ready_support_surfaces(monkeypatch, rpc_doctor)

    response = await get_dispatcher().dispatch(
        "req-1",
        "doctor.status",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert not [
        finding
        for finding in response.payload["findings"]
        if finding["surface"] == "migration"
        or finding["id"] == "migration.legacy_home_detected"
    ]
