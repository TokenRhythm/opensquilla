"""``list_models`` must attribute rows to the configured deployment identity.

A provider row's ``provider`` field is the configured ``provider_id`` (the
registry spec id an operator selected), not the wire dialect
(``_provider_kind``) and not the adapter family (``provider_name``). Those
three axes diverge for 11 runtime-supported specs, so attributing a listing
to either of the latter two mislabels the row — and ``models.list``'s
``provider`` filter compares that field verbatim, so a mislabeled row is
unreachable by its own configured id.

Every case here drives the real adapter through a mock transport: offline,
deterministic, credential-free.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config


def _patch_models_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    payload: dict[str, Any],
) -> None:
    """Serve ``payload`` for any GET issued by ``module``'s httpx client."""

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    real_async_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", patched)


# (provider_id, base_url) for openai_compat specs whose provider_id differs
# from their wire dialect. Keys are synthetic; base urls are never dialed.
_OPENAI_COMPAT_MISLABEL_CASES = [
    ("vllm", "http://127.0.0.1:8000/v1"),
    ("custom", "http://127.0.0.1:8000/v1"),
    ("kimi_coding_openai", "https://api.moonshot.cn/v1"),
    ("mimo_openai", "https://api.example.invalid/v1"),
    ("minimax_openai", "https://api.example.invalid/v1"),
    ("minimax_coding_openai", "https://api.example.invalid/v1"),
    ("bailian_coding_cn", "https://api.example.invalid/v1"),
    ("tencent_token_plan", "https://api.example.invalid/v1"),
    ("tencent_tokenhub_intl", "https://api.example.invalid/v1"),
]


@pytest.mark.parametrize(("provider_id", "base_url"), _OPENAI_COMPAT_MISLABEL_CASES)
async def test_openai_compat_listing_reports_configured_provider_id(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    base_url: str,
) -> None:
    # Guard the premise: these specs only exercise the defect because their
    # provider_id and wire dialect genuinely diverge.
    spec = get_provider_spec(provider_id)
    assert spec.provider_id != spec.provider_kind

    _patch_models_endpoint(
        monkeypatch,
        "opensquilla.provider.openai",
        {"object": "list", "data": [{"id": "listed-model", "object": "model"}]},
    )
    provider = build_provider_from_config(
        ProviderConfig(
            provider=provider_id,
            model="listed-model",
            api_key="synthetic-list-models-key",
            base_url=base_url,
        )
    )

    rows = await provider.list_models()

    assert [row.provider for row in rows] == [provider_id]
    # The dialect must not leak into attribution even though it drives the wire.
    assert rows[0].provider != spec.provider_kind


@pytest.mark.parametrize(
    "provider_id",
    ["byteplus_coding_plan", "volcengine_coding_plan"],
)
async def test_openai_responses_listing_reports_configured_provider_id(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    # The responses adapter's provider_name is the canonical
    # "openai_responses" spec id, so plan surfaces sharing the backend used
    # to report that id instead of their own.
    _patch_models_endpoint(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        {"object": "list", "data": [{"id": "listed-model"}]},
    )
    provider = build_provider_from_config(
        ProviderConfig(
            provider=provider_id,
            model="listed-model",
            api_key="synthetic-list-models-key",
            base_url="https://api.example.invalid/v1",
        )
    )

    rows = await provider.list_models()

    assert [row.provider for row in rows] == [provider_id]


async def test_ollama_listing_reports_configured_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_models_endpoint(
        monkeypatch,
        "opensquilla.provider.ollama",
        {"models": [{"name": "llama3.2", "details": {"context_length": 8192}}]},
    )
    provider = build_provider_from_config(
        ProviderConfig(
            provider="ollama",
            model="llama3.2",
            base_url="http://127.0.0.1:11434",
        )
    )

    rows = await provider.list_models()

    assert [row.provider for row in rows] == ["ollama"]


def test_direct_construction_falls_back_to_the_dialect_not_openai() -> None:
    # OpenAIProvider serves every OpenAI-compatible dialect, so falling back
    # to provider_name ("openai") would attribute a foreign deployment to
    # OpenAI. Direct construction is a test/ad-hoc path; production always
    # passes provider_id through the selector.
    sniffed = OpenAIProvider(
        api_key="synthetic-list-models-key",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
    )
    assert sniffed.provider_id == "openrouter"

    explicit_kind = OpenAIProvider(
        api_key="synthetic-list-models-key",
        model="minimax-test-1",
        base_url="https://api.example.invalid/v1",
        provider_kind="minimax",
    )
    assert explicit_kind.provider_id == "minimax"

    # An explicit provider_id still wins over the dialect.
    configured = OpenAIProvider(
        api_key="synthetic-list-models-key",
        model="listed-model",
        base_url="https://api.example.invalid/v1",
        provider_kind="minimax",
        provider_id="minimax_coding_openai",
    )
    assert configured.provider_id == "minimax_coding_openai"
