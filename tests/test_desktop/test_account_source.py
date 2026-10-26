import httpx
import pytest

from opensquilla import desktop_account as bridge
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_credential, resolve_llm_runtime_config
from opensquilla.provider.deployment import resolve_provider_deployment


@pytest.fixture(autouse=True)
def private_state(monkeypatch):
    monkeypatch.setattr(bridge, "_endpoint", "")
    monkeypatch.setattr(bridge, "_token", "")
    token = bridge.pin_account_state(None)
    yield
    bridge.reset_account_state(token)


def signed_in(**updates):
    return {
        "source": "account",
        "selectedAccountId": "a",
        "accountId": "a",
        "ready": True,
        "credential": "os_local_" + "x" * 43,
        "mediaSources": {"image": "inherit", "video": "inherit"},
        **updates,
    }


def test_managed_resolution_preserves_manual_secret_and_endpoint(monkeypatch):
    cfg = GatewayConfig()
    cfg.llm.api_key = "manual-secret"
    cfg.llm.base_url = "https://custom.invalid/v1"
    bridge.pin_account_state(signed_in())
    runtime = resolve_llm_runtime_config(cfg)
    assert runtime.api_key.startswith("os_local_")
    assert runtime.base_url == bridge.OFFICIAL_BASE
    assert cfg.llm.api_key == "manual-secret"
    assert cfg.llm.base_url == "https://custom.invalid/v1"
    with bridge.manual_credential_scope():
        assert resolve_llm_credential(cfg).api_key == "manual-secret"


def test_unavailable_account_never_uses_manual_or_environment(monkeypatch):
    cfg = GatewayConfig()
    cfg.llm.api_key = "manual-secret"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "environment-secret")
    bridge.pin_account_state(signed_in(ready=False))
    assert resolve_llm_credential(cfg).api_key == ""
    assert not resolve_provider_deployment(cfg, "tokenrhythm", "model").ready


def test_new_account_does_not_take_over_old_binding():
    bridge.pin_account_state(signed_in(accountId="b"))
    assert not bridge.account_credential("tokenrhythm").ready


def test_media_preserves_independent_keys_and_binds_explicit_account():
    state = signed_in()
    bridge.pin_account_state(state)
    assert bridge.account_credential("tokenrhythm", kind="image", explicit=True) is None
    assert bridge.account_credential("tokenrhythm", kind="video").ready
    state = signed_in(
        source="manual",
        mediaSources={"image": "account", "video": "manual"},
        mediaAccountIds={"image": "a"},
    )
    bridge.pin_account_state(state)
    assert bridge.account_credential("tokenrhythm") is None
    assert bridge.account_credential("tokenrhythm", kind="image", explicit=True).ready
    assert bridge.account_credential("tokenrhythm", kind="video") is None


def test_turn_snapshot_retains_source_after_live_change(monkeypatch):
    state = signed_in()
    bridge.pin_account_state(state)
    monkeypatch.setattr(bridge, "_cached", {"source": "manual"})
    assert bridge.account_credential("tokenrhythm").ready
    assert bridge.account_state_for_turn() == state


def test_bootstrap_removes_control_credentials_even_on_invalid_input(monkeypatch):
    monkeypatch.setattr(bridge, "_initialized", False)
    monkeypatch.setenv(bridge._URL_ENV, "https://evil.invalid")
    monkeypatch.setenv(bridge._TOKEN_ENV, "sensitive")
    with pytest.raises(ValueError):
        bridge.initialize_desktop_account()
    import os

    assert bridge._TOKEN_ENV not in os.environ and bridge._URL_ENV not in os.environ


