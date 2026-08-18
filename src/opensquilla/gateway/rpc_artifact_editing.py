"""Artifact IDE RPC surface layered beside the immutable artifact API.

Existing ``artifacts.list/get`` payloads stay untouched.  These handlers adopt
one immutable ArtifactRef into a stable logical document, then expose revision,
change-set, comment, context, and HTML-source operations using ArtifactSession's
CAS guarantees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from lxml import etree  # type: ignore[import-untyped]

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    Anchor,
    AnchorKind,
    AnchorState,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    Document,
    EditSession,
    MutationAttempt,
    PromptAnnotation,
    PromptAnnotationStatus,
    Revision,
    RevisionSource,
    WriterLease,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session.html_anchors import (
    HtmlAnchorChangedError,
    remap_html_anchor,
    target_projection,
)
from opensquilla.artifact_session.html_anchors import (
    canonical_browser_dom_digest as shared_browser_dom_digest,
)
from opensquilla.artifact_session.html_anchors import (
    canonical_element_at_path as shared_element_at_path,
)
from opensquilla.artifact_session.html_anchors import (
    canonical_element_proof_sha256 as shared_element_proof_sha256,
)
from opensquilla.artifact_session.html_anchors import (
    canonical_opening_anchor as shared_canonical_opening_anchor,
)
from opensquilla.artifact_session.html_anchors import (
    parse_element_path as shared_parse_element_path,
)
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
)
from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    artifact_product_error,
    logged_artifact_product_error,
)
from opensquilla.gateway.desktop_artifact_bridge import (
    DesktopArtifactBridgeError,
    get_desktop_artifact_bridge_client,
)
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    get_dispatcher,
)
from opensquilla.gateway.rpc_artifacts import _session_id_for_key
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.websocket import get_registry
from opensquilla.paths import media_root_from_config
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.tools.builtin.document_format_adapters import (
    DocumentAdapterError,
    get_document_format_adapter,
    probe_document_format_adapter,
    validate_editable_html_source,
)

_d = get_dispatcher()

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_MAX_SOURCE_PATCHES = 100
_MAX_PROMPT_ANNOTATION_BYTES = 16_384
_EDIT_SESSION_TTL_MS = 60_000
_HTML_TAG_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9:-]{0,127}$")
_OPAQUE_ANNOTATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_OFFSET_ENCODING = "unicode-code-point"
class _OpeningTagCollector(HTMLParser):
    """Collect canonical source opening-tag spans with raw spelling intact."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0]
        self.tags: list[tuple[int, int, str]] = []
        self.boolean_attributes: dict[tuple[int, int], tuple[str, ...]] = {}
        for index, character in enumerate(source):
            if character == "\n":
                self.line_starts.append(index + 1)

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, column = self.getpos()
        if line < 1 or line > len(self.line_starts):
            return
        start = self.line_starts[line - 1] + column
        raw = self.get_starttag_text()
        if not raw:
            return
        end = start + len(raw)
        self.tags.append((start, end, tag.lower()))
        self.boolean_attributes[(start, end)] = tuple(
            name.lower() for name, value in attrs if value is None
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)


def _opening_tags(source: str) -> tuple[tuple[int, int, str], ...]:
    collector = _OpeningTagCollector(source)
    try:
        collector.feed(source)
        collector.close()
    except (AssertionError, ValueError):
        return ()
    return tuple(collector.tags)


_HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
_SVG_HTML_INTEGRATION_POINTS = frozenset({"desc", "foreignobject", "title"})
_SVG_ATTRIBUTE_ADJUSTMENTS = {
    "attributename": "attributeName",
    "attributetype": "attributeType",
    "basefrequency": "baseFrequency",
    "baseprofile": "baseProfile",
    "calcmode": "calcMode",
    "clippathunits": "clipPathUnits",
    "diffuseconstant": "diffuseConstant",
    "edgemode": "edgeMode",
    "filterunits": "filterUnits",
    "glyphref": "glyphRef",
    "gradienttransform": "gradientTransform",
    "gradientunits": "gradientUnits",
    "kernelmatrix": "kernelMatrix",
    "kernelunitlength": "kernelUnitLength",
    "keypoints": "keyPoints",
    "keysplines": "keySplines",
    "keytimes": "keyTimes",
    "lengthadjust": "lengthAdjust",
    "limitingconeangle": "limitingConeAngle",
    "markerheight": "markerHeight",
    "markerunits": "markerUnits",
    "markerwidth": "markerWidth",
    "maskcontentunits": "maskContentUnits",
    "maskunits": "maskUnits",
    "numoctaves": "numOctaves",
    "pathlength": "pathLength",
    "patterncontentunits": "patternContentUnits",
    "patterntransform": "patternTransform",
    "patternunits": "patternUnits",
    "pointsatx": "pointsAtX",
    "pointsaty": "pointsAtY",
    "pointsatz": "pointsAtZ",
    "preservealpha": "preserveAlpha",
    "preserveaspectratio": "preserveAspectRatio",
    "primitiveunits": "primitiveUnits",
    "refx": "refX",
    "refy": "refY",
    "repeatcount": "repeatCount",
    "repeatdur": "repeatDur",
    "requiredextensions": "requiredExtensions",
    "requiredfeatures": "requiredFeatures",
    "specularconstant": "specularConstant",
    "specularexponent": "specularExponent",
    "spreadmethod": "spreadMethod",
    "startoffset": "startOffset",
    "stddeviation": "stdDeviation",
    "stitchtiles": "stitchTiles",
    "surfacescale": "surfaceScale",
    "systemlanguage": "systemLanguage",
    "tablevalues": "tableValues",
    "targetx": "targetX",
    "targety": "targetY",
    "textlength": "textLength",
    "viewbox": "viewBox",
    "viewtarget": "viewTarget",
    "xchannelselector": "xChannelSelector",
    "ychannelselector": "yChannelSelector",
    "zoomandpan": "zoomAndPan",
}
_SVG_FOREIGN_ATTRIBUTE_ADJUSTMENTS = {
    "xlink:actuate": ("http://www.w3.org/1999/xlink", "actuate"),
    "xlink:arcrole": ("http://www.w3.org/1999/xlink", "arcrole"),
    "xlink:href": ("http://www.w3.org/1999/xlink", "href"),
    "xlink:role": ("http://www.w3.org/1999/xlink", "role"),
    "xlink:show": ("http://www.w3.org/1999/xlink", "show"),
    "xlink:title": ("http://www.w3.org/1999/xlink", "title"),
    "xlink:type": ("http://www.w3.org/1999/xlink", "type"),
    "xml:lang": ("http://www.w3.org/XML/1998/namespace", "lang"),
    "xml:space": ("http://www.w3.org/XML/1998/namespace", "space"),
    "xmlns": ("http://www.w3.org/2000/xmlns/", "xmlns"),
    "xmlns:xlink": ("http://www.w3.org/2000/xmlns/", "xlink"),
}


def _element_name(element: etree._Element) -> tuple[str, str]:
    raw_tag = element.tag
    if not isinstance(raw_tag, str):
        return "", ""
    if raw_tag.startswith("{"):
        namespace, local_name = raw_tag[1:].split("}", 1)
    else:
        namespace, local_name = "", raw_tag
    if namespace == _HTML_NAMESPACE:
        namespace = ""
    local_name = local_name.lower()
    if namespace:
        return namespace, local_name
    parent = element.getparent()
    if local_name == "svg":
        return _SVG_NAMESPACE, local_name
    if local_name == "math":
        return _MATHML_NAMESPACE, local_name
    if parent is not None and isinstance(parent.tag, str):
        parent_namespace, parent_name = _element_name(parent)
        if parent_namespace == _SVG_NAMESPACE:
            if parent_name in _SVG_HTML_INTEGRATION_POINTS:
                return "", local_name
            return _SVG_NAMESPACE, local_name
        if parent_namespace == _MATHML_NAMESPACE:
            return _MATHML_NAMESPACE, local_name
    return "", local_name


