"""Typed client for the Electron main-process artifact bridge.

The endpoint and bearer token exist only in the environment of a
Desktop-managed Gateway.  This module validates that authority once and exposes
only the fixed protocol operations; it intentionally has no generic URL,
JavaScript, CDP, or arbitrary-method entry point.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http.client
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

DESKTOP_ARTIFACT_BRIDGE_URL_ENV: Final = "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_URL"
DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV: Final = "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN"
DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION: Final = 3

_LOOPBACK_HOST: Final = "127.0.0.1"
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ANCHOR_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPAQUE_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TAG_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9._:-]{0,63}$")
_SHA256_RE: Final = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_ID_RE: Final = re.compile(r"^art-[A-Za-z0-9_-]{1,200}$")
_MAX_REQUEST_BYTES: Final = 64 * 1024
_MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
_MAX_SCREENSHOT_BYTES: Final = 12 * 1024 * 1024
_MIN_DEADLINE_MS: Final = 100
_MAX_DEADLINE_MS: Final = 60_000

BridgeMethod = Literal[
    "captureSelection",
    "resolveAnnotationSelection",
    "focusAnnotation",
    "browserInspect",
    "browserAct",
    "screenshot",
    "officeFlush",
    "reloadSurface",
]
BrowserInspectScope = Literal["document", "selection", "viewport"]
BrowserKey = Literal[
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "Space",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
]

_BRIDGE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "captureSelection",
        "resolveAnnotationSelection",
        "focusAnnotation",
        "browserInspect",
        "browserAct",
        "screenshot",
        "officeFlush",
        "reloadSurface",
    }
)
_BROWSER_SCOPES: Final[frozenset[str]] = frozenset({"document", "selection", "viewport"})
_BROWSER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "Backspace",
        "Delete",
        "Space",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)


class DesktopArtifactBridgeError(RuntimeError):
    """Sanitized failure returned by the local bridge transport."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class DesktopArtifactBridgeCapabilities:
    version: int
    available: bool
    capture_selection: bool
    resolve_annotation_selection: bool
    focus_annotation: bool
    browser_inspect: bool
    browser_act: bool
    screenshot: bool
    office_flush: bool
    reload_surface: bool


@dataclass(frozen=True, slots=True)
class DesktopArtifactSelectionSnapshot:
    kind: str
    anchor: str | None
    text: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class DesktopArtifactResolvedAnnotationSelection:
    active_preview_artifact_id: str
    selection_id: str
    tag_name: str
    element_path: str
    dom_sha256: str | None
    element_proof_sha256: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class DesktopArtifactBrowserNode:
    anchor: str
    role: str | None
    name: str | None
    text: str | None
    interactive: bool
    disabled: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class DesktopArtifactBrowserSnapshot:
    scope: str
    nodes: tuple[DesktopArtifactBrowserNode, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class DesktopArtifactBrowserActResult:
    performed: bool
    changed: bool


@dataclass(frozen=True, slots=True)
class DesktopArtifactScreenshot:
    mime: Literal["image/png"]
    data: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DesktopArtifactOfficeFlushResult:
    flushed: bool
    revision: str | None


def _record(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DesktopArtifactBridgeError(
            "invalid-response", f"The Desktop bridge {label} is invalid."
        )
    return cast(dict[str, Any], value)


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise DesktopArtifactBridgeError(
            "invalid-response", f"The Desktop bridge {label} is invalid."
        )
    return value


def _optional_string(value: object, *, label: str, max_chars: int = 16_384) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_chars or "\x00" in value:
        raise DesktopArtifactBridgeError(
            "invalid-response", f"The Desktop bridge {label} is invalid."
        )
    return value


def _positive_dimension(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 32_768:
        raise DesktopArtifactBridgeError(
            "invalid-response", f"The Desktop bridge {label} is invalid."
        )
    return value


def _validate_token(value: str) -> str:
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError("Desktop artifact bridge token is invalid")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Desktop artifact bridge token is invalid") from exc
    if len(decoded) != 32:
        raise ValueError("Desktop artifact bridge token is invalid")
    return value


def _validate_endpoint(value: str) -> int:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Desktop artifact bridge URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != _LOOPBACK_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
        or port < 1
        or port > 65_535
    ):
        raise ValueError("Desktop artifact bridge URL is invalid")
    return port


def _annotation_element_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096 or "\x00" in value:
        raise ValueError("Desktop artifact annotation element path is invalid")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Desktop artifact annotation element path is invalid") from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 128:
        raise ValueError("Desktop artifact annotation element path is invalid")
    for segment in parsed:
        if (
            not isinstance(segment, list)
            or len(segment) != 3
            or not isinstance(segment[0], str)
            or len(segment[0]) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in segment[0])
            or not isinstance(segment[1], str)
            or not _TAG_NAME_RE.fullmatch(segment[1])
            or isinstance(segment[2], bool)
            or not isinstance(segment[2], int)
            or not 1 <= segment[2] <= 9_007_199_254_740_991
        ):
            raise ValueError("Desktop artifact annotation element path is invalid")
    if json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) != value:
        raise ValueError("Desktop artifact annotation element path is invalid")
    return value


