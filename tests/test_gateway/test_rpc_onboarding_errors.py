"""Onboarding RPC handlers raise stable, localizable error codes (i18n P4)."""

from __future__ import annotations

import asyncio

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401  ensures handler registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_onboarding import _channel_error, _validation_error


def _admin_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def test_validation_error_maps_valueerror_to_code():
    with pytest.raises(RpcHandlerError) as ei:
        with _validation_error("onboarding.provider.invalid"):
            raise ValueError("model is required")
    assert ei.value.code == "onboarding.provider.invalid"
    # the original English text is preserved as the message (client fallback)
    assert ei.value.message == "model is required"


def test_validation_error_passes_through_on_success():
    with _validation_error("onboarding.provider.invalid"):
        value = 1 + 1
    assert value == 2


def test_channel_error_distinguishes_not_found_from_invalid():
    with pytest.raises(RpcHandlerError) as missing:
        with _channel_error():
            raise KeyError("telegram-main")
    assert missing.value.code == "onboarding.channel.not_found"

    with pytest.raises(RpcHandlerError) as bad:
        with _channel_error():
            raise ValueError("unknown channel type")
    assert bad.value.code == "onboarding.channel.invalid"


def test_image_generation_configure_invalid_primary_yields_stable_code(tmp_path, monkeypatch):
    # The image RPC was the one onboarding handler not wrapped in _validation_error;
    # a bad primary model must now surface as a stable code, not a generic dispatch error.
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = asyncio.run(
        get_dispatcher().dispatch(
            "img1",
            "onboarding.imageGeneration.configure",
            {"providerId": "openrouter", "primary": "bogus-no-slash"},
            _admin_ctx(),
        )
    )
    assert res.error is not None
    assert res.error.code == "onboarding.imageGeneration.invalid"
    assert res.error.message  # carries the original English detail


def test_provider_configure_invalid_provider_yields_stable_code(tmp_path, monkeypatch):
    # Driven synchronously via asyncio.run so the test does not depend on
    # pytest-asyncio being active (CI uses asyncio_mode=auto; this runs anywhere).
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = asyncio.run(
        get_dispatcher().dispatch(
            "r1",
            "onboarding.provider.configure",
            {"providerId": "definitely-not-a-real-provider", "model": "x/y"},
            _admin_ctx(),
        )
    )
    assert res.error is not None
    assert res.error.code == "onboarding.provider.invalid"
    # the granular code carries the original English detail, not a blank message
    assert res.error.message


def test_channel_error_carries_structured_field_details():
    from opensquilla.onboarding.mutations import ChannelValidationError

    with pytest.raises(RpcHandlerError) as ei:
        with _channel_error():
            raise ChannelValidationError(
                "invalid channel entry: name: Field required",
                [{"field": "name", "message": "Field required"}],
            )
    assert ei.value.code == "onboarding.channel.invalid"
    assert ei.value.details == {"fields": [{"field": "name", "message": "Field required"}]}


def test_channel_error_plain_valueerror_has_no_details():
    with pytest.raises(RpcHandlerError) as ei:
        with _channel_error():
            raise ValueError("unknown channel type")
    assert ei.value.details is None