@pytest.mark.asyncio
async def test_proxy_transport_preserves_identity_and_rejects_arbitrary_targets(monkeypatch):
    lease = signed_in()["credential"]
    monkeypatch.setitem(bridge._leases, lease, "http://127.0.0.1:12345")
    transport = bridge.AccountProxyTransport(lease)
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    await transport._transport.aclose()
    transport._transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(
            bridge.OFFICIAL_BASE + "/chat/completions", json={"stream": True}
        )
        assert response.request.url.host == "tokenrhythm.studio"
        assert seen[0].url.host == "127.0.0.1"
        assert seen[0].headers["host"] == "127.0.0.1:12345"
        assert seen[0].headers["authorization"] == "Bearer " + lease
        for url in [
            "https://evil.invalid/v1/models",
            "https://tokenrhythm.studio/health",
            bridge.OFFICIAL_BASE + "/models?target=evil",
            "http://tokenrhythm.studio/v1/models",
        ]:
            with pytest.raises(httpx.TransportError):
                await client.get(url)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_account_provider_streams_through_real_loopback_proxy(monkeypatch):
    import asyncio

    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.provider.types import ChatConfig, ErrorEvent, Message, TextDeltaEvent

    requests = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("ascii").split("\r\n")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines[1:] if ": " in line)
            }
            body = await reader.readexactly(int(headers["content-length"]))
            requests.append((lines[0], headers, body))
            response = (
                b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                b'data: [DONE]\n\n'
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(response)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n" + response
            )
            await writer.drain()
        except asyncio.IncompleteReadError:
            # Before the Host fix, HTTPX connected and rejected its own request.
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        lease = signed_in()["credential"]
        monkeypatch.setitem(bridge._leases, lease, f"http://127.0.0.1:{port}")
        provider = OpenAIProvider(
            api_key=lease, model="deepseek-flash", base_url=bridge.OFFICIAL_BASE,
            provider_kind="tokenrhythm",
        )

        async def collect():
            return [event async for event in provider.chat(
                [Message(role="user", content="hello")], None, ChatConfig(max_tokens=16),
            )]

        events = await asyncio.wait_for(collect(), timeout=5)

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == "OK"
    assert events[-1].kind == "done"
    assert len(requests) == 1
    request_line, headers, body = requests[0]
    assert request_line == "POST /v1/chat/completions HTTP/1.1"
    assert headers["host"] == f"127.0.0.1:{port}"
    assert headers["authorization"] == "Bearer " + lease
    assert b'"hello"' in body


def test_account_provider_factory_cannot_fall_back_to_saved_key():
    from opensquilla.provider.selector import (
        ProviderConfig,
        ProviderNotConfiguredError,
        build_provider_from_config,
    )

    bridge.pin_account_state(signed_in(ready=False))
    with pytest.raises(ProviderNotConfiguredError):
        build_provider_from_config(ProviderConfig("tokenrhythm", "model", api_key="manual-secret"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"code": "request_error", "message": "network interrupted"},
    {"code": "502", "http_status": 502,
     "provider_code": "ACCOUNT_PROXY_UPSTREAM_UNAVAILABLE"},
    {"code": "504", "http_status": 504,
     "provider_code": "ACCOUNT_PROXY_UPSTREAM_TIMEOUT"},
    {"code": "upstream_error", "http_status": 503,
     "provider_code": "provider_overloaded"},
])
async def test_account_transport_failure_is_not_replayed(monkeypatch, failure):
    from opensquilla.engine.runtime import _should_use_selector_fallback
    from opensquilla.provider.failures import ProviderFailureKind
    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.provider.recovery import retry_provider_stream, transient_failure_kind
    from opensquilla.provider.types import ChatConfig, ErrorEvent

    provider = OpenAIProvider(api_key=signed_in()["credential"], model="test")
    attempts = []

    async def failed(*args):
        attempts.append(1)
        yield ErrorEvent(**{"message": "upstream unavailable", **failure})

    monkeypatch.setattr(provider, "_stream_impl", failed)
    config = ChatConfig()
    events = [event async for event in retry_provider_stream(
        lambda: provider._stream([], None, config),
        provider=provider, model="test", config=config,
        max_retries=1, base_ms=0, max_ms=0,
    )]
    assert len(attempts) == 1
    assert len(events) == 1
    assert events[0].code == "account_request_uncertain"
    assert events[0].http_status == failure.get("http_status")
    assert transient_failure_kind(provider, events[0]) == ProviderFailureKind.BAD_REQUEST
    assert not _should_use_selector_fallback("tokenrhythm", events[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("status", [502, 504])
async def test_proxy_http_failures_stop_account_replay_but_preserve_manual_retries(
    monkeypatch, managed, status,
):
    from opensquilla.provider.openai import OpenAIProvider
    from opensquilla.provider.recovery import retry_provider_stream
    from opensquilla.provider.types import ChatConfig, ErrorEvent, Message

    provider = OpenAIProvider(
        api_key=signed_in()["credential"] if managed else "manual-key",
        model="qwen3.7-max", base_url=bridge.OFFICIAL_BASE, provider_kind="tokenrhythm",
    )
    requests = []

    async def upstream(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {
            "code": "ACCOUNT_PROXY_UPSTREAM_UNAVAILABLE",
            "message": "upstream unavailable", "type": "account_error",
        }})

    async def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(provider, "_build_http_client", client)
    config = ChatConfig()
    messages = [Message(role="user", content="hello")]
    events = [event async for event in retry_provider_stream(
        lambda: provider.chat(messages, None, config),
        provider=provider, model="qwen3.7-max", config=config,
        max_retries=1, base_ms=0, max_ms=0,
    )]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(requests) == (1 if managed else 2)
    assert len(errors) == 1
    assert errors[0].http_status == status
    assert errors[0].provider_code == (
        "account_request_uncertain" if managed else "account_proxy_upstream_unavailable"
    )


def test_first_login_completes_only_a_truly_empty_primary():
    cfg = GatewayConfig()
    for field in ("provider", "model", "api_key", "api_key_env", "base_url"):
        setattr(cfg.llm, field, "")
    bridge.pin_account_state(signed_in())
    runtime = resolve_llm_runtime_config(cfg)
    assert runtime.provider == "tokenrhythm" and runtime.model
    assert runtime.api_key.startswith("os_local_")
    assert cfg.llm.credential_source == "account"
    assert cfg.llm.api_key == "" and cfg.llm.api_key_env == ""
    assert "os_local_" not in cfg.model_dump_json()

    other = GatewayConfig()
    other.llm.provider = "openai"
    other.llm.api_key = ""
    other.llm.api_key_env = "MISSING_EXISTING_KEY"
    resolve_llm_runtime_config(other)
    assert other.llm.provider == "openai"
    assert other.llm.api_key_env == "MISSING_EXISTING_KEY"


def test_deleting_manual_connection_keeps_account_routes_and_blocks_env_fallback(monkeypatch):
    from opensquilla.onboarding.mutations import remove_llm_profile

    cfg = GatewayConfig()
    cfg.llm.api_key = "manual-secret"
    cfg.llm.api_key_env = "TOKENRHYTHM_API_KEY"
    original_model = cfg.llm.model
    bridge.pin_account_state(signed_in())
    result = remove_llm_profile(cfg, provider_id="tokenrhythm").config
    assert result.llm.provider == "tokenrhythm" and result.llm.model == original_model
    assert result.squilla_router == cfg.squilla_router
    assert result.llm.credential_source == "account"
    assert result.llm.api_key == "" and result.llm.api_key_env == ""
    assert cfg.llm.api_key == "manual-secret"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "hidden-environment-key")
    with bridge.manual_credential_scope():
        assert not resolve_llm_credential(result, registry_env_key="TOKENRHYTHM_API_KEY").api_key