class DesktopArtifactBridgeClient:
    """Async, fixed-method facade over the authenticated loopback endpoint."""

    __slots__ = ("_port", "_token")

    def __init__(self, *, endpoint: str, token: str) -> None:
        self._port = _validate_endpoint(endpoint)
        self._token = _validate_token(token)

    def __repr__(self) -> str:
        return "DesktopArtifactBridgeClient(loopback=<redacted>, token=<redacted>)"

    async def capabilities(self, *, deadline_ms: int = 2_000) -> DesktopArtifactBridgeCapabilities:
        response = await self._post(
            "/v1/capabilities",
            {"version": DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION},
            deadline_ms=deadline_ms,
        )
        if response.get("ok") is not True:
            raise self._response_error(response)
        value = _record(response.get("value"), label="capabilities response")
        if value.get("version") != DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge protocol version is invalid."
            )
        return DesktopArtifactBridgeCapabilities(
            version=DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
            available=_boolean(value.get("available"), label="available capability"),
            capture_selection=_boolean(value.get("captureSelection"), label="selection capability"),
            resolve_annotation_selection=_boolean(
                value.get("resolveAnnotationSelection"),
                label="annotation selection resolution capability",
            ),
            focus_annotation=_boolean(
                value.get("focusAnnotation"), label="annotation focus capability"
            ),
            browser_inspect=_boolean(
                value.get("browserInspect"), label="browser inspection capability"
            ),
            browser_act=_boolean(value.get("browserAct"), label="browser action capability"),
            screenshot=_boolean(value.get("screenshot"), label="screenshot capability"),
            office_flush=_boolean(value.get("officeFlush"), label="Office flush capability"),
            reload_surface=_boolean(value.get("reloadSurface"), label="reload capability"),
        )

    async def capture_selection(
        self, *, deadline_ms: int = 2_000
    ) -> DesktopArtifactSelectionSnapshot:
        value = await self._call("captureSelection", {}, deadline_ms=deadline_ms)
        kind = value.get("kind")
        if kind not in {"none", "text", "cell", "range", "shape", "slide", "dom"}:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge selection kind is invalid."
            )
        anchor = _optional_string(value.get("anchor"), label="selection anchor", max_chars=128)
        if anchor is not None and not _ANCHOR_RE.fullmatch(anchor):
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge selection anchor is invalid."
            )
        return DesktopArtifactSelectionSnapshot(
            kind=kind,
            anchor=anchor,
            text=_optional_string(value.get("text"), label="selection text"),
            truncated=(
                _boolean(value.get("truncated"), label="selection truncation flag")
                if "truncated" in value
                else False
            ),
        )

    async def resolve_annotation_selection(
        self,
        *,
        active_preview_artifact_id: str,
        selection_id: str,
        tag_name: str,
        element_path: str,
        element_proof_sha256: str,
        dom_sha256: str | None = None,
        deadline_ms: int = 2_000,
    ) -> DesktopArtifactResolvedAnnotationSelection:
        if not isinstance(active_preview_artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(
            active_preview_artifact_id
        ):
            raise ValueError("Desktop artifact preview identity is invalid")
        if not isinstance(selection_id, str) or not _OPAQUE_ID_RE.fullmatch(selection_id):
            raise ValueError("Desktop artifact annotation selection identifier is invalid")
        if not isinstance(tag_name, str) or not _TAG_NAME_RE.fullmatch(tag_name):
            raise ValueError("Desktop artifact annotation tag name is invalid")
        element_path = _annotation_element_path(element_path)
        if dom_sha256 is not None and (
            not isinstance(dom_sha256, str) or not _SHA256_RE.fullmatch(dom_sha256)
        ):
            raise ValueError("Desktop artifact annotation DOM digest is invalid")
        if not isinstance(element_proof_sha256, str) or not _SHA256_RE.fullmatch(
            element_proof_sha256
        ):
            raise ValueError("Desktop artifact annotation element proof is invalid")
        request: dict[str, object] = {
            "activePreviewArtifactId": active_preview_artifact_id,
            "selectionId": selection_id,
            "tagName": tag_name,
            "elementPath": element_path,
            "elementProofSha256": element_proof_sha256,
        }
        if dom_sha256 is not None:
            request["domSha256"] = dom_sha256
        value = await self._call(
            "resolveAnnotationSelection",
            request,
            deadline_ms=deadline_ms,
        )
        response_selection_id = _optional_string(
            value.get("selectionId"), label="annotation selection identifier", max_chars=128
        )
        response_active_preview_artifact_id = _optional_string(
            value.get("activePreviewArtifactId"),
            label="active preview artifact identity",
            max_chars=204,
        )
        response_tag_name = _optional_string(
            value.get("tagName"), label="annotation tag name", max_chars=64
        )
        response_element_path = _optional_string(
            value.get("elementPath"), label="annotation element path", max_chars=4_096
        )
        response_dom_sha256 = _optional_string(
            value.get("domSha256"), label="annotation DOM digest", max_chars=64
        )
        response_element_proof_sha256 = _optional_string(
            value.get("elementProofSha256"),
            label="annotation element proof",
            max_chars=64,
        )
        scope_id = _optional_string(
            value.get("scopeId"), label="annotation scope", max_chars=512
        )
        if (
            response_active_preview_artifact_id != active_preview_artifact_id
            or response_selection_id != selection_id
            or response_tag_name != tag_name
            or response_element_path != element_path
            or response_dom_sha256 != dom_sha256
            or response_element_proof_sha256 != element_proof_sha256
            or scope_id is None
            or not scope_id
            or any(ord(character) < 32 or ord(character) == 127 for character in scope_id)
        ):
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge annotation selection is invalid."
            )
        rect = _record(value.get("rect"), label="annotation selection rectangle")
        for field in ("x", "y", "width", "height"):
            coordinate = rect.get(field)
            if (
                not isinstance(coordinate, (int, float))
                or isinstance(coordinate, bool)
                or not math.isfinite(coordinate)
                or abs(coordinate) > 1_000_000
                or (field in {"width", "height"} and coordinate < 0)
            ):
                raise DesktopArtifactBridgeError(
                    "invalid-response", "The Desktop bridge annotation rectangle is invalid."
                )
        return DesktopArtifactResolvedAnnotationSelection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id=selection_id,
            tag_name=tag_name,
            element_path=element_path,
            dom_sha256=dom_sha256,
            element_proof_sha256=element_proof_sha256,
            scope_id=scope_id,
        )

    async def focus_annotation(
        self,
        *,
        active_preview_artifact_id: str,
        annotation_id: str,
        scope_id: str,
        tag_name: str,
        element_path: str,
        element_proof_sha256: str,
        deadline_ms: int = 2_000,
    ) -> bool:
        if not isinstance(active_preview_artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(
            active_preview_artifact_id
        ):
            raise ValueError("Desktop artifact preview identity is invalid")
        if not isinstance(annotation_id, str) or not _OPAQUE_ID_RE.fullmatch(annotation_id):
            raise ValueError("Desktop artifact annotation identifier is invalid")
        if (
            not isinstance(scope_id, str)
            or not scope_id
            or len(scope_id) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in scope_id)
        ):
            raise ValueError("Desktop artifact annotation scope is invalid")
        if not isinstance(tag_name, str) or not _TAG_NAME_RE.fullmatch(tag_name):
            raise ValueError("Desktop artifact annotation tag name is invalid")
        element_path = _annotation_element_path(element_path)
        if not isinstance(element_proof_sha256, str) or not _SHA256_RE.fullmatch(
            element_proof_sha256
        ):
            raise ValueError("Desktop artifact annotation element proof is invalid")
        value = await self._call(
            "focusAnnotation",
            {
                "activePreviewArtifactId": active_preview_artifact_id,
                "annotationId": annotation_id,
                "scopeId": scope_id,
                "tagName": tag_name,
                "elementPath": element_path,
                "elementProofSha256": element_proof_sha256,
            },
            deadline_ms=deadline_ms,
        )
        if value != {
            "focused": True,
            "activePreviewArtifactId": active_preview_artifact_id,
        }:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge annotation focus response is invalid."
            )
        return True

    async def browser_inspect(
        self,
        *,
        scope: BrowserInspectScope,
        max_nodes: int,
        deadline_ms: int = 5_000,
    ) -> DesktopArtifactBrowserSnapshot:
        if scope not in _BROWSER_SCOPES or isinstance(max_nodes, bool) or not 1 <= max_nodes <= 200:
            raise ValueError("Desktop artifact browser inspection arguments are invalid")
        value = await self._call(
            "browserInspect",
            {"scope": scope, "maxNodes": max_nodes},
            deadline_ms=deadline_ms,
        )
        response_scope = value.get("scope")
        raw_nodes = value.get("nodes")
        if response_scope not in _BROWSER_SCOPES or not isinstance(raw_nodes, list):
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge browser snapshot is invalid."
            )
        if len(raw_nodes) > max_nodes:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge returned too many browser nodes."
            )
        nodes: list[DesktopArtifactBrowserNode] = []
        for raw_node in raw_nodes:
            node = _record(raw_node, label="browser node")
            anchor = _optional_string(
                node.get("anchor"), label="browser node anchor", max_chars=128
            )
            if anchor is None or not _ANCHOR_RE.fullmatch(anchor):
                raise DesktopArtifactBridgeError(
                    "invalid-response", "The Desktop bridge browser node anchor is invalid."
                )
            nodes.append(
                DesktopArtifactBrowserNode(
                    anchor=anchor,
                    role=_optional_string(
                        node.get("role"), label="browser node role", max_chars=256
                    ),
                    name=_optional_string(
                        node.get("name"), label="browser node name", max_chars=4_096
                    ),
                    text=_optional_string(node.get("text"), label="browser node text"),
                    interactive=(
                        _boolean(node.get("interactive"), label="browser node interactive flag")
                        if "interactive" in node
                        else False
                    ),
                    disabled=(
                        _boolean(node.get("disabled"), label="browser node disabled flag")
                        if "disabled" in node
                        else False
                    ),
                    selected=(
                        _boolean(node.get("selected"), label="browser node selected flag")
                        if "selected" in node
                        else False
                    ),
                )
            )
        return DesktopArtifactBrowserSnapshot(
            scope=response_scope,
            nodes=tuple(nodes),
            truncated=_boolean(value.get("truncated"), label="browser snapshot truncation flag"),
        )

    async def browser_click(
        self, *, anchor: str, focus_only: bool = False, deadline_ms: int = 5_000
    ) -> DesktopArtifactBrowserActResult:
        self._validate_anchor(anchor)
        return await self._browser_act(
            {"action": "focus" if focus_only else "click", "anchor": anchor},
            deadline_ms=deadline_ms,
        )

    async def browser_type(
        self,
        *,
        anchor: str,
        text: str,
        replace: bool = False,
        deadline_ms: int = 5_000,
    ) -> DesktopArtifactBrowserActResult:
        self._validate_anchor(anchor)
        if not isinstance(text, str) or len(text) > 16_384 or "\x00" in text:
            raise ValueError("Desktop artifact browser text is invalid")
        if not isinstance(replace, bool):
            raise ValueError("Desktop artifact browser replace flag is invalid")
        return await self._browser_act(
            {"action": "type", "anchor": anchor, "text": text, "replace": replace},
            deadline_ms=deadline_ms,
        )

    async def browser_press(
        self, *, key: BrowserKey, deadline_ms: int = 5_000
    ) -> DesktopArtifactBrowserActResult:
        if key not in _BROWSER_KEYS:
            raise ValueError("Desktop artifact browser key is invalid")
        return await self._browser_act({"action": "press", "key": key}, deadline_ms=deadline_ms)

    async def browser_scroll(
        self,
        *,
        direction: Literal["up", "down", "left", "right"],
        amount: Literal["line", "page"],
        deadline_ms: int = 5_000,
    ) -> DesktopArtifactBrowserActResult:
        if direction not in {"up", "down", "left", "right"} or amount not in {"line", "page"}:
            raise ValueError("Desktop artifact browser scroll arguments are invalid")
        return await self._browser_act(
            {"action": "scroll", "direction": direction, "amount": amount},
            deadline_ms=deadline_ms,
        )

    async def screenshot(self, *, deadline_ms: int = 10_000) -> DesktopArtifactScreenshot:
        value = await self._call("screenshot", {}, deadline_ms=deadline_ms)
        if value.get("mime") != "image/png" or not isinstance(value.get("dataBase64"), str):
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge screenshot is invalid."
            )
        encoded = value["dataBase64"]
        if len(encoded) > ((_MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
            raise DesktopArtifactBridgeError(
                "response-too-large", "The Desktop bridge screenshot is too large."
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge screenshot is invalid."
            ) from exc
        if len(data) > _MAX_SCREENSHOT_BYTES:
            raise DesktopArtifactBridgeError(
                "response-too-large", "The Desktop bridge screenshot is too large."
            )
        return DesktopArtifactScreenshot(
            mime="image/png",
            data=data,
            width=_positive_dimension(value.get("width"), label="screenshot width"),
            height=_positive_dimension(value.get("height"), label="screenshot height"),
        )

    async def office_flush(self, *, deadline_ms: int = 15_000) -> DesktopArtifactOfficeFlushResult:
        value = await self._call("officeFlush", {}, deadline_ms=deadline_ms)
        return DesktopArtifactOfficeFlushResult(
            flushed=_boolean(value.get("flushed"), label="Office flush status"),
            revision=_optional_string(
                value.get("revision"), label="Office revision", max_chars=512
            ),
        )

    async def reload_surface(self, *, deadline_ms: int = 10_000) -> bool:
        value = await self._call("reloadSurface", {}, deadline_ms=deadline_ms)
        return _boolean(value.get("reloaded"), label="reload result")

    async def _browser_act(
        self, request: dict[str, object], *, deadline_ms: int
    ) -> DesktopArtifactBrowserActResult:
        value = await self._call("browserAct", request, deadline_ms=deadline_ms)
        return DesktopArtifactBrowserActResult(
            performed=_boolean(value.get("performed"), label="browser action performed flag"),
            changed=_boolean(value.get("changed"), label="browser action changed flag"),
        )

    async def _call(
        self,
        method: BridgeMethod,
        request: dict[str, object],
        *,
        deadline_ms: int,
    ) -> dict[str, Any]:
        if method not in _BRIDGE_METHODS:
            raise ValueError("Desktop artifact bridge method is invalid")
        response = await self._post(
            "/v1/invoke",
            {
                "version": DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
                "method": method,
                "request": {
                    "version": DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
                    **request,
                },
            },
            deadline_ms=deadline_ms,
        )
        if response.get("ok") is not True:
            raise self._response_error(response)
        if response.get("method") != method:
            raise DesktopArtifactBridgeError(
                "invalid-response", "The Desktop bridge method response is invalid."
            )
        return _record(response.get("value"), label="operation response")

    async def _post(
        self,
        path: Literal["/v1/capabilities", "/v1/invoke"],
        payload: dict[str, object],
        *,
        deadline_ms: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(deadline_ms, int)
            or isinstance(deadline_ms, bool)
            or not _MIN_DEADLINE_MS <= deadline_ms <= _MAX_DEADLINE_MS
        ):
            raise ValueError("Desktop artifact bridge deadline is invalid")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("Desktop artifact bridge request is too large")
        deadline_at = int(time.time() * 1_000) + deadline_ms
        return await asyncio.to_thread(
            self._post_sync,
            path,
            encoded,
            deadline_at,
            deadline_ms,
        )

    def _post_sync(
        self,
        path: str,
        body: bytes,
        deadline_at: int,
        deadline_ms: int,
    ) -> dict[str, Any]:
        connection = http.client.HTTPConnection(
            _LOOPBACK_HOST,
            self._port,
            timeout=(deadline_ms / 1_000) + 0.5,
        )
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-OpenSquilla-Deadline-At-Ms": str(deadline_at),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                try:
                    length = int(declared_length)
                except ValueError as exc:
                    raise DesktopArtifactBridgeError(
                        "invalid-response", "The Desktop bridge response length is invalid."
                    ) from exc
                if length < 0 or length > _MAX_RESPONSE_BYTES:
                    raise DesktopArtifactBridgeError(
                        "response-too-large", "The Desktop bridge response is too large."
                    )
            content_type = (
                (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            )
            if content_type != "application/json":
                raise DesktopArtifactBridgeError(
                    "invalid-response", "The Desktop bridge response type is invalid."
                )
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise DesktopArtifactBridgeError(
                    "response-too-large", "The Desktop bridge response is too large."
                )
            try:
                payload = _record(json.loads(raw), label="response")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DesktopArtifactBridgeError(
                    "invalid-response", "The Desktop bridge response is invalid."
                ) from exc
            if response.status < 200 or response.status >= 300:
                raise self._response_error(payload, status=response.status)
            return payload
        except DesktopArtifactBridgeError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise DesktopArtifactBridgeError(
                "transport-unavailable", "The Desktop artifact bridge is unavailable."
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _response_error(
        response: Mapping[str, object], *, status: int | None = None
    ) -> DesktopArtifactBridgeError:
        code = response.get("code")
        message = response.get("message")
        safe_code = code if isinstance(code, str) and 1 <= len(code) <= 64 else "bridge-failed"
        safe_message = (
            message
            if isinstance(message, str) and 1 <= len(message) <= 1_000
            else "The Desktop artifact bridge request failed."
        )
        return DesktopArtifactBridgeError(safe_code, safe_message, status=status)

    @staticmethod
    def _validate_anchor(anchor: str) -> None:
        if not isinstance(anchor, str) or not _ANCHOR_RE.fullmatch(anchor):
            raise ValueError("Desktop artifact browser anchor is invalid")


def desktop_artifact_bridge_client_from_environment(
    environ: Mapping[str, str] | None = None,
) -> DesktopArtifactBridgeClient | None:
    """Build a bridge client only for an explicitly Desktop-managed Gateway."""

    source = os.environ if environ is None else environ
    if source.get("OPENSQUILLA_DESKTOP", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    endpoint = source.get(DESKTOP_ARTIFACT_BRIDGE_URL_ENV)
    token = source.get(DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV)
    if endpoint is None and token is None:
        return None
    if not endpoint or not token:
        raise ValueError("Desktop artifact bridge environment is incomplete")
    return DesktopArtifactBridgeClient(endpoint=endpoint, token=token)


_runtime_client_lock = threading.Lock()
_runtime_client_initialized = False
_runtime_client: DesktopArtifactBridgeClient | None = None


def initialize_desktop_artifact_bridge_client() -> DesktopArtifactBridgeClient | None:
    """Consume bridge credentials into a process-local client exactly once.

    Credentials are removed from ``os.environ`` even when validation fails, so
    provider, tool, and shell subprocesses can never inherit the bearer token.
    """

    global _runtime_client, _runtime_client_initialized
    with _runtime_client_lock:
        if _runtime_client_initialized:
            return _runtime_client
        snapshot = dict(os.environ)
        os.environ.pop(DESKTOP_ARTIFACT_BRIDGE_URL_ENV, None)
        os.environ.pop(DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV, None)
        try:
            _runtime_client = desktop_artifact_bridge_client_from_environment(snapshot)
        finally:
            _runtime_client_initialized = True
        return _runtime_client


def get_desktop_artifact_bridge_client() -> DesktopArtifactBridgeClient | None:
    """Return the runtime-only client, lazily initializing for embedded boots."""

    return initialize_desktop_artifact_bridge_client()


__all__ = [
    "DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV",
    "DESKTOP_ARTIFACT_BRIDGE_URL_ENV",
    "DesktopArtifactBridgeCapabilities",
    "DesktopArtifactBridgeClient",
    "DesktopArtifactBridgeError",
    "DesktopArtifactBrowserActResult",
    "DesktopArtifactBrowserNode",
    "DesktopArtifactBrowserSnapshot",
    "DesktopArtifactOfficeFlushResult",
    "DesktopArtifactResolvedAnnotationSelection",
    "DesktopArtifactScreenshot",
    "DesktopArtifactSelectionSnapshot",
    "desktop_artifact_bridge_client_from_environment",
    "get_desktop_artifact_bridge_client",
    "initialize_desktop_artifact_bridge_client",
]
