from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from opensquilla.gateway import desktop_artifact_bridge as bridge_module
from opensquilla.gateway.desktop_artifact_bridge import (
    DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV,
    DESKTOP_ARTIFACT_BRIDGE_URL_ENV,
    DesktopArtifactBridgeClient,
    DesktopArtifactBridgeError,
    desktop_artifact_bridge_client_from_environment,
)


def _token() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")


class _Response:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        declared_length: str | None = None,
    ) -> None:
        self.status = status
        self._body = json.dumps(payload, separators=(",", ":")).encode()
        self._content_type = content_type
        self._declared_length = declared_length

    def getheader(self, name: str) -> str | None:
        if name.lower() == "content-type":
            return self._content_type
        if name.lower() == "content-length":
            return self._declared_length or str(len(self._body))
        return None

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


class _Connection:
    def __init__(
        self, response: _Response, calls: list[dict[str, Any]], *args: object, **kwargs: object
    ) -> None:
        self._response = response
        self._calls = calls
        self._calls.append({"constructor_args": args, "constructor_kwargs": kwargs})

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self._calls.append({"method": method, "path": path, "body": body, "headers": headers})

    def getresponse(self) -> _Response:
        return self._response

    def close(self) -> None:
        self._calls.append({"closed": True})


def _install_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    *,
    status: int = 200,
    content_type: str = "application/json; charset=utf-8",
    declared_length: str | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    response = _Response(
        payload,
        status=status,
        content_type=content_type,
        declared_length=declared_length,
    )
    monkeypatch.setattr(
        bridge_module.http.client,
        "HTTPConnection",
        lambda *args, **kwargs: _Connection(response, calls, *args, **kwargs),
    )
    return calls


def test_environment_requires_desktop_and_exact_ipv4_loopback() -> None:
    token = _token()
    assert desktop_artifact_bridge_client_from_environment({}) is None
    assert (
        desktop_artifact_bridge_client_from_environment(
            {
                DESKTOP_ARTIFACT_BRIDGE_URL_ENV: "http://127.0.0.1:1234",
                DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV: token,
            }
        )
        is None
    )

    with pytest.raises(ValueError, match="incomplete"):
        desktop_artifact_bridge_client_from_environment(
            {"OPENSQUILLA_DESKTOP": "1", DESKTOP_ARTIFACT_BRIDGE_URL_ENV: "http://127.0.0.1:1234"}
        )
    for endpoint in (
        "https://127.0.0.1:1234",
        "http://localhost:1234",
        "http://[::1]:1234",
        "http://127.0.0.1:1234/path",
        "http://user@127.0.0.1:1234",
    ):
        with pytest.raises(ValueError, match="URL is invalid"):
            DesktopArtifactBridgeClient(endpoint=endpoint, token=token)

    client = desktop_artifact_bridge_client_from_environment(
        {
            "OPENSQUILLA_DESKTOP": "1",
            DESKTOP_ARTIFACT_BRIDGE_URL_ENV: "http://127.0.0.1:1234",
            DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV: token,
        }
    )
    assert client is not None
    assert token not in repr(client)
    assert "1234" not in repr(client)
    assert not hasattr(client, "invoke")


def test_runtime_initialization_scrubs_credentials_before_child_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _token()
    monkeypatch.setattr(bridge_module, "_runtime_client_initialized", False)
    monkeypatch.setattr(bridge_module, "_runtime_client", None)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv(DESKTOP_ARTIFACT_BRIDGE_URL_ENV, "http://127.0.0.1:1234")
    monkeypatch.setenv(DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV, token)

    client = bridge_module.initialize_desktop_artifact_bridge_client()

    assert client is not None
    assert DESKTOP_ARTIFACT_BRIDGE_URL_ENV not in bridge_module.os.environ
    assert DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV not in bridge_module.os.environ
    assert bridge_module.get_desktop_artifact_bridge_client() is client