def test_credential_reveal_never_returns_account_or_loopback_credentials():
    from opensquilla.gateway.adapters.setup_mutations import GatewayCredentialResolutionPort
    from opensquilla.gateway.rpc import RpcHandlerError

    cfg = GatewayConfig()
    cfg.llm.api_key = "manual-secret"
    bridge.pin_account_state(signed_in())
    result = GatewayCredentialResolutionPort(cfg, is_owner=True).reveal_active("tokenrhythm")
    assert result["apiKey"] == "manual-secret"
    cfg.llm.api_key = ""
    cfg.llm.credential_source = "account"
    with pytest.raises(RpcHandlerError):
        GatewayCredentialResolutionPort(cfg, is_owner=True).reveal_active("tokenrhythm")


def test_video_does_not_inherit_an_independent_image_account_override():
    from opensquilla.provider.video_generation_credentials import (
        resolve_video_generation_credential,
    )

    cfg = GatewayConfig()
    cfg.llm.api_key = "manual-model-key"
    cfg.video_generation.provider = "tokenrhythm"
    cfg.video_generation.primary = "wan3.0-video"
    bridge.pin_account_state(signed_in(
        source="manual",
        mediaSources={"image": "account", "video": "inherit"},
        mediaAccountIds={"image": "a"},
    ))
    result = resolve_video_generation_credential(cfg.video_generation, gateway_config=cfg)
    assert result.available and result.api_key == "manual-model-key"
    bridge.pin_account_state(signed_in())
    result = resolve_video_generation_credential(cfg.video_generation, gateway_config=cfg)
    assert result.available and result.api_key.startswith("os_local_")


def test_manual_key_can_replace_an_account_only_primary():
    from opensquilla.onboarding.mutations import upsert_llm_provider

    cfg = GatewayConfig()
    cfg.llm.credential_source = "account"
    cfg.llm.api_key = ""
    with bridge.manual_credential_scope():
        result = upsert_llm_provider(
            cfg, provider_id="tokenrhythm", api_key="new-manual-key"
        ).config
        assert result.llm.credential_source == "manual"
        assert resolve_llm_credential(result).api_key == "new-manual-key"


def test_editing_account_only_profile_keeps_its_fail_closed_reference(monkeypatch):
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.onboarding.mutations import upsert_llm_profile

    cfg = GatewayConfig()
    cfg.llm.provider = "openai"
    cfg.llm_profiles["tokenrhythm"] = LlmProviderProfile(credential_source="account")
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "hidden-env")
    with bridge.manual_credential_scope():
        result = upsert_llm_profile(cfg, provider_id="tokenrhythm", model="new-model").config
        assert result.llm_profiles["tokenrhythm"].credential_source == "account"
        assert not resolve_provider_deployment(result, "tokenrhythm", "new-model").ready
