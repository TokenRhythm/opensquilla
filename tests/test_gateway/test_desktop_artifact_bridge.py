from __future__ import annotations

import asyncio
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
    TurnAuthorityCleanup,
    desktop_artifact_bridge_client_from_environment,
    desktop_artifact_bridge_token_valid,
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


def _install_response_sequence(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, object]],
) -> list[dict[str, Any]]:
    """Return one deterministic response per loopback connection."""

    calls: list[dict[str, Any]] = []
    responses = [_Response(payload) for payload in payloads]
    next_response = 0

    def connection_factory(*args: object, **kwargs: object) -> _Connection:
        nonlocal next_response
        response = responses[min(next_response, len(responses) - 1)]
        next_response += 1
        return _Connection(response, calls, *args, **kwargs)

    monkeypatch.setattr(bridge_module.http.client, "HTTPConnection", connection_factory)
    return calls


@pytest.mark.asyncio
async def test_v5_binding_is_opaque_bound_and_released_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_token = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()
    capabilities = {
        "version": 5,
        "available": True,
        "captureSelection": False,
        "resolveAnnotationSelection": True,
        "focusAnnotation": True,
        "browserInspect": True,
        "browserAct": True,
        "screenshot": True,
        "officeFlush": False,
        "reloadSurface": True,
        "bindCandidatePreview": True,
        "restoreCanonicalPreview": True,
    }
    advertised_capabilities = {**capabilities, "browserAct": False}
    calls = _install_response_sequence(monkeypatch, [
        {"ok": True, "value": advertised_capabilities},
        {
            "ok": True,
            "value": {
                "version": 5,
                "bindingToken": binding_token,
                "capabilities": capabilities,
            },
        },
        {"ok": True, "method": "reloadSurface", "value": {"reloaded": True}},
        {"ok": True, "value": {"released": True}},
    ])
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    bound = await client.acquire_binding()
    assert bound is not None
    assert (await bound.capabilities()).browser_act is True
    assert await bound.reload_surface() is True
    await bound.aclose()
    await bound.aclose()

    requests = [json.loads(call["body"]) for call in calls if call.get("method") == "POST"]
    assert requests[0] == {"version": 5}
    assert requests[1] == {"version": 5}
    assert requests[2]["bindingToken"] == binding_token
    assert requests[2]["version"] == 5
    assert requests[3] == {"version": 5, "bindingToken": binding_token}
    assert len(requests) == 4
    assert binding_token not in repr(bound)


@pytest.mark.asyncio
async def test_process_scoped_client_keeps_unbound_invocations_on_v4_after_v5_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_token = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()
    capabilities = {
        "version": 5,
        "available": True,
        "captureSelection": False,
        "resolveAnnotationSelection": True,
        "focusAnnotation": True,
        "browserInspect": True,
        "browserAct": True,
        "screenshot": True,
        "officeFlush": False,
        "reloadSurface": True,
        "bindCandidatePreview": True,
        "restoreCanonicalPreview": True,
    }
    calls = _install_response_sequence(
        monkeypatch,
        [
            {"ok": True, "value": capabilities},
            {
                "ok": True,
                "value": {
                    "version": 5,
                    "bindingToken": binding_token,
                    "capabilities": capabilities,
                },
            },
            {"ok": True, "method": "reloadSurface", "value": {"reloaded": True}},
            {"ok": True, "value": {"released": True}},
        ],
    )
    client = DesktopArtifactBridgeClient(
        endpoint="http://127.0.0.1:4321",
        token=_token(),
    )

    bound = await client.acquire_binding()
    assert bound is not None
    assert await client.reload_surface() is True
    await bound.aclose()

    requests = [json.loads(call["body"]) for call in calls if call.get("method") == "POST"]
    assert requests[2] == {
        "version": 4,
        "method": "reloadSurface",
        "request": {"version": 4},
    }
    assert "bindingToken" not in requests[2]


@pytest.mark.asyncio
async def test_v5_acquire_validation_failure_releases_new_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_token = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode()
    capabilities = {
        "version": 5,
        "available": True,
        "captureSelection": False,
        "resolveAnnotationSelection": True,
        "focusAnnotation": True,
        "browserInspect": True,
        "browserAct": True,
        "screenshot": True,
        "officeFlush": False,
        "reloadSurface": True,
        "bindCandidatePreview": True,
        "restoreCanonicalPreview": True,
    }
    calls = _install_response_sequence(
        monkeypatch,
        [
            {"ok": True, "value": capabilities},
            {
                "ok": True,
                "value": {
                    "version": 5,
                    "bindingToken": binding_token,
                    "capabilities": {**capabilities, "browserInspect": "invalid"},
                },
            },
            {"ok": True, "value": {"released": True}},
        ],
    )
    client = DesktopArtifactBridgeClient(
        endpoint="http://127.0.0.1:4321",
        token=_token(),
    )

    with pytest.raises(DesktopArtifactBridgeError, match="inspection capability"):
        await client.acquire_binding()

    requests = [json.loads(call["body"]) for call in calls if call.get("method") == "POST"]
    assert requests[-1] == {"version": 5, "bindingToken": binding_token}