@pytest.mark.asyncio
async def test_capabilities_uses_authenticated_fixed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "value": {
                "version": 3,
                "available": True,
                "captureSelection": False,
                "resolveAnnotationSelection": True,
                "focusAnnotation": True,
                "browserInspect": False,
                "browserAct": False,
                "screenshot": True,
                "officeFlush": False,
                "reloadSurface": True,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:1234", token=_token())

    capabilities = await client.capabilities()

    assert capabilities.available is True
    assert capabilities.capture_selection is False
    assert capabilities.resolve_annotation_selection is True
    assert capabilities.focus_annotation is True
    assert capabilities.browser_inspect is False
    assert capabilities.browser_act is False
    assert capabilities.screenshot is True
    assert capabilities.office_flush is False
    assert capabilities.reload_surface is True
    request = calls[1]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/capabilities"
    assert json.loads(request["body"]) == {"version": 3}
    assert request["headers"]["Authorization"] == f"Bearer {_token()}"
    assert int(request["headers"]["X-OpenSquilla-Deadline-At-Ms"]) > 0
    assert calls[-1] == {"closed": True}


@pytest.mark.asyncio
async def test_resolve_annotation_selection_is_typed_and_requires_exact_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_preview_artifact_id = "art-bridge-fixture"
    digest = "a" * 64
    element_proof = "c" * 64
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "button", 2]],
        separators=(",", ":"),
    )
    calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "resolveAnnotationSelection",
            "value": {
                "activePreviewArtifactId": active_preview_artifact_id,
                "selectionId": "selection_42",
                "tagName": "button",
                "elementPath": element_path,
                "domSha256": digest,
                "elementProofSha256": element_proof,
                "scopeId": "agent:fixture:webchat:fixture",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 24},
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    resolved = await client.resolve_annotation_selection(
        active_preview_artifact_id=active_preview_artifact_id,
        selection_id="selection_42",
        tag_name="button",
        element_path=element_path,
        dom_sha256=digest,
        element_proof_sha256=element_proof,
    )

    assert resolved.selection_id == "selection_42"
    assert resolved.active_preview_artifact_id == active_preview_artifact_id
    assert resolved.tag_name == "button"
    assert resolved.element_path == element_path
    assert resolved.dom_sha256 == digest
    assert resolved.element_proof_sha256 == element_proof
    assert resolved.scope_id == "agent:fixture:webchat:fixture"
    request = json.loads(calls[1]["body"])
    assert request == {
        "version": 3,
        "method": "resolveAnnotationSelection",
        "request": {
            "version": 3,
            "activePreviewArtifactId": active_preview_artifact_id,
            "selectionId": "selection_42",
            "tagName": "button",
            "elementPath": element_path,
            "domSha256": digest,
            "elementProofSha256": element_proof,
        },
    }
    serialized = json.dumps(request).lower()
    assert "surfaceid" not in serialized
    assert "url" not in serialized
    assert "javascript" not in serialized
    assert "cdp" not in serialized

    mismatched_calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "resolveAnnotationSelection",
            "value": {
                "activePreviewArtifactId": active_preview_artifact_id,
                "selectionId": "selection_substituted",
                "tagName": "button",
                "elementPath": element_path,
                "domSha256": digest,
                "elementProofSha256": element_proof,
                "scopeId": "agent:fixture:webchat:fixture",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 24},
            },
        },
    )
    with pytest.raises(DesktopArtifactBridgeError, match="selection is invalid"):
        await client.resolve_annotation_selection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id="selection_42",
            tag_name="button",
            element_path=element_path,
            dom_sha256=digest,
            element_proof_sha256=element_proof,
        )
    assert mismatched_calls

    mismatched_identity_calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "resolveAnnotationSelection",
            "value": {
                "activePreviewArtifactId": "art-different-preview",
                "selectionId": "selection_42",
                "tagName": "button",
                "elementPath": element_path,
                "domSha256": digest,
                "elementProofSha256": element_proof,
                "scopeId": "agent:fixture:webchat:fixture",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 24},
            },
        },
    )
    with pytest.raises(DesktopArtifactBridgeError, match="selection is invalid"):
        await client.resolve_annotation_selection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id="selection_42",
            tag_name="button",
            element_path=element_path,
            dom_sha256=digest,
            element_proof_sha256=element_proof,
        )
    assert mismatched_identity_calls

    mismatched_calls.clear()
    with pytest.raises(ValueError, match="DOM digest"):
        await client.resolve_annotation_selection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id="selection_42",
            tag_name="button",
            element_path=element_path,
            dom_sha256="not-a-digest",
            element_proof_sha256=element_proof,
        )
    assert mismatched_calls == []

    with pytest.raises(ValueError, match="element proof"):
        await client.resolve_annotation_selection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id="selection_42",
            tag_name="button",
            element_path=element_path,
            dom_sha256=digest,
            element_proof_sha256="not-a-proof",
        )
    assert mismatched_calls == []


@pytest.mark.asyncio
async def test_resolve_annotation_selection_omits_optional_dom_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_preview_artifact_id = "art-bridge-without-dom"
    element_proof = "d" * 64
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1]],
        separators=(",", ":"),
    )
    calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "resolveAnnotationSelection",
            "value": {
                "activePreviewArtifactId": active_preview_artifact_id,
                "selectionId": "selection_without_dom_digest",
                "tagName": "main",
                "elementPath": element_path,
                "elementProofSha256": element_proof,
                "scopeId": "agent:fixture:webchat:fixture",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 24},
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    resolved = await client.resolve_annotation_selection(
        active_preview_artifact_id=active_preview_artifact_id,
        selection_id="selection_without_dom_digest",
        tag_name="main",
        element_path=element_path,
        element_proof_sha256=element_proof,
    )

    assert resolved.dom_sha256 is None
    request = json.loads(calls[1]["body"])
    assert "domSha256" not in request["request"]
    assert request["request"]["activePreviewArtifactId"] == active_preview_artifact_id
    assert request["request"]["elementProofSha256"] == element_proof