def _element_children(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def _dom_elements_preorder(root: etree._Element) -> list[etree._Element]:
    elements: list[etree._Element] = []
    stack = [root]
    while stack:
        current = stack.pop()
        elements.append(current)
        stack.extend(reversed(_element_children(current)))
    return elements


_HTML_HEAD_ELEMENTS = frozenset(
    {"base", "basefont", "bgsound", "link", "meta", "noframes", "script", "style", "title"}
)


def _normalize_browser_html_dom(root: etree._Element, *, source: str | None = None) -> None:
    """Reconcile conservative HTML5 implied nodes that lxml omits.

    The Electron digest is still the authority. This normalization merely lets
    common fragments reach the same documentElement shape; any remaining parser
    difference produces a digest mismatch and fails closed.
    """

    if _element_name(root) != ("", "html"):
        return
    direct = _element_children(root)
    head = next((child for child in direct if _element_name(child) == ("", "head")), None)
    body = next((child for child in direct if _element_name(child) == ("", "body")), None)
    if head is None:
        head = etree.Element("head")
        root.insert(0, head)
    if body is None:
        body = etree.Element("body")
        root.append(body)

    # lxml may keep body content in an implied head after a head-only token.
    seen_body_content = False
    for child in list(_element_children(head)):
        if _element_name(child)[1] not in _HTML_HEAD_ELEMENTS:
            seen_body_content = True
        if seen_body_content:
            head.remove(child)
            body.append(child)
    for child in list(_element_children(root)):
        if child is head or child is body:
            continue
        root.remove(child)
        body.append(child)

    # Browsers insert tbody around a direct run of tr children.
    for table in list(_dom_elements_preorder(root)):
        if _element_name(table) != ("", "table"):
            continue
        run: list[etree._Element] = []
        for child in list(_element_children(table)) + [None]:
            if child is not None and _element_name(child) == ("", "tr"):
                run.append(child)
                continue
            if not run:
                continue
            first_index = table.index(run[0])
            tbody = etree.Element("tbody")
            table.insert(first_index, tbody)
            for row in run:
                table.remove(row)
                tbody.append(row)
            run = []

    if source is not None:
        collector = _OpeningTagCollector(source)
        try:
            collector.feed(source)
            collector.close()
        except (AssertionError, ValueError):
            return
        elements = _dom_elements_preorder(root)
        cursor = 0
        for start, end, source_tag in collector.tags:
            matched: etree._Element | None = None
            while cursor < len(elements):
                candidate = elements[cursor]
                cursor += 1
                if _element_name(candidate)[1] == source_tag:
                    matched = candidate
                    break
            if matched is None:
                return
            for name in collector.boolean_attributes.get((start, end), ()):
                if name in matched.attrib:
                    matched.attrib[name] = ""


def _browser_dom_digest(root: etree._Element, *, source: str | None = None) -> str:
    _normalize_browser_html_dom(root, source=source)
    tokens: list[str] = []
    token_bytes = 0
    node_count = 0

    def append_token(value: str) -> None:
        nonlocal token_bytes
        token_bytes += len(value.encode("utf-8")) + 1
        if token_bytes > 4 * 1024 * 1024:
            raise ValueError("canonical DOM is too large")
        tokens.append(value)

    stack: list[tuple[str, object]] = [("element", root)]
    while stack:
        kind, value = stack.pop()
        node_count += 1
        if node_count > 50_000:
            raise ValueError("canonical DOM contains too many nodes")
        if kind == "close":
            append_token("X")
            continue
        if kind == "text":
            append_token(
                json.dumps(["T", str(value)], ensure_ascii=False, separators=(",", ":"))
            )
            continue

        element = value
        assert isinstance(element, etree._Element)
        namespace, tag_name = _element_name(element)
        attributes: list[list[str]] = []
        for raw_name, raw_value in element.attrib.items():
            if raw_name.startswith("{"):
                attr_namespace, attr_name = raw_name[1:].split("}", 1)
            else:
                attr_namespace, attr_name = "", raw_name
            if namespace == _SVG_NAMESPACE:
                attr_name = _SVG_ATTRIBUTE_ADJUSTMENTS.get(attr_name, attr_name)
            attributes.append([attr_namespace, attr_name, raw_value])
        attributes.sort(
            key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        )
        append_token(
            json.dumps(
                ["E", namespace, tag_name, attributes],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        children: list[tuple[str, object]] = []
        if element.text:
            children.append(("text", element.text))
        for child in element:
            if isinstance(child.tag, str):
                children.append(("element", child))
            if child.tail:
                children.append(("text", child.tail))
        stack.append(("close", element))
        stack.extend(reversed(children))

    return hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()


def _normalized_element_attributes(element: etree._Element) -> list[list[str]]:
    """Return the browser-compatible attribute tuples used by element proofs.

    Attribute order in source is not significant.  The compact JSON tuple is
    therefore sorted by Unicode code-point sequence so Python and the Desktop
    bridge produce byte-identical proof input even for non-BMP names/values.
    """

    element_namespace, _tag_name = _element_name(element)
    attributes: list[list[str]] = []
    for raw_name, raw_value in element.attrib.items():
        if raw_name.startswith("{"):
            attr_namespace, attr_name = raw_name[1:].split("}", 1)
        else:
            attr_namespace, attr_name = "", raw_name
        # HTML parsing adjusts foreign-content names/namespaces before the DOM
        # exposes Attr.namespaceURI/localName. lxml keeps the source token.
        if element_namespace in {_SVG_NAMESPACE, _MATHML_NAMESPACE} and not attr_namespace:
            adjusted_foreign = _SVG_FOREIGN_ATTRIBUTE_ADJUSTMENTS.get(attr_name)
            if adjusted_foreign is not None:
                attr_namespace, attr_name = adjusted_foreign
        if element_namespace == _SVG_NAMESPACE and not attr_namespace:
            attr_name = _SVG_ATTRIBUTE_ADJUSTMENTS.get(attr_name, attr_name)
        elif element_namespace == _MATHML_NAMESPACE and attr_name == "definitionurl":
            attr_name = "definitionURL"
        attributes.append([attr_namespace, attr_name, raw_value])
    attributes.sort(
        key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    )
    return attributes


def _element_nth_of_type(element: etree._Element) -> int:
    """Return the 1-based sibling index for the normalized namespace/tag."""

    parent = element.getparent()
    if parent is None:
        return 1
    wanted_name = _element_name(element)
    index = 0
    for sibling in _element_children(parent):
        if _element_name(sibling) == wanted_name:
            index += 1
        if sibling is element:
            return index
    raise ValueError("The selected element is detached from the canonical DOM")


def _element_proof_sha256(
    root: etree._Element,
    *,
    selected: etree._Element,
) -> str:
    """Hash only the selected element's source-backed ancestor identity.

    Text and descendants are intentionally excluded. Runtime changes elsewhere
    in the preview must not invalidate a source-backed selection, while changes
    to the selected element or any ancestor still fail closed.
    """

    ancestors: list[etree._Element] = []
    current: etree._Element | None = selected
    while current is not None:
        ancestors.append(current)
        if current is root:
            break
        current = current.getparent()
    if not ancestors or ancestors[-1] is not root:
        raise ValueError("The selected element is outside the canonical DOM")
    ancestors.reverse()

    tokens: list[str] = []
    for element in ancestors:
        namespace, tag_name = _element_name(element)
        tokens.append(
            json.dumps(
                [
                    namespace,
                    tag_name,
                    _element_nth_of_type(element),
                    _normalized_element_attributes(element),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()


def _parse_element_path(value: str) -> tuple[tuple[str, str, int], ...]:
    return shared_parse_element_path(value)


def _element_at_path(root: etree._Element, path: str) -> etree._Element:
    segments = _parse_element_path(path)
    root_namespace, root_tag = _element_name(root)
    if segments[0] != (root_namespace, root_tag, 1):
        raise ValueError("params.selection.elementPath does not match the canonical DOM")
    current = root
    for namespace, tag_name, wanted_index in segments[1:]:
        index = 0
        matched: etree._Element | None = None
        for child in _element_children(current):
            if _element_name(child) != (namespace, tag_name):
                continue
            index += 1
            if index == wanted_index:
                matched = child
                break
        if matched is None:
            raise ValueError("params.selection.elementPath does not match the canonical DOM")
        current = matched
    return current


def _opening_span_for_element(
    source: str,
    *,
    root: etree._Element,
    selected: etree._Element,
) -> tuple[int, int, str]:
    elements = _dom_elements_preorder(root)
    cursor = 0
    selected_span: tuple[int, int, str] | None = None
    for span in _opening_tags(source):
        _start, _end, source_tag = span
        matched: etree._Element | None = None
        while cursor < len(elements):
            candidate = elements[cursor]
            cursor += 1
            if _element_name(candidate)[1] == source_tag:
                matched = candidate
                break
        if matched is None:
            raise ValueError("HTML source cannot be mapped uniquely to its canonical DOM")
        if matched is selected:
            if selected_span is not None:
                raise ValueError("HTML selection maps to more than one opening tag")
            selected_span = span
    if selected_span is None:
        raise ValueError("The selected DOM element has no editable source opening tag")
    return selected_span


def _canonical_opening_anchor(
    source: str,
    *,
    element_path: str,
    expected_element_proof_sha256: str,
    expected_tag_name: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        return shared_canonical_opening_anchor(
            source,
            element_path=element_path,
            expected_element_proof_sha256=expected_element_proof_sha256,
            expected_tag_name=expected_tag_name,
        )
    except HtmlAnchorChangedError as exc:
        raise artifact_product_error(ArtifactProductErrorCode.DOCUMENT_CHANGED) from exc


# Compatibility aliases for tests and older internal imports. Production
# creation/focus paths use the shared pure module directly through the wrapper
# above, so DOM proof and source-span rules have one implementation.
_browser_dom_digest = shared_browser_dom_digest
_element_at_path = shared_element_at_path
_element_proof_sha256 = shared_element_proof_sha256


def _validate_source_offset_encoding(container: object) -> str:
    if not isinstance(container, dict):
        return _SOURCE_OFFSET_ENCODING
    value = container.get("offsetEncoding")
    # Omission remains compatible with the first ArtifactSession clients,
    # whose offsets were already Python/Unicode-code-point indexes.
    if value is None:
        return _SOURCE_OFFSET_ENCODING
    if value != _SOURCE_OFFSET_ENCODING:
        raise ValueError(f"offsetEncoding must be {_SOURCE_OFFSET_ENCODING}")
    return _SOURCE_OFFSET_ENCODING


def _require_string(params: dict[str, Any] | None, name: str) -> str:
    if not isinstance(params, dict) or name not in params:
        raise ValueError(f"params.{name} is required")
    value = params[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"params.{name} must be a non-empty string")
    value = value.strip()
    if len(value) > 2048:
        raise ValueError(f"params.{name} is too long")
    return value


def _optional_string(params: dict[str, Any] | None, name: str) -> str | None:
    if not isinstance(params, dict) or params.get(name) is None:
        return None
    return _require_string(params, name)


def _require_positive_int(params: dict[str, Any] | None, name: str) -> int:
    if not isinstance(params, dict) or name not in params:
        raise ValueError(f"params.{name} is required")
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"params.{name} must be a positive integer")
    return int(value)


def _bounded_limit(params: dict[str, Any] | None, *, default: int = 100) -> int:
    value = params.get("limit") if isinstance(params, dict) else None
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("params.limit must be a positive integer")
    return min(int(value), 500)


def _session_key(params: dict[str, Any] | None) -> str:
    return canonicalize_session_key(_require_string(params, "sessionKey"))


def _actor(ctx: RpcContext) -> Actor:
    public_id = getattr(ctx.principal, "token_public_id", None)
    actor_id = public_id if isinstance(public_id, str) and public_id else None
    if actor_id is None:
        actor_id = "local-owner" if ctx.principal.is_owner else ctx.principal.role
    return Actor(kind=ActorKind.USER, actor_id=actor_id)


async def _service(ctx: RpcContext) -> ArtifactSessionService:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            retryable=True,
            reason_code="service_unavailable",
        )
    return await ArtifactSessionService.from_session_storage(storage)


async def _scope(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> tuple[str, str, ArtifactSessionService]:
    session_key = _session_key(params)
    session_id = await _session_id_for_key(ctx, session_key)
    if session_id is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            reason_code="session_unavailable",
        )
    return session_key, session_id, await _service(ctx)


async def _session_epoch(ctx: RpcContext, session_key: str) -> int:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            retryable=True,
            reason_code="service_unavailable",
        )
    return int(await storage.get_epoch(session_key))


def _prompt_annotation_body(params: dict[str, Any] | None) -> str:
    value = params.get("body", "") if isinstance(params, dict) else ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_PROMPT_ANNOTATION_BYTES:
        raise ValueError("params.body must be a string no larger than 16 KiB")
    return value


def _not_found(kind: str, identifier: str) -> RpcHandlerError:
    del kind, identifier
    return artifact_product_error(
        ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
        reason_code="resource_unavailable",
    )


def _conflict(
    exc: Exception,
    *,
    code: ArtifactProductErrorCode = ArtifactProductErrorCode.DOCUMENT_CHANGED,
    operation: str = "artifact_document.mutate",
) -> RpcHandlerError:
    return logged_artifact_product_error(
        code,
        exc,
        operation=operation,
        retryable=False,
    )


async def _scoped_document(
    service: ArtifactSessionService,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
) -> Document:
    try:
        document = await service.get_document(document_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Document", document_id) from None
    if document.session_key != session_key or document.session_id != session_id:
        raise _not_found("Document", document_id)
    return document


async def _scoped_revision(
    service: ArtifactSessionService,
    *,
    document: Document,
    revision_id: str,
) -> Revision:
    try:
        revision = await service.get_revision(revision_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Revision", revision_id) from None
    if revision.document_id != document.document_id:
        raise _not_found("Revision", revision_id)
    return revision


async def _scoped_edit_session(
    service: ArtifactSessionService,
    *,
    edit_session_id: str,
    session_key: str,
    session_id: str,
    actor: Actor,
) -> tuple[EditSession, Document]:
    try:
        edit_session = await service.get_edit_session(edit_session_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("EditSession", edit_session_id) from None
    document = await _scoped_document(
        service,
        document_id=edit_session.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    if edit_session.user_id != actor.actor_id:
        raise _not_found("EditSession", edit_session_id)
    return edit_session, document


def _format_for(name: str, media_type: str, kind: ArtifactKind | None = None) -> str:
    suffix = Path(name).suffix.lower()
    mime = media_type.split(";", 1)[0].strip().lower()
    adapter = probe_document_format_adapter(name=name, media_type=mime)
    if adapter is not None:
        return adapter.format_id
    if suffix == ".docx" or mime == _DOCX_MIME:
        return "docx"
    if suffix == ".xlsx" or mime == _XLSX_MIME:
        return "xlsx"
    if suffix == ".pptx" or mime == _PPTX_MIME:
        return "pptx"
    if suffix in _HTML_SUFFIXES or mime in _HTML_MIMES or kind is ArtifactKind.HTML:
        return "html"
    return "other"


def _kind_for(ref: ArtifactRef) -> ArtifactKind:
    match _format_for(ref.name, ref.mime):
        case "docx":
            return ArtifactKind.DOCUMENT
        case "xlsx":
            return ArtifactKind.SPREADSHEET
        case "pptx":
            return ArtifactKind.PRESENTATION
        case "html":
            return ArtifactKind.HTML
        case _:
            return ArtifactKind.OTHER


def _capabilities(artifact_format: str) -> dict[str, Any]:
    # Every value here describes a Document. Immutable attachments and
    # deliverables advertise publish=false in the Workbench resource RPC.
    common = {
        "download": True,
        "versionHistory": True,
        "publish": True,
        "promptAnnotations": False,
    }
    if artifact_format == "html":
        adapter = get_document_format_adapter(artifact_format)
        adapter_capabilities = adapter.capabilities()
        selection_context = (
            adapter_capabilities.get("selectionContext") is True
            or adapter_capabilities.get("selection") is True
        ) and adapter_capabilities.get("promptAnnotations") is True
        return {
            **common,
            "preview": adapter_capabilities["preview"],
            "selectionContext": selection_context,
            "manualEdit": adapter_capabilities["manualEdit"],
            "agentEdit": adapter_capabilities["agentEdit"],
            "sourceEdit": adapter_capabilities["sourceEdit"],
            # Enabled only after the Desktop bridge is attached to Gateway.
            "browserUse": False,
            "selection": adapter_capabilities["selection"],
            "promptAnnotations": adapter_capabilities["promptAnnotations"],
            "engine": "html-source",
            "adapterId": adapter.format_id,
            "adapterVersion": adapter.adapter_version,
            "semanticOperations": adapter_capabilities["semanticOperations"],
        }
    if artifact_format in {"docx", "xlsx", "pptx"}:
        return {
            **common,
            "publish": False,
            "preview": False,
            "manualEdit": False,
            "agentEdit": False,
            "sourceEdit": False,
            "browserUse": False,
            "selectionContext": False,
            "selection": False,
            "engine": None,
            "unavailableReason": "office_adapter_not_available",
        }
    return {
        **common,
        "preview": False,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selectionContext": False,
        "selection": False,
        "engine": None,
        "unavailableReason": "unsupported_format",
    }


def _html_bundle_capabilities() -> dict[str, Any]:
    """Advertise only the bundle behavior implemented by the current workbench."""

    return {
        "download": True,
        "versionHistory": True,
        "publish": True,
        "promptAnnotations": False,
        "preview": True,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selection": False,
        "engine": None,
        "unavailableReason": "html_bundle_edit_not_supported",
    }


def _html_integrity_failure_capabilities() -> dict[str, Any]:
    """Fail closed when the immutable HTML material cannot be classified safely."""

    return {
        "download": True,
        "versionHistory": True,
        "publish": True,
        "promptAnnotations": False,
        "preview": False,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selection": False,
        "engine": None,
        "unavailableReason": "artifact_integrity_error",
    }


def _html_source_unavailable_capabilities(reason: str) -> dict[str, Any]:
    """Keep preview/download available while disabling canonical-source mutations."""

    return {
        "download": True,
        "versionHistory": True,
        "publish": True,
        "promptAnnotations": False,
        "preview": True,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selection": False,
        "engine": None,
        "unavailableReason": reason,
    }


async def _revision_capabilities(
    ctx: RpcContext,
    document: Document,
    revision: Revision,
) -> dict[str, Any]:
    artifact_format = _format_for(revision.filename, revision.media_type, document.kind)
    capabilities = _capabilities(artifact_format)
    if artifact_format != "html":
        return capabilities
    if document.session_id is None:
        return _html_integrity_failure_capabilities()

    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        supports_editing = await asyncio.to_thread(
            store.supports_single_file_editing,
            revision.artifact_id,
            session_id=document.session_id,
        )
    except (ArtifactNotFoundError, ArtifactIntegrityError, OSError, ValueError):
        return _html_integrity_failure_capabilities()
    if not supports_editing:
        return _html_bundle_capabilities()
    try:
        ref, path = await asyncio.to_thread(
            store.resolve_for_download,
            revision.artifact_id,
            session_id=document.session_id,
        )
        source = await asyncio.to_thread(_html_source, ref, path)
        validate_editable_html_source(source)
    except RpcHandlerError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        reason = (
            "html_source_encoding_unsupported"
            if exc.code == "ARTIFACT_SOURCE_ENCODING"
            or details.get("reasonCode") == "encoding_unsupported"
            else "html_source_unavailable"
        )
        return _html_source_unavailable_capabilities(reason)
    except (
        ArtifactNotFoundError,
        ArtifactIntegrityError,
        DocumentAdapterError,
        OSError,
        ValueError,
    ):
        return _html_integrity_failure_capabilities()
    if get_desktop_artifact_bridge_client() is None:
        # Preview annotations rely on a trusted Electron-owned CDP selection.
        # The ordinary Web UI keeps source editing, but must not expose a
        # button that can only fail after the user has entered an instruction.
        return {
            **capabilities,
            "selectionContext": False,
            "promptAnnotations": False,
        }
    return capabilities


def _revision_payload(revision: Revision) -> dict[str, Any]:
    return {
        "id": revision.revision_id,
        "documentId": revision.document_id,
        "parentRevisionId": revision.parent_revision_id,
        "generation": revision.generation,
        "artifactId": revision.artifact_id,
        "source": revision.source.value,
        "actorKind": revision.actor_kind.value,
        "actorId": revision.actor_id,
        "changeSetId": revision.change_set_id,
        "copiedFromRevisionId": revision.copied_from_revision_id,
        "sha256": revision.artifact_sha256,
        "name": revision.filename,
        "mime": revision.media_type,
        "size": revision.byte_size,
        "createdAt": revision.created_at,
        "schemaVersion": revision.schema_version,
        "downloadUrl": (
            f"/api/v1/artifact-documents/{revision.document_id}?revisionId={revision.revision_id}"
        ),
    }


def _document_payload(
    document: Document,
    head: Revision,
    *,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_format = _format_for(head.filename, head.media_type, document.kind)
    effective_capabilities = capabilities or _capabilities(artifact_format)
    return {
        "id": document.document_id,
        "sessionKey": document.session_key,
        "sessionId": document.session_id,
        "name": document.name,
        "kind": document.kind.value,
        "format": artifact_format,
        "headRevisionId": document.head_revision_id,
        "generation": document.generation,
        "stateRevision": document.state_revision,
        "capabilities": effective_capabilities,
        "editorState": (
            "source_ready"
            if effective_capabilities["sourceEdit"]
            else "preview_ready"
            if effective_capabilities["preview"]
            else "download_only"
        ),
        "latestDownloadUrl": f"/api/v1/artifact-documents/{document.document_id}",
        "createdAt": document.created_at,
        "updatedAt": document.updated_at,
        "schemaVersion": document.schema_version,
        "head": _revision_payload(head),
    }


def _change_set_payload(change_set: ChangeSet) -> dict[str, Any]:
    candidate = change_set.candidate_artifact
    return {
        "id": change_set.change_set_id,
        "documentId": change_set.document_id,
        "baseRevisionId": change_set.base_revision_id,
        "resultRevisionId": change_set.applied_revision_id,
        "turnId": change_set.turn_id,
        "state": change_set.status.value,
        "stateRevision": change_set.state_revision,
        "summary": change_set.summary,
        "operations": list(change_set.operations),
        "candidateArtifact": (
            None
            if candidate is None
            else {
                "id": candidate.artifact_id,
                "sha256": candidate.sha256,
                "name": candidate.filename,
                "mime": candidate.media_type,
                "size": candidate.byte_size,
            }
        ),
        "validation": change_set.validation,
        "createdByKind": change_set.created_by_kind.value,
        "createdById": change_set.created_by_id,
        "createdAt": change_set.created_at,
        "updatedAt": change_set.updated_at,
        "schemaVersion": change_set.schema_version,
    }


def _edit_session_payload(edit_session: EditSession) -> dict[str, Any]:
    """Return editor state without exposing lease ids or fencing tokens."""

    return {
        "id": edit_session.edit_session_id,
        "documentId": edit_session.document_id,
        "baseRevisionId": edit_session.base_revision_id,
        "lastSavedRevisionId": edit_session.last_saved_revision_id,
        "mode": edit_session.mode.value,
        "status": edit_session.status.value,
        "stateRevision": edit_session.state_revision,
        "expiresAt": edit_session.expires_at,
        "lastAccessAt": edit_session.last_access_at,
        "createdAt": edit_session.created_at,
        "updatedAt": edit_session.updated_at,
        "schemaVersion": edit_session.schema_version,
    }


def _prompt_annotation_payload(
    annotation: PromptAnnotation,
    *,
    anchor: Anchor,
    current_head_revision_id: str,
) -> dict[str, Any]:
    target_status, target_reason, target_kind, target_text = target_projection(anchor)
    return {
        "id": annotation.annotation_id,
        "documentId": annotation.document_id,
        "revisionId": annotation.revision_id,
        "anchorId": annotation.anchor_id,
        "anchor": _anchor_payload(anchor),
        "body": annotation.body,
        "status": annotation.status.value,
        "freshness": (
            "current"
            if annotation.revision_id == current_head_revision_id
            else "stale"
        ),
        "targetStatus": target_status,
        "targetReason": target_reason,
        "targetKind": target_kind,
        "targetText": target_text,
        "stateRevision": annotation.state_revision,
        "sentMessageId": annotation.sent_message_id,
        "sentTurnId": annotation.sent_turn_id,
        "sentOrder": annotation.sent_order,
        "createdAt": annotation.created_at,
        "updatedAt": annotation.updated_at,
        "schemaVersion": annotation.schema_version,
    }


def _anchor_payload(anchor: Anchor) -> dict[str, Any]:
    return {
        "anchorId": anchor.anchor_id,
        "documentId": anchor.document_id,
        "revisionId": anchor.revision_id,
        "kind": anchor.kind.value,
        "locator": anchor.locator,
        "quote": anchor.quote,
        "context": anchor.context,
        "state": anchor.state.value,
        "remappedFromAnchorId": anchor.remapped_from_anchor_id,
        "createdAt": anchor.created_at,
        "schemaVersion": anchor.schema_version,
    }


async def _prompt_annotation_anchor(
    service: ArtifactSessionService,
    annotation: PromptAnnotation,
) -> Anchor:
    try:
        anchor = await service.get_anchor(annotation.anchor_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Anchor", annotation.anchor_id) from None
    if (
        anchor.anchor_id != annotation.anchor_id
        or anchor.document_id != annotation.document_id
        or anchor.revision_id != annotation.revision_id
    ):
        raise _not_found("Anchor", annotation.anchor_id)
    return anchor


async def _document_with_head(
    ctx: RpcContext,
    service: ArtifactSessionService,
    document: Document,
    head: Revision | None = None,
) -> dict[str, Any]:
    effective_head = head or await service.get_revision(document.head_revision_id)
    capabilities = await _revision_capabilities(ctx, document, effective_head)
    return _document_payload(document, effective_head, capabilities=capabilities)


async def _mutation_document_payload(
    ctx: RpcContext,
    service: ArtifactSessionService,
    result: CommitResult,
) -> dict[str, Any]:
    """Return a coherent document projection for a durable mutation receipt.

    An idempotent replay can arrive after a later collaborator has advanced the
    document. The receipt must still identify the originally applied revision,
    while the mutable document projection must describe the current head rather
    than pairing a new headRevisionId with the old revision payload.
    """

    head = (
        result.revision
        if result.document.head_revision_id == result.revision.revision_id
        else None
    )
    return await _document_with_head(ctx, service, result.document, head)


async def _emit_artifact_state(
    ctx: RpcContext,
    *,
    session_key: str,
    service: ArtifactSessionService,
    document_id: str,
    action: str,
    revision_id: str | None = None,
    change_set_id: str | None = None,
) -> None:
    latest = await service.latest_audit_event(document_id)
    if latest is None:
        return
    payload = {
        "artifactEventSeq": latest.sequence,
        "documentId": document_id,
        "revisionId": revision_id,
        "changeSetId": change_set_id,
        "action": action,
    }
    bridge = EventBridge(ctx.subscription_manager, get_registry())
    # Keep the legacy session event during the compatibility window while the
    # format-neutral workbench migrates to the document lifecycle name.
    await bridge.emit(session_key, "session.event.artifact_state", payload)
    await bridge.emit(session_key, "document.state_changed", payload)


async def _commit_revision_copy_mutation(
    service: ArtifactSessionService,
    *,
    document: Document,
    target_revision: Revision,
    expected_head_revision_id: str,
    expected_state_revision: int,
    actor: Actor,
    turn_id: str,
    operations: tuple[dict[str, Any], ...],
    summary: str,
    source: RevisionSource,
    revision_event_type: str,
) -> tuple[CommitResult, ChangeSet, bool]:
    replay = await _applied_mutation_replay(
        service,
        document_id=document.document_id,
        turn_id=turn_id,
        base_revision_id=expected_head_revision_id,
        operations=operations,
        candidate_sha256=target_revision.artifact_sha256,
        candidate_artifact_id=target_revision.artifact_id,
    )
    if replay is not None:
        replay_result, replay_change = replay
        return replay_result, replay_change, True

    lease: WriterLease | None = None
    result: CommitResult | None = None
    committed_change: ChangeSet | None = None
    try:
        holder_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24]
        lease = await service.acquire_writer_lease(
            document_id=document.document_id,
            holder_id=f"artifact-user:{actor.actor_id[:128]}:{holder_digest}",
            ttl_ms=60_000,
            actor=actor,
        )
        locked_document = await service.get_document(document.document_id)
        if (
            locked_document.head_revision_id != expected_head_revision_id
            or locked_document.state_revision != expected_state_revision
        ):
            raise ArtifactConflictError("document changed before the revision-copy lease")
        result, committed_change = await service.commit_change_set_atomically(
            document_id=document.document_id,
            base_revision_id=expected_head_revision_id,
            expected_document_state_revision=expected_state_revision,
            operations=operations,
            candidate_artifact=target_revision.artifact,
            validation={
                "target_revision_id": target_revision.revision_id,
                "target_sha256": target_revision.artifact_sha256,
                "status": "passed",
            },
            actor=actor,
            turn_id=turn_id,
            summary=summary,
            source=source,
            copied_from_revision_id=target_revision.revision_id,
            revision_event_type=revision_event_type,
            lease=lease,
            require_lease=True,
        )
    except BaseException as exc:
        try:
            replay = await _applied_mutation_replay(
                service,
                document_id=document.document_id,
                turn_id=turn_id,
                base_revision_id=expected_head_revision_id,
                operations=operations,
                candidate_sha256=target_revision.artifact_sha256,
                candidate_artifact_id=target_revision.artifact_id,
            )
        except ArtifactConflictError:
            replay = None
        if replay is not None:
            result, committed_change = replay
        if result is None:
            if isinstance(exc, ArtifactConflictError):
                raise
            raise
        if not isinstance(exc, Exception):
            raise
    finally:
        if lease is not None:
            try:
                await service.release_writer_lease(lease=lease, actor=actor)
            except Exception:  # noqa: BLE001 - the bounded lease expires if release fails
                pass
    assert result is not None and committed_change is not None
    return result, committed_change, False


@_d.method("artifacts.edit.capabilities", scope="operator.read")
async def _handle_artifact_capabilities(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    formats = {name: _capabilities(name) for name in ("docx", "xlsx", "pptx", "html")}
    document_id = _optional_string(params, "documentId")
    if document_id is None:
        return {"formats": formats, "desktopFirst": True}
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    head = await service.get_revision(document.head_revision_id)
    artifact_format = _format_for(head.filename, head.media_type, document.kind)
    return {
        "documentId": document.document_id,
        "format": artifact_format,
        "capabilities": await _revision_capabilities(ctx, document, head),
    }


@_d.method("artifacts.documents.open", scope="operator.write")
async def _handle_document_open(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    artifact_id = _require_string(params, "artifactId")
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        ref, _ = await asyncio.to_thread(
            store.resolve_for_download,
            artifact_id,
            session_id=session_id,
        )
    except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
        raise _not_found("Artifact", artifact_id) from None

    try:
        result, adopted = await service.adopt_document(
            session_key=session_key,
            session_id=session_id,
            name=ref.name,
            kind=_kind_for(ref),
            initial_artifact=ArtifactBlobRef(
                artifact_id=ref.id,
                sha256=ref.sha256,
                filename=ref.name,
                media_type=ref.mime,
                byte_size=ref.size,
            ),
            actor=_actor(ctx),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="document.open",
        ) from exc
    if adopted:
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=result.document.document_id,
            revision_id=result.revision.revision_id,
            action="document.opened",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "adopted": adopted,
    }


@_d.method("artifacts.documents.list", scope="operator.read")
async def _handle_documents_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    documents = await service.list_documents(
        session_key=session_key,
        session_id=session_id,
        limit=_bounded_limit(params),
    )
    return {
        "documents": [await _document_with_head(ctx, service, item) for item in documents]
    }


@_d.method("artifacts.documents.get", scope="operator.read")
async def _handle_document_get(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    return {"document": await _document_with_head(ctx, service, document)}


@_d.method("artifacts.documents.rename", scope="operator.write")
async def _handle_document_rename(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId")
    await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    try:
        document = await service.rename_document(
            document_id=document_id,
            expected_state_revision=_require_positive_int(params, "expectedStateRevision"),
            name=_require_string(params, "name"),
            actor=_actor(ctx),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="document.rename",
        ) from exc
    await _emit_artifact_state(
        ctx,
        session_key=session_key,
        service=service,
        document_id=document_id,
        revision_id=document.head_revision_id,
        action="document.renamed",
    )
    return {"document": await _document_with_head(ctx, service, document)}


@_d.method("artifacts.documents.close", scope="operator.write")
async def _handle_document_close(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    return {
        "document": await _document_with_head(ctx, service, document),
        "closed": True,
    }


def _new_edit_session_id(
    *,
    session_id: str,
    actor: Actor,
    client_request_id: str | None,
) -> str:
    if client_request_id is None:
        return f"edit_{secrets.token_urlsafe(24)}"
    if len(client_request_id) > 256:
        raise ValueError("params.clientRequestId is too long")
    digest = hashlib.sha256(
        "\0".join(
            (
                "documents.editSessions.start.v1",
                session_id,
                actor.kind.value,
                actor.actor_id,
                client_request_id,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"edit_{digest}"


@_d.method("documents.editSessions.start", scope="operator.write")
async def _handle_edit_session_start(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId")
    await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    mode = params.get("mode", "edit") if isinstance(params, dict) else "edit"
    if mode != "edit":
        raise ValueError("params.mode must be edit")
    actor = _actor(ctx)
    edit_session_id = _new_edit_session_id(
        session_id=session_id,
        actor=actor,
        client_request_id=_optional_string(params, "clientRequestId"),
    )
    try:
        edit_session = await service.start_edit_session(
            document_id=document_id,
            user_id=actor.actor_id,
            ttl_ms=_EDIT_SESSION_TTL_MS,
            actor=actor,
            edit_session_id=edit_session_id,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.WRITE_BUSY,
            operation="edit_session.start",
        ) from exc
    return {"editSession": _edit_session_payload(edit_session)}


@_d.method("documents.editSessions.heartbeat", scope="operator.write")
async def _handle_edit_session_heartbeat(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    edit_session_id = _require_string(params, "editSessionId")
    actor = _actor(ctx)
    await _scoped_edit_session(
        service,
        edit_session_id=edit_session_id,
        session_key=session_key,
        session_id=session_id,
        actor=actor,
    )
    try:
        edit_session = await service.heartbeat_edit_session(
            edit_session_id=edit_session_id,
            user_id=actor.actor_id,
            expected_state_revision=_require_positive_int(
                params,
                "expectedStateRevision",
            ),
            ttl_ms=_EDIT_SESSION_TTL_MS,
            actor=actor,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED,
            operation="edit_session.heartbeat",
        ) from exc
    return {"editSession": _edit_session_payload(edit_session)}


@_d.method("documents.editSessions.close", scope="operator.write")
async def _handle_edit_session_close(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    edit_session_id = _require_string(params, "editSessionId")
    actor = _actor(ctx)
    await _scoped_edit_session(
        service,
        edit_session_id=edit_session_id,
        session_key=session_key,
        session_id=session_id,
        actor=actor,
    )
    try:
        edit_session = await service.close_edit_session(
            edit_session_id=edit_session_id,
            user_id=actor.actor_id,
            expected_state_revision=_require_positive_int(
                params,
                "expectedStateRevision",
            ),
            actor=actor,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED,
            operation="edit_session.close",
        ) from exc
    return {"editSession": _edit_session_payload(edit_session)}


@_d.method("artifacts.revisions.list", scope="operator.read")
async def _handle_revisions_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    revisions = await service.list_revisions(
        document.document_id,
        limit=_bounded_limit(params),
    )
    return {"revisions": [_revision_payload(item) for item in revisions]}


@_d.method("artifacts.revisions.restore", scope="operator.write")
async def _handle_revision_restore(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId")
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    target_id = _require_string(params, "revisionId")
    target_revision = await _scoped_revision(
        service,
        document=document,
        revision_id=target_id,
    )
    expected_head = _require_string(params, "expectedHeadRevisionId")
    expected_state_revision = _require_positive_int(params, "expectedStateRevision")
    request_id = _manual_mutation_request_id(params)
    turn_id = f"revision-restore:{request_id}"
    operations: tuple[dict[str, Any], ...] = (
        {
            "op": "restore_revision",
            "target_revision_id": target_id,
            "target_sha256": target_revision.artifact_sha256,
            "expected_document_state_revision": expected_state_revision,
        },
    )
    try:
        result, mutation_change, replayed = await _commit_revision_copy_mutation(
            service,
            document=document,
            target_revision=target_revision,
            expected_head_revision_id=expected_head,
            expected_state_revision=expected_state_revision,
            actor=_actor(ctx),
            turn_id=turn_id,
            operations=operations,
            summary="Restore document revision",
            source=RevisionSource.RESTORE,
            revision_event_type="document.restored",
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="revision.restore",
        ) from exc
    if not replayed:
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=document_id,
            revision_id=result.revision.revision_id,
            change_set_id=mutation_change.change_set_id,
            action="revision.restored",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "revision": _revision_payload(result.revision),
        "changeSet": _change_set_payload(mutation_change),
        "receipt": _mutation_receipt_payload(
            request_id=request_id,
            base_revision_id=expected_head,
            result=result,
            change_set=mutation_change,
        ),
    }


@_d.method("artifacts.changes.list", scope="operator.read")
async def _handle_changes_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    changes = await service.list_change_sets(
        document.document_id,
        limit=_bounded_limit(params),
    )
    return {"changeSets": [_change_set_payload(item) for item in changes]}


@_d.method("artifacts.changes.get", scope="operator.read")
async def _handle_change_get(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    change_id = _require_string(params, "changeSetId")
    try:
        change_set = await service.get_change_set(change_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("ChangeSet", change_id) from None
    if change_set.document_id != document.document_id:
        raise _not_found("ChangeSet", change_id)
    return {"changeSet": _change_set_payload(change_set)}


@_d.method("artifacts.changes.revert", scope="operator.write")
async def _handle_change_revert(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId")
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    change_id = _require_string(params, "changeSetId")
    try:
        change_set = await service.get_change_set(change_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("ChangeSet", change_id) from None
    if change_set.document_id != document.document_id:
        raise _not_found("ChangeSet", change_id)
    if change_set.applied_revision_id is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.MUTATION_NOT_APPLIED,
            reason_code="change_not_applied",
        )
    target_revision = await _scoped_revision(
        service,
        document=document,
        revision_id=change_set.base_revision_id,
    )
    expected_head = _require_string(params, "expectedHeadRevisionId")
    expected_state_revision = _require_positive_int(params, "expectedStateRevision")
    request_id = _manual_mutation_request_id(params)
    turn_id = f"change-revert:{request_id}"
    operations: tuple[dict[str, Any], ...] = (
        {
            "op": "revert_change_set",
            "reverted_change_set_id": change_id,
            "target_revision_id": target_revision.revision_id,
            "target_sha256": target_revision.artifact_sha256,
            "expected_document_state_revision": expected_state_revision,
        },
    )
    try:
        replay = await _applied_mutation_replay(
            service,
            document_id=document_id,
            turn_id=turn_id,
            base_revision_id=expected_head,
            operations=operations,
            candidate_sha256=target_revision.artifact_sha256,
            candidate_artifact_id=target_revision.artifact_id,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="change.revert_replay",
        ) from exc
    if replay is not None:
        result, mutation_change = replay
        replayed = True
    else:
        if document.head_revision_id != change_set.applied_revision_id:
            raise artifact_product_error(
                ArtifactProductErrorCode.DOCUMENT_CHANGED,
                reason_code="change_not_current",
            )
        try:
            result, mutation_change, replayed = await _commit_revision_copy_mutation(
                service,
                document=document,
                target_revision=target_revision,
                expected_head_revision_id=expected_head,
                expected_state_revision=expected_state_revision,
                actor=_actor(ctx),
                turn_id=turn_id,
                operations=operations,
                summary="Revert applied document change",
                source=RevisionSource.REVERT,
                revision_event_type="document.reverted",
            )
        except ArtifactConflictError as exc:
            raise _conflict(exc) from exc
    if not replayed:
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=document_id,
            revision_id=result.revision.revision_id,
            change_set_id=mutation_change.change_set_id,
            action="change.reverted",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "revision": _revision_payload(result.revision),
        # Preserve the v1 field: callers asked to revert this applied change.
        "changeSet": _change_set_payload(change_set),
        "mutationChangeSet": _change_set_payload(mutation_change),
        "receipt": _mutation_receipt_payload(
            request_id=request_id,
            base_revision_id=expected_head,
            result=result,
            change_set=mutation_change,
        ),
    }


def _annotation_selection(
    params: dict[str, Any] | None,
) -> tuple[str, str, str, str | None, str]:
    selection = params.get("selection") if isinstance(params, dict) else None
    if not isinstance(selection, dict):
        raise ValueError("params.selection is required")
    selection_id = selection.get("selectionId")
    tag_name = selection.get("tagName")
    element_path = selection.get("elementPath")
    dom_sha256 = selection.get("domSha256")
    element_proof_sha256 = selection.get("elementProofSha256")
    if (
        not isinstance(selection_id, str)
        or not _OPAQUE_ANNOTATION_ID_RE.fullmatch(selection_id)
        or not isinstance(tag_name, str)
        or not _HTML_TAG_NAME_RE.fullmatch(tag_name)
        or not isinstance(element_path, str)
        or not 1 <= len(element_path) <= 4096
        or (
            dom_sha256 is not None
            and (not isinstance(dom_sha256, str) or not _SHA256_RE.fullmatch(dom_sha256))
        )
        or not isinstance(element_proof_sha256, str)
        or not _SHA256_RE.fullmatch(element_proof_sha256)
    ):
        raise ValueError("params.selection is invalid")
    _parse_element_path(element_path)
    return (
        selection_id,
        tag_name.lower(),
        element_path,
        dom_sha256,
        element_proof_sha256,
    )


async def _trusted_annotation_selection(
    *,
    session_key: str,
    active_preview_artifact_id: str,
    selection_id: str,
    tag_name: str,
    element_path: str,
    dom_sha256: str | None,
    element_proof_sha256: str,
) -> None:
    bridge = get_desktop_artifact_bridge_client()
    if bridge is None or not hasattr(bridge, "resolve_annotation_selection"):
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            reason_code="preview_unavailable",
        )
    try:
        resolved = await bridge.resolve_annotation_selection(
            active_preview_artifact_id=active_preview_artifact_id,
            selection_id=selection_id,
            tag_name=tag_name,
            element_path=element_path,
            dom_sha256=dom_sha256,
            element_proof_sha256=element_proof_sha256,
            deadline_ms=2_000,
        )
    except (DesktopArtifactBridgeError, ValueError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            exc,
            operation="prompt_annotations.resolve_native_selection",
            reason_code="selection_changed",
        ) from exc
    if (
        getattr(resolved, "selection_id", None) != selection_id
        or getattr(resolved, "tag_name", None) != tag_name
        or getattr(resolved, "element_path", None) != element_path
        or getattr(resolved, "dom_sha256", None) != dom_sha256
        or getattr(resolved, "element_proof_sha256", None) != element_proof_sha256
        or getattr(resolved, "scope_id", None) != session_key
        or getattr(resolved, "active_preview_artifact_id", None)
        != active_preview_artifact_id
    ):
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            reason_code="preview_changed",
        )


async def _scoped_prompt_annotation(
    service: ArtifactSessionService,
    *,
    annotation_id: str,
    session_key: str,
    session_id: str,
    session_epoch: int,
) -> PromptAnnotation:
    try:
        annotation = await service.get_prompt_annotation(annotation_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("PromptAnnotation", annotation_id) from None
    if (
        annotation.session_key != session_key
        or annotation.session_id != session_id
        or annotation.session_epoch != session_epoch
    ):
        raise _not_found("PromptAnnotation", annotation_id)
    return annotation


async def _idempotent_prompt_annotation_create(
    service: ArtifactSessionService,
    *,
    annotation_id: str,
    session_key: str,
    session_id: str,
    session_epoch: int,
    document_id: str,
    revision_id: str,
    body: str,
    tag_name: str,
    element_path: str,
    element_proof_sha256: str,
) -> tuple[PromptAnnotation, Anchor] | None:
    """Recover a committed create without consuming another native selection.

    A create response can be lost after SQLite commits. The renderer retries
    with the same client-owned annotation ID, but the one-shot native candidate
    has already been consumed. Only an exact match to the already validated
    persisted anchor is eligible for this response-only replay path.
    """

    try:
        annotation = await service.get_prompt_annotation(annotation_id)
    except ArtifactSessionNotFoundError:
        return None
    if (
        annotation.session_key != session_key
        or annotation.session_id != session_id
        or annotation.session_epoch != session_epoch
    ):
        raise _not_found("PromptAnnotation", annotation_id)
    anchor = await _prompt_annotation_anchor(service, annotation)
    context = anchor.context or {}
    locator_tag_name = anchor.locator.get("tag_name")
    if (
        annotation.status is not PromptAnnotationStatus.DRAFT
        or annotation.document_id != document_id
        or annotation.revision_id != revision_id
        or annotation.body != body
        or anchor.kind is not AnchorKind.DOM_SOURCE
        or anchor.state is not AnchorState.RESOLVED
        or anchor.document_id != document_id
        or anchor.revision_id != revision_id
        or locator_tag_name != tag_name
        or context.get("element_path") != element_path
        or context.get("element_proof_sha256") != element_proof_sha256
    ):
        raise artifact_product_error(ArtifactProductErrorCode.ANNOTATION_BUSY)
    return annotation, anchor


@_d.method("artifacts.prompt_annotations.list", scope="operator.read")
async def _handle_prompt_annotations_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    document_id = _optional_string(params, "documentId")
    if document_id is not None:
        await _scoped_document(
            service,
            document_id=document_id,
            session_key=session_key,
            session_id=session_id,
        )
    raw_status = _optional_string(params, "status")
    try:
        status = (
            PromptAnnotationStatus.DRAFT
            if raw_status is None
            else PromptAnnotationStatus(raw_status)
        )
    except ValueError as exc:
        raise ValueError("params.status is unsupported") from exc
    annotations = await service.list_prompt_annotations(
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
        status=status,
        document_id=document_id,
        limit=_bounded_limit(params, default=500),
    )
    documents: dict[str, Document] = {}
    payloads: list[dict[str, Any]] = []
    for annotation in annotations:
        document = documents.get(annotation.document_id)
        if document is None:
            document = await _scoped_document(
                service,
                document_id=annotation.document_id,
                session_key=session_key,
                session_id=session_id,
            )
            documents[annotation.document_id] = document
        payloads.append(
            _prompt_annotation_payload(
                annotation,
                anchor=await _prompt_annotation_anchor(service, annotation),
                current_head_revision_id=document.head_revision_id,
            )
        )
    return {"annotations": payloads}


@_d.method("artifacts.prompt_annotations.create", scope="operator.write")
async def _handle_prompt_annotation_create(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    annotation_id = _require_string(params, "annotationId")
    if not _OPAQUE_ANNOTATION_ID_RE.fullmatch(annotation_id):
        raise ValueError("params.annotationId is invalid")
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    revision_id = _optional_string(params, "revisionId") or document.head_revision_id
    if revision_id != document.head_revision_id:
        raise artifact_product_error(ArtifactProductErrorCode.DOCUMENT_CHANGED)
    revision = await _scoped_revision(service, document=document, revision_id=revision_id)
    (
        selection_id,
        tag_name,
        element_path,
        dom_sha256,
        element_proof_sha256,
    ) = _annotation_selection(params)
    body = _prompt_annotation_body(params)
    replayed = await _idempotent_prompt_annotation_create(
        service,
        annotation_id=annotation_id,
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
        document_id=document.document_id,
        revision_id=revision_id,
        body=body,
        tag_name=tag_name,
        element_path=element_path,
        element_proof_sha256=element_proof_sha256,
    )
    if replayed is not None:
        annotation, anchor = replayed
        return {
            "annotation": _prompt_annotation_payload(
                annotation,
                anchor=anchor,
                current_head_revision_id=document.head_revision_id,
            )
        }
    capabilities = await _revision_capabilities(ctx, document, revision)
    if not capabilities.get("promptAnnotations"):
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="annotation_unsupported",
        )
    await _trusted_annotation_selection(
        session_key=session_key,
        active_preview_artifact_id=revision.artifact_id,
        selection_id=selection_id,
        tag_name=tag_name,
        element_path=element_path,
        dom_sha256=dom_sha256,
        element_proof_sha256=element_proof_sha256,
    )
    _resolved, _ref, _path, source = await _resolve_source_revision(
        ctx=ctx,
        service=service,
        session_id=session_id,
        document=document,
        revision_id=revision_id,
    )
    locator, opening_tag, anchor_context = _canonical_opening_anchor(
        source,
        element_path=element_path,
        expected_element_proof_sha256=element_proof_sha256,
        expected_tag_name=tag_name,
    )

    try:
        anchor, annotation = await service.create_prompt_annotation_with_anchor(
            annotation_id=annotation_id,
            session_key=session_key,
            session_id=session_id,
            session_epoch=session_epoch,
            document_id=document.document_id,
            revision_id=revision_id,
            kind=AnchorKind.DOM_SOURCE,
            locator=locator,
            quote=opening_tag[:2048],
            context=anchor_context,
            actor=_actor(ctx),
            body=body,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.ANNOTATION_BUSY,
            operation="prompt_annotations.create",
        ) from exc
    except ArtifactSessionNotFoundError:
        raise _not_found("PromptAnnotation", annotation_id) from None
    return {
        "annotation": _prompt_annotation_payload(
            annotation,
            anchor=anchor,
            current_head_revision_id=document.head_revision_id,
        )
    }


@_d.method("artifacts.prompt_annotations.focus", scope="operator.write")
async def _handle_prompt_annotation_focus(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    annotation_id = _require_string(params, "annotationId")
    if not _OPAQUE_ANNOTATION_ID_RE.fullmatch(annotation_id):
        raise ValueError("params.annotationId is invalid")
    annotation = await _scoped_prompt_annotation(
        service,
        annotation_id=annotation_id,
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
    )
    if annotation.status is not PromptAnnotationStatus.DRAFT:
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            reason_code="not_draft",
        )
    document = await _scoped_document(
        service,
        document_id=annotation.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    revision = await _scoped_revision(
        service,
        document=document,
        revision_id=document.head_revision_id,
    )
    bridge = get_desktop_artifact_bridge_client()
    if bridge is None or not hasattr(bridge, "focus_annotation"):
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            reason_code="preview_unavailable",
        )
    if not (await _revision_capabilities(ctx, document, revision)).get("promptAnnotations"):
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="annotation_unsupported",
        )
    anchor = await _prompt_annotation_anchor(service, annotation)
    if anchor.kind is not AnchorKind.DOM_SOURCE:
        raise artifact_product_error(ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE)
    try:
        old_revision = await _scoped_revision(
            service,
            document=document,
            revision_id=annotation.revision_id,
        )
        _old_resolved, _old_ref, _old_path, old_source = await _resolve_source_revision(
            ctx=ctx,
            service=service,
            session_id=session_id,
            document=document,
            revision_id=old_revision.revision_id,
        )
        _current_resolved, _current_ref, _current_path, current_source = (
            await _resolve_source_revision(
                ctx=ctx,
                service=service,
                session_id=session_id,
                document=document,
                revision_id=revision.revision_id,
            )
        )
        resolution = remap_html_anchor(
            old_source=old_source,
            current_source=current_source,
            anchor=anchor,
        )
    except ValueError as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            exc,
            operation="prompt_annotations.focus_remap",
            retryable=False,
            annotation_id=annotation.annotation_id,
        ) from exc
    if resolution.status != "ready":
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            reason_code=resolution.reason,
        )
    verified_context = resolution.context
    verified_locator = resolution.locator
    element_path = verified_context.get("element_path")
    element_proof_sha256 = verified_context.get("element_proof_sha256")
    tag_name = verified_locator.get("tag_name")
    if (
        not isinstance(element_path, str)
        or not isinstance(element_proof_sha256, str)
        or not _SHA256_RE.fullmatch(element_proof_sha256)
        or not isinstance(tag_name, str)
        or not _HTML_TAG_NAME_RE.fullmatch(tag_name)
    ):
        raise artifact_product_error(ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE)
    try:
        focused = await bridge.focus_annotation(
            annotation_id=annotation.annotation_id,
            scope_id=session_key,
            active_preview_artifact_id=revision.artifact_id,
            tag_name=tag_name.lower(),
            element_path=element_path,
            element_proof_sha256=element_proof_sha256,
            deadline_ms=2_000,
        )
    except (DesktopArtifactBridgeError, ValueError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            exc,
            operation="prompt_annotations.focus_native",
            retryable=True,
            reason_code="preview_unavailable",
            annotation_id=annotation.annotation_id,
        ) from exc
    if focused is not True:
        raise artifact_product_error(
            ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
            retryable=True,
            reason_code="preview_unavailable",
        )
    return {
        "focused": True,
        "annotationId": annotation.annotation_id,
        "documentId": annotation.document_id,
    }


@_d.method("artifacts.prompt_annotations.update", scope="operator.write")
async def _handle_prompt_annotation_update(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    annotation = await _scoped_prompt_annotation(
        service,
        annotation_id=_require_string(params, "annotationId"),
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
    )
    document = await _scoped_document(
        service,
        document_id=annotation.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    try:
        annotation = await service.update_prompt_annotation(
            annotation_id=annotation.annotation_id,
            expected_state_revision=_require_positive_int(params, "expectedStateRevision"),
            body=_prompt_annotation_body(params),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.ANNOTATION_BUSY,
            operation="prompt_annotations.update",
        ) from exc
    return {
        "annotation": _prompt_annotation_payload(
            annotation,
            anchor=await _prompt_annotation_anchor(service, annotation),
            current_head_revision_id=document.head_revision_id,
        )
    }


@_d.method("artifacts.prompt_annotations.discard", scope="operator.write")
async def _handle_prompt_annotation_discard(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    annotation = await _scoped_prompt_annotation(
        service,
        annotation_id=_require_string(params, "annotationId"),
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
    )
    document = await _scoped_document(
        service,
        document_id=annotation.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    try:
        annotation = await service.discard_prompt_annotation(
            annotation_id=annotation.annotation_id,
            expected_state_revision=_require_positive_int(params, "expectedStateRevision"),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.ANNOTATION_BUSY,
            operation="prompt_annotations.discard",
        ) from exc
    return {
        "annotation": _prompt_annotation_payload(
            annotation,
            anchor=await _prompt_annotation_anchor(service, annotation),
            current_head_revision_id=document.head_revision_id,
        )
    }


def _html_source(ref: ArtifactRef, path: Path) -> str:
    if _format_for(ref.name, ref.mime) != "html":
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="format_unsupported",
        )
    payload = path.read_bytes()
    if len(payload) > DEFAULT_ARTIFACT_MAX_BYTES:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="size_unsupported",
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="encoding_unsupported",
        ) from exc


async def _resolve_source_revision(
    *,
    ctx: RpcContext,
    service: ArtifactSessionService,
    session_id: str,
    document: Document,
    revision_id: str,
) -> tuple[Revision, ArtifactRef, Path, str]:
    revision = await _scoped_revision(
        service,
        document=document,
        revision_id=revision_id,
    )
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        supports_editing = await asyncio.to_thread(
            store.supports_single_file_editing,
            revision.artifact_id,
            session_id=session_id,
        )
    except ArtifactNotFoundError:
        raise _not_found("Revision", revision_id) from None
    except (ArtifactIntegrityError, OSError, ValueError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            exc,
            operation="artifact.source.supports_editing",
            retryable=True,
        ) from exc
    if not supports_editing:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="bundle_unsupported",
        )
    try:
        ref, path = await asyncio.to_thread(
            store.resolve_for_download,
            revision.artifact_id,
            session_id=session_id,
        )
    except ArtifactNotFoundError:
        raise _not_found("Revision", revision_id) from None
    except ArtifactIntegrityError as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            exc,
            operation="artifact.source.resolve",
            retryable=True,
        ) from exc
    source = await asyncio.to_thread(_html_source, ref, path)
    return revision, ref, path, source


@_d.method("artifacts.source.read", scope="operator.read")
async def _handle_source_read(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document = await _scoped_document(
        service,
        document_id=_require_string(params, "documentId"),
        session_key=session_key,
        session_id=session_id,
    )
    revision_id = _optional_string(params, "revisionId") or document.head_revision_id
    revision, _ref, _path, source = await _resolve_source_revision(
        ctx=ctx,
        service=service,
        session_id=session_id,
        document=document,
        revision_id=revision_id,
    )
    canonical = get_document_format_adapter("html").read(source, view="source")
    if not isinstance(canonical, str):
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="source_view_unavailable",
        )
    return {
        "source": {
            "documentId": document.document_id,
            "revisionId": revision.revision_id,
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "text": canonical,
            "language": "html",
            "offsetEncoding": _SOURCE_OFFSET_ENCODING,
            "stateRevision": document.state_revision,
        }
    }


def _apply_source_patches(
    source: str,
    raw_patches: object,
) -> tuple[str, tuple[dict[str, object], ...], dict[str, object]]:
    if not isinstance(raw_patches, list) or not raw_patches:
        raise ValueError("params.patches must be a non-empty array")
    if len(raw_patches) > _MAX_SOURCE_PATCHES:
        raise ValueError("params.patches contains too many edits")
    patches: list[tuple[int, int, str]] = []
    for item in raw_patches:
        if not isinstance(item, dict):
            raise ValueError("each source patch must be an object")
        start = item.get("startOffset")
        end = item.get("endOffset")
        replacement = item.get("replacement")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not isinstance(replacement, str)
        ):
            raise ValueError("source patch offsets and replacement are invalid")
        if start < 0 or end < start or end > len(source):
            raise ValueError("source patch range is out of bounds")
        patches.append((start, end, replacement))
    patches.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(patches, patches[1:], strict=False):
        if current[0] < previous[1] or current[0] == previous[0]:
            raise ValueError("source patches must not overlap or share a start offset")
    result = source
    for start, end, replacement in reversed(patches):
        result = result[:start] + replacement + result[end:]
    adapter_validation = validate_editable_html_source(result)
    audit_patches = tuple(
        {
            "start_offset": start,
            "end_offset": end,
            "expected_chars": end - start,
            "expected_sha256": hashlib.sha256(source[start:end].encode("utf-8")).hexdigest(),
            "replacement_chars": len(replacement),
            "replacement_sha256": hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
        }
        for start, end, replacement in patches
    )
    return result, audit_patches, adapter_validation


def _manual_mutation_request_id(params: dict[str, Any] | None) -> str:
    request_id = _optional_string(params, "clientRequestId")
    if request_id is not None:
        if len(request_id) > 256:
            raise ValueError("params.clientRequestId is too long")
        return request_id
    # Older clients do not send an idempotency key. Hash their validated JSON
    # request so a transport-loss retry still resolves the same durable receipt
    # without persisting source text or replacement content in the turn id.
    if not isinstance(params, dict):
        raise ValueError("params are required")
    canonical = json.dumps(
        params,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"legacy-{hashlib.sha256(canonical).hexdigest()}"


def _mutation_receipt_payload(
    *,
    request_id: str,
    base_revision_id: str,
    result: CommitResult,
    change_set: ChangeSet,
) -> dict[str, Any]:
    state_revision = _mutation_result_state_revision(result, change_set)
    return {
        "requestId": request_id,
        "documentId": result.document.document_id,
        "baseRevisionId": base_revision_id,
        "resultRevisionId": result.revision.revision_id,
        "changeSetId": change_set.change_set_id,
        "stateRevision": state_revision,
        "status": "applied",
    }


def _mutation_result_state_revision(
    result: CommitResult,
    change_set: ChangeSet,
) -> int:
    expected = {
        operation.get("expected_document_state_revision")
        for operation in change_set.operations
        if isinstance(operation.get("expected_document_state_revision"), int)
        and not isinstance(operation.get("expected_document_state_revision"), bool)
    }
    if len(expected) == 1:
        value = next(iter(expected))
        assert isinstance(value, int)
        if value > 0:
            return value + 1
    return result.document.state_revision


async def _applied_mutation_replay(
    service: ArtifactSessionService,
    *,
    document_id: str,
    turn_id: str,
    base_revision_id: str,
    operations: tuple[dict[str, Any], ...],
    candidate_sha256: str,
    candidate_artifact_id: str | None = None,
) -> tuple[CommitResult, ChangeSet] | None:
    change_set = await service.get_change_set_by_turn(
        document_id=document_id,
        turn_id=turn_id,
    )
    if change_set is None:
        return None
    if (
        change_set.base_revision_id != base_revision_id
        or change_set.operations != operations
        or change_set.candidate_artifact_sha256 != candidate_sha256
        or (
            candidate_artifact_id is not None
            and change_set.candidate_artifact_id != candidate_artifact_id
        )
    ):
        raise ArtifactConflictError(
            "clientRequestId was already used for a different document mutation"
        )
    if (
        change_set.status is not ChangeSetStatus.APPLIED
        or change_set.applied_revision_id is None
    ):
        raise ArtifactConflictError("document mutation receipt is not applied")
    document = await service.get_document(document_id)
    revision = await service.get_revision(change_set.applied_revision_id)
    if (
        revision.change_set_id != change_set.change_set_id
        or revision.artifact_sha256 != candidate_sha256
        or revision.artifact_id != change_set.candidate_artifact_id
    ):
        raise ArtifactConflictError("applied document mutation receipt is inconsistent")
    return CommitResult(document=document, revision=revision), change_set


async def _source_patch_response(
    *,
    ctx: RpcContext,
    service: ArtifactSessionService,
    request_id: str,
    base_revision_id: str,
    result: CommitResult,
    change_set: ChangeSet,
    patch_count: int,
    edit_session: EditSession | None = None,
) -> dict[str, Any]:
    payload = {
        "document": await _mutation_document_payload(ctx, service, result),
        "revision": _revision_payload(result.revision),
        "changeSet": _change_set_payload(change_set),
        "receipt": _mutation_receipt_payload(
            request_id=request_id,
            base_revision_id=base_revision_id,
            result=result,
            change_set=change_set,
        ),
        "source": {
            "documentId": result.document.document_id,
            "revisionId": result.revision.revision_id,
            "sha256": result.revision.artifact_sha256,
            "offsetEncoding": _SOURCE_OFFSET_ENCODING,
            "patchCount": patch_count,
            "stateRevision": _mutation_result_state_revision(result, change_set),
        },
    }
    if edit_session is not None:
        payload["editSession"] = _edit_session_payload(edit_session)
    return payload


@_d.method("artifacts.source.patch", scope="operator.write")
async def _handle_source_patch(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId")
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    actor = _actor(ctx)
    edit_session_id = _optional_string(params, "editSessionId")
    edit_session_state_revision: int | None = None
    edit_session_last_saved_revision_id: str | None = None
    edit_session: EditSession | None = None
    edit_session_keys = (
        "editSessionId",
        "expectedEditSessionStateRevision",
        "expectedLastSavedRevisionId",
    )
    if edit_session_id is None:
        if isinstance(params, dict) and any(name in params for name in edit_session_keys):
            raise ValueError(
                "editSessionId, expectedEditSessionStateRevision, and "
                "expectedLastSavedRevisionId must be supplied together"
            )
    else:
        edit_session_state_revision = _require_positive_int(
            params,
            "expectedEditSessionStateRevision",
        )
        edit_session_last_saved_revision_id = _require_string(
            params,
            "expectedLastSavedRevisionId",
        )
        edit_session, edit_document = await _scoped_edit_session(
            service,
            edit_session_id=edit_session_id,
            session_key=session_key,
            session_id=session_id,
            actor=actor,
        )
        if edit_document.document_id != document_id:
            raise _conflict(
                ArtifactConflictError("edit session belongs to another document"),
                code=ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED,
                operation="edit_session.validate_save",
            )
    expected_head = _require_string(params, "expectedHeadRevisionId")
    revision, ref, _path, source = await _resolve_source_revision(
        ctx=ctx,
        service=service,
        session_id=session_id,
        document=document,
        revision_id=expected_head,
    )
    expected_sha = _require_string(params, "expectedSourceSha256").lower()
    expected_state_revision = _require_positive_int(params, "expectedStateRevision")
    try:
        _validate_source_offset_encoding(params)
    except ValueError:
        raise artifact_product_error(
            ArtifactProductErrorCode.INVALID_REQUEST,
            reason_code="invalid_source_edit",
        ) from None
    actual_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if expected_sha != actual_sha:
        raise _conflict(ArtifactConflictError("source sha256 changed"))
    try:
        updated, audit_patches, adapter_validation = _apply_source_patches(
            source,
            params.get("patches") if isinstance(params, dict) else None,
        )
    except (DocumentAdapterError, ValueError):
        raise artifact_product_error(
            ArtifactProductErrorCode.INVALID_REQUEST,
            reason_code="invalid_source_edit",
        ) from None
    patch_count = len(audit_patches)
    request_id = _manual_mutation_request_id(params)
    turn_id = f"manual-source-patch:{request_id}"
    updated_bytes = updated.encode("utf-8")
    updated_sha256 = hashlib.sha256(updated_bytes).hexdigest()
    operation: dict[str, Any] = {
        "op": "html_source_patch",
        "origin": "manual",
        "expected_document_state_revision": expected_state_revision,
        "expected_source_sha256": actual_sha,
        "result_source_sha256": updated_sha256,
        "offset_encoding": _SOURCE_OFFSET_ENCODING,
        "patches": list(audit_patches),
    }
    if edit_session_id is not None:
        operation["edit_session"] = {
            "id": edit_session_id,
            "expected_state_revision": edit_session_state_revision,
            "expected_last_saved_revision_id": edit_session_last_saved_revision_id,
        }
    operations: tuple[dict[str, Any], ...] = (operation,)
    proposal_sha256 = hashlib.sha256(
        json.dumps(
            {
                "document_id": document_id,
                "base_revision_id": expected_head,
                "request_id": request_id,
                "candidate_sha256": updated_sha256,
                "operations": operations,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    try:
        replay = await _applied_mutation_replay(
            service,
            document_id=document_id,
            turn_id=turn_id,
            base_revision_id=expected_head,
            operations=operations,
            candidate_sha256=updated_sha256,
        )
    except ArtifactConflictError as exc:
        raise _conflict(exc) from exc
    if replay is not None:
        replay_result, replay_change = replay
        if edit_session_id is not None:
            edit_session, _ = await _scoped_edit_session(
                service,
                edit_session_id=edit_session_id,
                session_key=session_key,
                session_id=session_id,
                actor=actor,
            )
        return await _source_patch_response(
            ctx=ctx,
            service=service,
            request_id=request_id,
            base_revision_id=expected_head,
            result=replay_result,
            change_set=replay_change,
            patch_count=patch_count,
            edit_session=edit_session,
        )
    if (
        document.head_revision_id != expected_head
        or document.state_revision != expected_state_revision
    ):
        raise _conflict(ArtifactConflictError("document head or state revision changed"))
    if edit_session_id is not None:
        assert edit_session_state_revision is not None
        assert edit_session_last_saved_revision_id is not None
        try:
            edit_session = await service.validate_edit_session_for_save(
                edit_session_id=edit_session_id,
                document_id=document_id,
                user_id=actor.actor_id,
                expected_state_revision=edit_session_state_revision,
                expected_last_saved_revision_id=edit_session_last_saved_revision_id,
            )
        except ArtifactConflictError as exc:
            raise _conflict(
                exc,
                code=ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED,
                operation="edit_session.validate_save",
            ) from exc
    store = ArtifactStore(media_root_from_config(ctx.config))
    candidate: ArtifactRef | None = None
    candidate_id: str | None = None
    candidate_publish: asyncio.Task[ArtifactRef] | None = None
    result: CommitResult | None = None
    committed_change: ChangeSet | None = None
    lease: WriterLease | None = None
    release_lease = False
    updated_edit_session: EditSession | None = None
    attempt: MutationAttempt | None = None
    tool_use_id = f"rpc-source-patch:{hashlib.sha256(turn_id.encode()).hexdigest()[:32]}"
    try:
        attempt, created = await service.reserve_mutation_attempt_with_status(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            base_revision_id=expected_head,
            proposal_sha256=proposal_sha256,
        )
        if not created:
            raise ArtifactConflictError(
                f"document mutation request is already {attempt.status.value}"
            )
        holder_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24]
        lease = await service.acquire_writer_lease(
            document_id=document_id,
            holder_id=f"artifact-user:{actor.actor_id[:128]}:{holder_digest}",
            ttl_ms=_EDIT_SESSION_TTL_MS,
            actor=actor,
        )
        release_lease = True
        locked_document = await service.get_document(document_id)
        if (
            locked_document.head_revision_id != expected_head
            or locked_document.state_revision != expected_state_revision
        ):
            raise ArtifactConflictError("document changed before the manual write lease")
        candidate_id = store.allocate_artifact_id()
        await service.register_mutation_candidate(
            document_id=document_id,
            turn_id=turn_id,
            candidate_session_id=session_id,
            candidate_artifact_id=candidate_id,
            candidate_artifact_sha256=updated_sha256,
        )
        candidate_publish = asyncio.create_task(
            asyncio.to_thread(
                store.publish_bytes,
                updated_bytes,
                session_id=session_id,
                session_key=session_key,
                name=ref.name,
                mime=ref.mime,
                source="artifact_source_patch",
                visibility="internal",
                artifact_id=candidate_id,
            )
        )
        candidate = await asyncio.shield(candidate_publish)
        result, committed_change = await service.commit_change_set_atomically(
            document_id=document_id,
            base_revision_id=expected_head,
            expected_document_state_revision=expected_state_revision,
            operations=operations,
            candidate_artifact=ArtifactBlobRef(
                artifact_id=candidate.id,
                sha256=candidate.sha256,
                filename=candidate.name,
                media_type=candidate.mime,
                byte_size=candidate.size,
            ),
            validation={
                "format": "html",
                "encoding": "utf-8",
                "source_sha256": candidate.sha256,
                "patch_count": patch_count,
                "adapter_validation": adapter_validation,
                "status": "passed",
            },
            actor=actor,
            turn_id=turn_id,
            summary="Manual HTML source edit",
            source=RevisionSource.MANUAL,
            lease=lease,
            require_lease=True,
            edit_session_id=edit_session_id,
            expected_edit_session_state_revision=edit_session_state_revision,
            expected_last_saved_revision_id=edit_session_last_saved_revision_id,
        )
        if edit_session_id is not None:
            updated_edit_session = await service.get_edit_session(edit_session_id)
        await service.mark_mutation_attempt_applied(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            change_set_id=committed_change.change_set_id,
            revision_id=result.revision.revision_id,
        )
    except BaseException as exc:
        if candidate is None and candidate_publish is not None:
            try:
                candidate = await asyncio.shield(candidate_publish)
            except Exception:  # noqa: BLE001 - publication failed before an artifact existed
                pass
        durable_change: ChangeSet | None = None
        durable_state_known = False
        try:
            durable_change = await service.get_change_set_by_turn(
                document_id=document_id,
                turn_id=turn_id,
            )
            durable_state_known = True
        except Exception:  # noqa: BLE001 - ambiguous durable state must retain bytes
            pass

        if (
            result is None
            and candidate is not None
            and durable_change is not None
            and durable_change.status is ChangeSetStatus.APPLIED
            and durable_change.applied_revision_id is not None
            and durable_change.candidate_artifact_id == candidate.id
            and durable_change.candidate_artifact_sha256 == candidate.sha256
        ):
            try:
                durable_document = await service.get_document(document_id)
                durable_revision = await service.get_revision(
                    durable_change.applied_revision_id
                )
            except Exception:  # noqa: BLE001 - preserve bytes on ambiguous commit state
                pass
            else:
                if (
                    durable_document.head_revision_id == durable_revision.revision_id
                    and durable_revision.change_set_id == durable_change.change_set_id
                    and durable_revision.artifact_id == candidate.id
                    and durable_revision.artifact_sha256 == candidate.sha256
                ):
                    result = CommitResult(
                        document=durable_document,
                        revision=durable_revision,
                    )
                    committed_change = durable_change

        candidate_can_be_deleted = (
            result is None
            and durable_state_known
            and (
                durable_change is None
                or (
                    candidate_id is not None
                    and durable_change.candidate_artifact_id != candidate_id
                )
            )
        )
        cleanup_ambiguous = candidate_id is not None and not durable_state_known
        if candidate_can_be_deleted and candidate_id is not None:
            try:
                await asyncio.to_thread(
                    store.delete_reserved_bucket,
                    session_id=session_id,
                    artifact_id=candidate_id,
                )
            except (ArtifactError, OSError, ValueError):
                cleanup_ambiguous = True
        if result is None and attempt is not None:
            if cleanup_ambiguous:
                try:
                    await service.mark_mutation_attempt_ambiguous(
                        document_id=document_id,
                        turn_id=turn_id,
                        tool_use_id=tool_use_id,
                        failure_code="manual_candidate_cleanup_failed",
                    )
                except Exception:  # noqa: BLE001 - leave the reserved journal recoverable
                    pass
            else:
                try:
                    await service.mark_mutation_attempt_failed(
                        document_id=document_id,
                        turn_id=turn_id,
                        tool_use_id=tool_use_id,
                        failure_code="manual_mutation_failed",
                    )
                except Exception:  # noqa: BLE001 - restart recovery owns unresolved attempts
                    pass
        elif result is not None and committed_change is not None and attempt is not None:
            try:
                await service.mark_mutation_attempt_applied(
                    document_id=document_id,
                    turn_id=turn_id,
                    tool_use_id=tool_use_id,
                    change_set_id=committed_change.change_set_id,
                    revision_id=result.revision.revision_id,
                )
            except Exception:  # noqa: BLE001 - restart recovery can reconcile the receipt
                pass
        if result is None:
            if cleanup_ambiguous:
                raise artifact_product_error(
                    ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                    retryable=True,
                    reason_code="cleanup_pending",
                ) from None
            if isinstance(exc, ArtifactConflictError):
                raise _conflict(exc) from exc
            raise
        if not isinstance(exc, Exception):
            raise
    finally:
        if release_lease and lease is not None:
            try:
                await service.release_writer_lease(lease=lease, actor=actor)
            except Exception:  # noqa: BLE001 - the bounded lease expires if release fails
                pass
    assert result is not None
    assert committed_change is not None
    assert candidate is not None
    if edit_session_id is not None and updated_edit_session is None:
        updated_edit_session, _ = await _scoped_edit_session(
            service,
            edit_session_id=edit_session_id,
            session_key=session_key,
            session_id=session_id,
            actor=actor,
        )
    await _emit_artifact_state(
        ctx,
        session_key=session_key,
        service=service,
        document_id=document_id,
        revision_id=result.revision.revision_id,
        change_set_id=committed_change.change_set_id,
        action="source.patched",
    )
    return await _source_patch_response(
        ctx=ctx,
        service=service,
        request_id=request_id,
        base_revision_id=expected_head,
        result=result,
        change_set=committed_change,
        patch_count=patch_count,
        edit_session=updated_edit_session,
    )


__all__ = [
    "_handle_artifact_capabilities",
    "_handle_change_get",
    "_handle_change_revert",
    "_handle_changes_list",
    "_handle_document_close",
    "_handle_document_get",
    "_handle_document_open",
    "_handle_document_rename",
    "_handle_documents_list",
    "_handle_edit_session_close",
    "_handle_edit_session_heartbeat",
    "_handle_edit_session_start",
    "_handle_prompt_annotation_create",
    "_handle_prompt_annotation_discard",
    "_handle_prompt_annotation_focus",
    "_handle_prompt_annotation_update",
    "_handle_prompt_annotations_list",
    "_handle_revision_restore",
    "_handle_revisions_list",
    "_handle_source_patch",
    "_handle_source_read",
]