@pytest.mark.asyncio
async def test_turn_authority_cleanup_is_cancellation_safe_and_exactly_once() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def release() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()

    cleanup = TurnAuthorityCleanup(release)
    first = asyncio.create_task(cleanup.aclose())
    second = asyncio.create_task(cleanup.aclose())
    await started.wait()
    first.cancel()
    finish.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    await cleanup.aclose()
    assert calls == 1


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


def test_process_local_token_verifier_survives_environment_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate preview auth must continue after startup consumes env creds."""

    token = _token()
    monkeypatch.setattr(bridge_module, "_runtime_client_initialized", False)
    monkeypatch.setattr(bridge_module, "_runtime_client", None)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv(DESKTOP_ARTIFACT_BRIDGE_URL_ENV, "http://127.0.0.1:1234")
    monkeypatch.setenv(DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV, token)

    client = bridge_module.initialize_desktop_artifact_bridge_client()
    assert client is not None
    assert DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV not in bridge_module.os.environ
    assert desktop_artifact_bridge_token_valid(token) is True
    assert desktop_artifact_bridge_token_valid("wrong-token") is False


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
async def test_candidate_handle_is_carried_on_browser_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_handle = "candidate_0123456789abcdef"
    calls = _install_response_sequence(
        monkeypatch,
        [
            {
                "ok": True,
                "method": "browserAct",
                "value": {"performed": True, "changed": True},
            },
            {
                "ok": True,
                "method": "browserAct",
                "value": {"performed": True, "changed": True},
            },
            {
                "ok": True,
                "method": "browserAct",
                "value": {"performed": True, "changed": True},
            },
            {
                "ok": True,
                "method": "browserAct",
                "value": {"performed": True, "changed": True},
            },
            {
                "ok": True,
                "method": "screenshot",
                "value": {
                    "mime": "image/png",
                    "dataBase64": base64.b64encode(b"png").decode(),
                    "width": 1,
                    "height": 1,
                },
            },
            {
                "ok": True,
                "method": "reloadSurface",
                "value": {"reloaded": True},
            },
        ],
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    await client.browser_click(anchor="a1", candidate_handle=candidate_handle)
    await client.browser_type(anchor="a1", text="x", candidate_handle=candidate_handle)
    await client.browser_press(key="Enter", candidate_handle=candidate_handle)
    await client.browser_scroll(
        direction="down",
        amount="line",
        candidate_handle=candidate_handle,
    )
    await client.screenshot(candidate_handle=candidate_handle)
    await client.reload_surface(candidate_handle=candidate_handle)

    requests = [json.loads(call["body"]) for call in calls if call.get("method") == "POST"]
    assert len(requests) == 6
    assert all(request["request"]["candidateHandle"] == candidate_handle for request in requests)
    assert [request["method"] for request in requests] == [
        "browserAct",
        "browserAct",
        "browserAct",
        "browserAct",
        "screenshot",
        "reloadSurface",
    ]

    calls.clear()
    with pytest.raises(ValueError, match="candidate handle"):
        await client.browser_press(key="Enter", candidate_handle="https://example.invalid")
    with pytest.raises(ValueError, match="candidate handle"):
        await client.screenshot(candidate_handle="not-a-handle")
    assert calls == []


@pytest.mark.asyncio
async def test_candidate_preview_lifecycle_is_opaque_and_protocol_v4_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_handle = "candidate_0123456789abcdef"
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "value": {
                "version": 4,
                "available": True,
                "captureSelection": False,
                "resolveAnnotationSelection": False,
                "focusAnnotation": False,
                "browserInspect": True,
                "browserAct": True,
                "screenshot": True,
                "officeFlush": False,
                "reloadSurface": True,
                "bindCandidatePreview": True,
                "restoreCanonicalPreview": True,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())
    capabilities = await client.capabilities()
    assert capabilities.version == 4
    assert capabilities.bind_candidate_preview is True
    assert capabilities.restore_canonical_preview is True

    bind_calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "bindCandidatePreview",
            "value": {"bound": True, "candidateHandle": candidate_handle},
        },
    )
    assert await client.bind_candidate_preview(candidate_handle) is True
    bind_request = json.loads(bind_calls[1]["body"])
    assert bind_request["version"] == 4
    assert bind_request["method"] == "bindCandidatePreview"
    assert bind_request["request"] == {
        "version": 4,
        "candidateHandle": candidate_handle,
    }
    serialized = json.dumps(bind_request).lower()
    assert "artifactid" not in serialized
    assert "path" not in serialized
    assert "url" not in serialized
    assert "javascript" not in serialized
    assert "cdp" not in serialized

    restore_calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "restoreCanonicalPreview",
            "value": {"restored": True},
        },
    )
    assert await client.restore_canonical_preview(candidate_handle) is True
    restore_request = json.loads(restore_calls[1]["body"])
    assert restore_request["method"] == "restoreCanonicalPreview"
    assert restore_request["request"] == {
        "version": 4,
        "candidateHandle": candidate_handle,
    }

    bind_calls.clear()
    with pytest.raises(ValueError, match="candidate handle"):
        await client.bind_candidate_preview("https://example.invalid/candidate")
    assert bind_calls == []


@pytest.mark.asyncio
async def test_candidate_preview_first_call_negotiates_protocol_v4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_handle = "candidate_0123456789abcdef"
    calls = _install_response_sequence(
        monkeypatch,
        [
            {
                "ok": True,
                "value": {
                    "version": 4,
                    "available": True,
                    "captureSelection": False,
                    "resolveAnnotationSelection": False,
                    "focusAnnotation": False,
                    "browserInspect": True,
                    "browserAct": True,
                    "screenshot": True,
                    "officeFlush": False,
                    "reloadSurface": True,
                    "bindCandidatePreview": True,
                    "restoreCanonicalPreview": True,
                },
            },
            {
                "ok": True,
                "method": "bindCandidatePreview",
                "value": {"bound": True, "candidateHandle": candidate_handle},
            },
        ],
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    assert await client.bind_candidate_preview(candidate_handle) is True
    requests = [
        json.loads(call["body"])
        for call in calls
        if call.get("method") == "POST"
    ]
    assert requests[0] == {"version": 3}
    assert requests[1]["version"] == 4
    assert requests[1]["method"] == "bindCandidatePreview"
    assert requests[1]["request"] == {
        "version": 4,
        "candidateHandle": candidate_handle,
    }

    restore_calls = _install_response_sequence(
        monkeypatch,
        [
            {
                "ok": True,
                "value": {
                    "version": 4,
                    "available": True,
                    "captureSelection": False,
                    "resolveAnnotationSelection": False,
                    "focusAnnotation": False,
                    "browserInspect": True,
                    "browserAct": True,
                    "screenshot": True,
                    "officeFlush": False,
                    "reloadSurface": True,
                    "bindCandidatePreview": True,
                    "restoreCanonicalPreview": True,
                },
            },
            {
                "ok": True,
                "method": "restoreCanonicalPreview",
                "value": {"restored": True},
            },
        ],
    )
    fresh_client = DesktopArtifactBridgeClient(
        endpoint="http://127.0.0.1:4321",
        token=_token(),
    )
    assert await fresh_client.restore_canonical_preview(candidate_handle) is True
    restore_requests = [
        json.loads(call["body"])
        for call in restore_calls
        if call.get("method") == "POST"
    ]
    assert restore_requests[0] == {"version": 3}
    assert restore_requests[1] == {
        "version": 4,
        "method": "restoreCanonicalPreview",
        "request": {
            "version": 4,
            "candidateHandle": candidate_handle,
        },
    }


@pytest.mark.asyncio
async def test_candidate_preview_lifecycle_fails_closed_for_v3_shell(
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
                "resolveAnnotationSelection": False,
                "focusAnnotation": False,
                "browserInspect": False,
                "browserAct": False,
                "screenshot": True,
                "officeFlush": False,
                "reloadSurface": True,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())
    await client.capabilities()
    with pytest.raises(DesktopArtifactBridgeError, match="protocol-v4"):
        await client.bind_candidate_preview("candidate_0123456789abcdef")
    with pytest.raises(DesktopArtifactBridgeError, match="protocol-v4"):
        await client.restore_canonical_preview("candidate_0123456789abcdef")
    assert sum(item.get("method") == "POST" for item in calls) == 1


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


@pytest.mark.asyncio
async def test_browser_snapshot_preserves_v4_active_surface_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "browserInspect",
            "value": {
                "scope": "document",
                "nodes": [],
                "truncated": False,
                "activePreviewArtifactId": "art-candidate",
                "scopeId": "agent:main:webchat:preview",
                "candidateHandle": "candidate_0123456789abcdef",
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    snapshot = await client.browser_inspect(scope="document", max_nodes=20)

    assert snapshot.active_preview_artifact_id == "art-candidate"
    assert snapshot.scope_id == "agent:main:webchat:preview"
    assert snapshot.candidate_handle == "candidate_0123456789abcdef"


@pytest.mark.asyncio
async def test_browser_inspect_carries_candidate_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_handle = "candidate_0123456789abcdef"
    calls = _install_response(
        monkeypatch,
        {
            "ok": True,
            "method": "browserInspect",
            "value": {
                "scope": "document",
                "nodes": [],
                "truncated": False,
                "activePreviewArtifactId": "art-candidate",
                "scopeId": "agent:main:webchat:preview",
                "candidateHandle": candidate_handle,
            },
        },
    )
    client = DesktopArtifactBridgeClient(endpoint="http://127.0.0.1:4321", token=_token())

    await client.browser_inspect(
        scope="document",
        max_nodes=1,
        candidate_handle=candidate_handle,
    )

    request = next(call for call in calls if call.get("method") == "POST")
    payload = json.loads(request["body"])
    assert payload["request"]["candidateHandle"] == candidate_handle
