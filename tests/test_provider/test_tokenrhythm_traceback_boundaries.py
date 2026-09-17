"""Install identity never survives in outward provider traceback frames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from opensquilla.onboarding import image_generation_model_discovery as image_discovery
from opensquilla.provider import (
    image_generation,
    live_catalog,
    openai,
    tokenrhythm_catalog,
)
from opensquilla.provider.image_generation import (
    ImageGenerationRequest,
    OpenAIImageGenerationProvider,
    OpenRouterImageGenerationProvider,
    TokenRhythmImageGenerationProvider,
)
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, Message

_INSTALL_ID = "synthetic-install-id-frame-boundary"
_HEADER = "X-OpenSquilla-Install-Id"


def _retained_value_text(
    value: object,
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> str:
    """Recursively render only state reachable from one production frame."""

    if seen is None:
        seen = set()
    if depth > 7:
        return ""
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return repr(value)
    identity = id(value)
    if identity in seen:
        return ""
    seen.add(identity)

    children: list[object] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            children.extend((key, item))
    elif isinstance(value, (list, tuple, set, frozenset)):
        children.extend(value)
    elif isinstance(value, httpx.Headers):
        children.extend(value.multi_items())
    elif isinstance(value, httpx.Request):
        children.extend((str(value.url), value.headers))
        try:
            children.append(value.content)
        except (httpx.RequestNotRead, httpx.StreamConsumed):
            pass
    elif isinstance(value, httpx.Response):
        children.extend((value.headers, value.request))
        try:
            children.append(value.content)
        except (httpx.ResponseNotRead, httpx.StreamConsumed):
            pass
    elif isinstance(value, BaseException):
        children.extend((value.args, getattr(value, "__dict__", {})))
        for name in ("request", "response", "__cause__", "__context__"):
            try:
                children.append(getattr(value, name))
            except (AttributeError, RuntimeError):
                pass
    else:
        state = getattr(value, "__dict__", None)
        if isinstance(state, dict):
            children.append(state)

    return " ".join(
        (
            repr(value),
            *(
                _retained_value_text(item, seen=seen, depth=depth + 1)
                for item in children
            ),
        )
    )


def _production_traceback_state(
    exc: BaseException,
    *,
    file_fragment: str,
    function_name: str,
) -> str:
    matched: list[str] = []
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        filename = frame.f_code.co_filename.replace("\\", "/")
        if file_fragment in filename and frame.f_code.co_name == function_name:
            matched.append(_retained_value_text(dict(frame.f_locals)))
        traceback = traceback.tb_next
    assert matched, f"missing traceback frame {file_fragment}:{function_name}"
    return " ".join(matched)


def _patch_install_header(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    module = {
        "image": image_generation,
        "openai-image": image_generation,
        "openrouter-image": image_generation,
        "live": live_catalog,
        "discovery": image_discovery,
        "models": openai,
        "catalog-published": tokenrhythm_catalog,
        "catalog-declared": tokenrhythm_catalog,
    }[case]
    monkeypatch.setattr(
        module,
        "tokenrhythm_install_id_headers",
        lambda *_args, **_kwargs: {_HEADER: _INSTALL_ID},
    )
    monkeypatch.setattr(
        "opensquilla.provider.error_redaction.redact_tokenrhythm_install_ids",
        lambda text: text.replace(_INSTALL_ID, "***"),
    )
    monkeypatch.setattr(
        module,
        "redact_tokenrhythm_install_ids",
        lambda text: text.replace(_INSTALL_ID, "***"),
        raising=False,
    )


async def _call_boundary(case: str) -> object:
    if case == "image":
        provider = TokenRhythmImageGenerationProvider(api_key="synthetic-provider-key")
        return await provider.generate(
            ImageGenerationRequest(
                prompt="draw a squid",
                model="qwen-image-2.0",
                size="1024x1024",
            )
        )
    if case == "openrouter-image":
        provider = OpenRouterImageGenerationProvider(
            api_key="synthetic-provider-key",
            base_url="https://api.tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
        return await provider.generate(
            ImageGenerationRequest(
                prompt="draw a squid",
                model="image-model",
                size="1024x1024",
            )
        )
    if case == "openai-image":
        provider = OpenAIImageGenerationProvider(
            api_key="synthetic-provider-key",
            base_url="https://api.tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
        return await provider.generate(
            ImageGenerationRequest(
                prompt="draw a squid",
                model="image-model",
                size="1024x1024",
            )
        )
    if case == "live":
        return await live_catalog.fetch_live_catalog_entries(
            "https://tokenrhythm.studio/api/models",
            "tokenrhythm",
        )
    if case == "discovery":
        return await image_discovery._fetch_tokenrhythm_image_models()
    if case == "catalog-published":
        return await tokenrhythm_catalog.fetch_tokenrhythm_published()
    if case == "catalog-declared":
        return await tokenrhythm_catalog.fetch_tokenrhythm_declared(
            api_key="synthetic-provider-key"
        )
    provider = OpenAIProvider(
        api_key="synthetic-provider-key",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    return await provider.list_models(raise_on_error=True)


_BOUNDARIES = (
    pytest.param("image", "provider/image_generation.py", "generate", id="image"),
    pytest.param(
        "openai-image",
        "provider/image_generation.py",
        "generate",
        id="openai-compatible-image",
    ),
    pytest.param(
        "openrouter-image",
        "provider/image_generation.py",
        "generate",
        id="openrouter-compatible-image",
    ),
    pytest.param("live", "provider/live_catalog.py", "fetch_live_catalog_entries", id="live"),
    pytest.param(
        "discovery",
        "onboarding/image_generation_model_discovery.py",
        "_fetch_tokenrhythm_image_models",
        id="image-model-discovery",
    ),
    pytest.param(
        "catalog-published",
        "provider/tokenrhythm_catalog.py",
        "fetch_tokenrhythm_published",
        id="typed-catalog-published",
    ),
    pytest.param(
        "catalog-declared",
        "provider/tokenrhythm_catalog.py",
        "fetch_tokenrhythm_declared",
        id="typed-catalog-declared",
    ),
    pytest.param("models", "provider/openai.py", "list_models", id="models"),
)


@pytest.mark.parametrize(("case", "file_fragment", "function_name"), _BOUNDARIES)
async def test_http_error_scrubs_production_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    file_fragment: str,
    function_name: str,
) -> None:
    client_options: list[dict[str, Any]] = []

    class StatusClient:
        def __init__(self) -> None:
            self.retained_response: httpx.Response | None = None

        async def __aenter__(self) -> StatusClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            return self._response("GET", url, headers)

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> httpx.Response:
            return self._response("POST", url, headers, json=json)

        def _response(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            *,
            json: object | None = None,
        ) -> httpx.Response:
            request = httpx.Request(method, url, headers=headers, json=json)
            self.retained_response = httpx.Response(
                502,
                request=request,
                headers={"X-Upstream-Echo": _INSTALL_ID},
                text=f"upstream echoed {_INSTALL_ID}",
            )
            return self.retained_response

    def client_factory(**kwargs: Any) -> StatusClient:
        client_options.append(kwargs)
        return StatusClient()

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    _patch_install_header(monkeypatch, case)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _call_boundary(case)

    state = _production_traceback_state(
        raised.value,
        file_fragment=file_fragment,
        function_name=function_name,
    )
    assert _INSTALL_ID not in state
    if case == "catalog-declared":
        assert "synthetic-provider-key" not in state
    assert raised.value.__context__ is None
    assert client_options[-1]["follow_redirects"] is False


@pytest.mark.parametrize(("case", "file_fragment", "function_name"), _BOUNDARIES)
async def test_cancellation_scrubs_production_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    file_fragment: str,
    function_name: str,
) -> None:
    client_options: list[dict[str, Any]] = []

    class CancellingClient:
        def __init__(self) -> None:
            self.retained_headers: dict[str, str] = {}

        async def __aenter__(self) -> CancellingClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def get(self, _url: str, *, headers: dict[str, str]) -> httpx.Response:
            self.retained_headers = headers
            raise asyncio.CancelledError()

        async def post(
            self,
            _url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> httpx.Response:
            self.retained_headers = headers
            raise asyncio.CancelledError()

    def client_factory(**kwargs: Any) -> CancellingClient:
        client_options.append(kwargs)
        return CancellingClient()

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    _patch_install_header(monkeypatch, case)

    with pytest.raises(asyncio.CancelledError) as raised:
        await _call_boundary(case)

    state = _production_traceback_state(
        raised.value,
        file_fragment=file_fragment,
        function_name=function_name,
    )
    assert _INSTALL_ID not in state
    if case == "catalog-declared":
        assert "synthetic-provider-key" not in state
    assert raised.value.__context__ is None
    assert client_options[-1]["follow_redirects"] is False


@pytest.mark.parametrize(("case", "file_fragment", "function_name"), _BOUNDARIES)
async def test_invalid_json_scrubs_response_and_payload_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    file_fragment: str,
    function_name: str,
) -> None:
    class InvalidJsonClient:
        async def __aenter__(self) -> InvalidJsonClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            return self._response("GET", url, headers)

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> httpx.Response:
            return self._response("POST", url, headers, json=json)

        @staticmethod
        def _response(
            method: str,
            url: str,
            headers: dict[str, str],
            *,
            json: object | None = None,
        ) -> httpx.Response:
            request = httpx.Request(method, url, headers=headers, json=json)
            return httpx.Response(
                200,
                request=request,
                text=f'{{"echo":"{_INSTALL_ID}"',
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: InvalidJsonClient())
    _patch_install_header(monkeypatch, case)

    with pytest.raises((RuntimeError, ValueError, json.JSONDecodeError)) as raised:
        await _call_boundary(case)

    state = _production_traceback_state(
        raised.value,
        file_fragment=file_fragment,
        function_name=function_name,
    )
    assert _INSTALL_ID not in state
    if case == "catalog-declared":
        assert "synthetic-provider-key" not in state
    assert raised.value.__context__ is None


@pytest.mark.parametrize("failure_kind", ("validation", "download", "download-cancel"))
async def test_image_post_request_failures_drop_raw_response_state(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    image_url = f"https://cdn.example.test/{_INSTALL_ID}.png"

    class PayloadClient:
        async def __aenter__(self) -> PayloadClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> httpx.Response:
            request = httpx.Request("POST", url, headers=headers, json=json)
            data = (
                {"data": [], "echo": _INSTALL_ID}
                if failure_kind == "validation"
                else {"data": [{"url": image_url}], "echo": _INSTALL_ID}
            )
            return httpx.Response(200, request=request, json=data)

    async def failing_download(
        _url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, bytes]:
        assert timeout_seconds > 0
        if failure_kind == "download-cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic download failure")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: PayloadClient())
    monkeypatch.setattr(
        image_generation,
        "_download_tokenrhythm_image",
        failing_download,
    )
    _patch_install_header(monkeypatch, "image")

    expected_error = (
        asyncio.CancelledError
        if failure_kind == "download-cancel"
        else RuntimeError
    )
    with pytest.raises(expected_error) as raised:
        await _call_boundary("image")

    state = _production_traceback_state(
        raised.value,
        file_fragment="provider/image_generation.py",
        function_name="generate",
    )
    assert _INSTALL_ID not in state
    assert raised.value.__context__ is None


async def test_chat_cancellation_drops_physical_stream_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_headers: dict[str, str] = {}

    class CancellingStream:
        async def __aenter__(self) -> object:
            raise asyncio.CancelledError()

        async def __aexit__(self, *_args: object) -> bool:
            return False

    class CancellingClient:
        async def __aenter__(self) -> CancellingClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        def stream(self, *_args: object, **kwargs: Any) -> CancellingStream:
            sent_headers.update(kwargs["headers"])
            return CancellingStream()

    monkeypatch.setattr(
        openai.httpx,
        "AsyncClient",
        lambda **_kwargs: CancellingClient(),
    )
    _patch_install_header(monkeypatch, "models")
    provider = OpenAIProvider(
        api_key="synthetic-provider-key",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    async def collect() -> None:
        async for _event in provider.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(),
        ):
            pass

    with pytest.raises(asyncio.CancelledError) as raised:
        await collect()

    assert sent_headers[_HEADER] == _INSTALL_ID
    state = _production_traceback_state(
        raised.value,
        file_fragment="provider/openai.py",
        function_name="_stream_with_detached_cancellation",
    )
    assert _INSTALL_ID not in state
    assert raised.value.__context__ is None
