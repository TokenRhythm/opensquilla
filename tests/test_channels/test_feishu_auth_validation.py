from __future__ import annotations

import base64
import hashlib
import json
import os

import httpx
import pytest
from Crypto.Cipher import AES
from starlette.requests import Request

from opensquilla.channels._util import EventDedupeCache
from opensquilla.channels.feishu import (
    FeishuApiError,
    FeishuAuthError,
    FeishuChannel,
    FeishuChannelConfig,
    FeishuWebhookTransport,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_tenant_access_token_uses_app_credentials_and_caches_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads((await request.aread()).decode())
        assert request.url.path == "/open-apis/auth/v3/tenant_access_token/internal"
        assert payload == {"app_id": "cli_test", "app_secret": "secret"}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            },
        )

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="cli_test", app_secret="secret", connection_mode="webhook")
    )
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        first = await channel._get_token()
        second = await channel._get_token()
    finally:
        await channel.stop()

    assert first == "tenant-token"
    assert second == "tenant-token"
    assert len(requests) == 1


@pytest.mark.anyio
async def test_tenant_access_token_error_raises_auth_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 999, "msg": "invalid app credentials"})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="cli_test", app_secret="bad-secret", connection_mode="webhook")
    )
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(FeishuAuthError, match="invalid app credentials") as caught:
            await channel._get_token()
    finally:
        await channel.stop()

    assert caught.value.diagnostic == {
        "error_class": "auth_invalid",
        "provider_code": "999",
        "message": "invalid app credentials",
        "retryable": False,
    }


@pytest.mark.anyio
async def test_bot_identity_probe_fails_closed_on_provider_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            )
        assert request.url.path.endswith("/bot/v3/info")
        return httpx.Response(200, json={"code": 999, "msg": "bot unavailable"})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="cli_test", app_secret="secret")
    )
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(FeishuApiError, match="bot unavailable"):
            await channel.probe_connection()
    finally:
        await channel.stop()


async def _feishu_webhook_response(
    transport: FeishuWebhookTransport,
    *,
    body: dict,
    signature: str | None,
    timestamp: str = "1710000000",
    nonce: str = "nonce",
):
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"x-lark-request-timestamp", timestamp.encode()),
        (b"x-lark-request-nonce", nonce.encode()),
    ]
    if signature is not None:
        headers.append((b"x-lark-signature", signature.encode()))
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": transport.config.webhook_path,
        "headers": headers,
        "query_string": b"",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request(scope, receive)
    return await transport._handle_webhook(request)


def _feishu_signature(*, encrypt_key: str, timestamp: str, nonce: str, body: dict) -> str:
    body_str = json.dumps(body)
    return hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}{body_str}".encode()).hexdigest()


def _encrypt_feishu_payload(encrypt_key: str, body: dict) -> str:
    plaintext = json.dumps(body).encode()
    pad = AES.block_size - (len(plaintext) % AES.block_size)
    padded = plaintext + bytes([pad]) * pad
    iv = os.urandom(AES.block_size)
    key = hashlib.sha256(encrypt_key.encode()).digest()
    encrypted = iv + AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    return base64.b64encode(encrypted).decode()


@pytest.mark.anyio
async def test_feishu_webhook_rejects_missing_or_wrong_signature() -> None:
    transport = FeishuWebhookTransport(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            encrypt_key="encrypt-key",
            connection_mode="webhook",
        ),
        EventDedupeCache(max_size=10),
    )
    body = {"type": "url_verification", "challenge": "challenge-token"}

    missing = await _feishu_webhook_response(transport, body=body, signature=None)
    wrong = await _feishu_webhook_response(transport, body=body, signature="bad-signature")

    assert missing.status_code == 401
    assert wrong.status_code == 401


@pytest.mark.anyio
async def test_feishu_webhook_accepts_valid_signature_for_url_verification() -> None:
    transport = FeishuWebhookTransport(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            encrypt_key="encrypt-key",
            connection_mode="webhook",
        ),
        EventDedupeCache(max_size=10),
    )
    body = {"type": "url_verification", "challenge": "challenge-token"}
    signature = _feishu_signature(
        encrypt_key="encrypt-key",
        timestamp="1710000000",
        nonce="nonce",
        body=body,
    )

    response = await _feishu_webhook_response(transport, body=body, signature=signature)

    assert response.status_code == 200
    assert json.loads(response.body) == {"challenge": "challenge-token"}


@pytest.mark.anyio
async def test_feishu_webhook_decrypts_official_encrypted_callback_shape() -> None:
    encrypt_key = "encrypt-key"
    verification_token = "verification-token"
    transport = FeishuWebhookTransport(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            encrypt_key=encrypt_key,
            verification_token=verification_token,
            connection_mode="webhook",
        ),
        EventDedupeCache(max_size=10),
    )
    decrypted = {
        "type": "url_verification",
        "token": verification_token,
        "challenge": "encrypted-challenge",
    }
    outer = {"encrypt": _encrypt_feishu_payload(encrypt_key, decrypted)}
    signature = _feishu_signature(
        encrypt_key=encrypt_key,
        timestamp="1710000000",
        nonce="nonce",
        body=outer,
    )

    response = await _feishu_webhook_response(
        transport,
        body=outer,
        signature=signature,
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"challenge": "encrypted-challenge"}


@pytest.mark.anyio
async def test_feishu_webhook_rejects_wrong_verification_token() -> None:
    transport = FeishuWebhookTransport(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            verification_token="expected-token",
            connection_mode="webhook",
        ),
        EventDedupeCache(max_size=10),
    )

    response = await _feishu_webhook_response(
        transport,
        body={
            "type": "url_verification",
            "token": "wrong-token",
            "challenge": "challenge-token",
        },
        signature=None,
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_feishu_webhook_without_verification_material_fails_closed() -> None:
    transport = FeishuWebhookTransport(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            connection_mode="webhook",
        ),
        EventDedupeCache(max_size=10),
    )

    response = await _feishu_webhook_response(
        transport,
        body={"type": "url_verification", "challenge": "challenge-token"},
        signature=None,
    )

    assert response.status_code == 503


@pytest.mark.anyio
async def test_feishu_webhook_start_requires_verification_material() -> None:
    # encrypt_key alone still authenticates (HMAC signature verification), so
    # start only fails closed when NEITHER credential is configured.
    channel = FeishuChannel(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            connection_mode="webhook",
        )
    )

    with pytest.raises(FeishuAuthError, match="verification_token or encrypt_key"):
        await channel.start()


def test_feishu_webhook_verification_material_is_kept_in_config() -> None:
    config = FeishuChannelConfig(
        app_id="cli_test",
        app_secret="secret",
        encrypt_key="encrypt-key",
        verification_token="verification-token",
        connection_mode="webhook",
    )

    assert config.encrypt_key == "encrypt-key"
    assert config.verification_token == "verification-token"


@pytest.mark.anyio
async def test_lark_domain_uses_larksuite_rest_api_by_default() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(
            app_id="cli_test",
            app_secret="secret",
            domain="lark",
        )
    )

    client = channel._get_client()
    try:
        assert str(client.base_url) == "https://open.larksuite.com/open-apis/"
    finally:
        await channel.stop()