@pytest.mark.asyncio
async def test_focus_annotation_accepts_only_server_scoped_canonical_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_preview_artifact_id = "art-focus-fixture"
    element_proof = "b" * 64
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "section", 1]],
        separators=(",", ":"),
    )
    calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "focusAnnotation",
            "value": {
                "focused": True,
                "activePreviewArtifactId": active_preview_artifact_id,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    assert await client.focus_annotation(
        active_preview_artifact_id=active_preview_artifact_id,
        annotation_id="annotation_42",
        scope_id="agent:fixture:webchat:fixture",
        tag_name="section",
        element_path=element_path,
        element_proof_sha256=element_proof,
    )
    request = json.loads(calls[1]["body"])
    assert request == {
        "version": 3,
        "method": "focusAnnotation",
        "request": {
            "version": 3,
            "activePreviewArtifactId": active_preview_artifact_id,
            "annotationId": "annotation_42",
            "scopeId": "agent:fixture:webchat:fixture",
            "tagName": "section",
            "elementPath": element_path,
            "elementProofSha256": element_proof,
        },
    }
    serialized = json.dumps(request).lower()
    assert "surfaceid" not in serialized
    assert "selector" not in serialized
    assert "javascript" not in serialized
    assert "cdp" not in serialized

    calls.clear()
    with pytest.raises(ValueError, match="element path"):
        await client.focus_annotation(
            active_preview_artifact_id=active_preview_artifact_id,
            annotation_id="annotation_42",
            scope_id="agent:fixture:webchat:fixture",
            tag_name="section",
            element_path='[["", "html", 1]]',
            element_proof_sha256=element_proof,
        )
    assert calls == []

    _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "focusAnnotation",
            "value": {"focused": True, "rendererLocator": "#untrusted"},
        },
    )
    with pytest.raises(DesktopArtifactBridgeError, match="focus response is invalid"):
        await client.focus_annotation(
            active_preview_artifact_id=active_preview_artifact_id,
            annotation_id="annotation_42",
            scope_id="agent:fixture:webchat:fixture",
            tag_name="section",
            element_path=element_path,
            element_proof_sha256=element_proof,
        )

    _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "focusAnnotation",
            "value": {
                "focused": True,
                "activePreviewArtifactId": "art-different-preview",
            },
        },
    )
    with pytest.raises(DesktopArtifactBridgeError, match="focus response is invalid"):
        await client.focus_annotation(
            active_preview_artifact_id=active_preview_artifact_id,
            annotation_id="annotation_42",
            scope_id="agent:fixture:webchat:fixture",
            tag_name="section",
            element_path=element_path,
            element_proof_sha256=element_proof,
        )


@pytest.mark.asyncio
async def test_reload_surface_has_no_raw_transport_or_surface_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_response(
        monkeypatch,
        {"ok": True, "method": "reloadSurface", "value": {"reloaded": True}},
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    assert await client.reload_surface() is True

    request = json.loads(calls[1]["body"])
    assert request == {
        "version": 3,
        "method": "reloadSurface",
        "request": {"version": 3},
    }
    serialized = json.dumps(request)
    assert "surfaceId" not in serialized
    assert "url" not in serialized.lower()
    assert "javascript" not in serialized.lower()
    assert "cdp" not in serialized.lower()


@pytest.mark.asyncio
async def test_typed_screenshot_decodes_bounded_png(monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\n"
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "screenshot",
            "value": {
                "mime": "image/png",
                "dataBase64": base64.b64encode(png).decode(),
                "width": 20,
                "height": 10,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    result = await client.screenshot()

    assert result.data == png
    assert result.mime == "image/png"
    assert result.width == 20
    assert result.height == 10


@pytest.mark.asyncio
async def test_bridge_errors_are_sanitized_and_response_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_response(
        monkeypatch,
        {"ok": False, "code": "unsupported", "message": "Capability is disabled."},
        status=503,
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())
    with pytest.raises(DesktopArtifactBridgeError) as raised:
        await client.reload_surface()
    assert raised.value.code == "unsupported"
    assert raised.value.status == 503
    assert _token() not in str(raised.value)

    _install_response(
        monkeypatch,
        {"ok": True},
        declared_length=str(16 * 1024 * 1024 + 1),
    )
    with pytest.raises(DesktopArtifactBridgeError, match="too large") as oversized:
        await client.capabilities()
    assert oversized.value.code == "response-too-large"


@pytest.mark.asyncio
async def test_invalid_typed_arguments_never_reach_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_response(monkeypatch, {"ok": True})
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    with pytest.raises(ValueError, match="anchor"):
        await client.browser_click(anchor="document.querySelector('body')")
    with pytest.raises(ValueError, match="inspection"):
        await client.browser_inspect(scope="document", max_nodes=201)
    with pytest.raises(ValueError, match="deadline"):
        await client.reload_surface(deadline_ms=60_001)
    assert calls == []
