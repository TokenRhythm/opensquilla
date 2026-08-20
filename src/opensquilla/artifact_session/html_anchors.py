"""Pure HTML anchor profiling, deterministic remapping, and candidate validation.

The functions in this module operate only on UTF-8 source text and persisted
anchor data.  They deliberately do not know about Gateway RPCs, Desktop/CDP
selection handles, artifact storage, or database transactions.  Creation,
send-time normalization, focus, and document tools can therefore share the
same conservative source identity rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

from lxml import etree  # type: ignore[import-untyped]
from lxml import html as lxml_html  # type: ignore[import-untyped]

from .models import Anchor, AnchorKind, AnchorState

SOURCE_OFFSET_ENCODING = "unicode-code-point"
MAX_SEMANTIC_PROFILE_BYTES = 4 * 1024
MAX_CONTEXT_BYTES = 8 * 1024
MAX_CANDIDATE_SOURCE_BYTES = 16 * 1024

_SAFE_ATTRIBUTES = (
    "id",
    "name",
    "role",
    "aria-label",
    "title",
    "alt",
    "data-testid",
    "data-test",
    "type",
)
_STRONG_IDENTITY_ATTRIBUTES = (
    "id",
    "data-testid",
    "data-test",
    "name",
    "aria-label",
)
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
UNSUPPORTED_CONTEXTUAL_ELEMENTS = frozenset(
    {"iframe", "noembed", "noframes", "plaintext", "script", "style", "template", "xmp"}
)
_TAG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9:-]{0,127}$")

TargetStatus = Literal["ready", "contextual"]
TargetReason = Literal["no_match", "ambiguous"]


def _normalized_text(value: str, *, limit: int = 256) -> str:
    return " ".join(value.split())[:limit]


def _bounded_value(value: str, *, limit: int = 160) -> str:
    return _normalized_text(value, limit=limit)


@dataclass(slots=True)
class _Element:
    tag_name: str
    start: int
    opening_end: int
    opening_text: str
    attributes: dict[str, str]
    boolean_attributes: tuple[str, ...]
    parent: int | None
    children: list[int]
    text_parts: list[str]


class _SourceTreeParser(HTMLParser):
    """Build a bounded source-backed tree without executing or fetching content."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source = source
        self._line_starts = [0]
        for index, character in enumerate(source):
            if character == "\n":
                self._line_starts.append(index + 1)
        self.elements: list[_Element] = []
        self._stack: list[int] = []
        self._ignored_depth = 0

    def _source_offset(self) -> int | None:
        line, column = self.getpos()
        if line < 1 or line > len(self._line_starts):
            return None
        return self._line_starts[line - 1] + column

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool) -> None:
        if len(self.elements) >= 50_000:
            raise ValueError("HTML contains too many elements")
        start = self._source_offset()
        opening = self.get_starttag_text()
        if start is None or not opening:
            return
        end = start + len(opening)
        if end > len(self._source) or self._source[start:end] != opening:
            raise ValueError("HTML opening tag is not source-backed")
        name = tag.lower()
        parent = self._stack[-1] if self._stack else None
        normalized_attrs: dict[str, str] = {}
        raw_attrs: dict[str, str] = {}
        boolean_attributes: list[str] = []
        for raw_name, raw_value in attrs:
            key = raw_name.lower()
            raw_attrs.setdefault(key, raw_value or "")
            if raw_value is None:
                boolean_attributes.append(key)
            if key in _SAFE_ATTRIBUTES and key not in normalized_attrs:
                normalized_attrs[key] = _bounded_value(raw_value or "")
        input_type = raw_attrs.get("type", "text").strip().lower()
        if name == "input" and input_type in {"button", "submit", "reset"}:
            normalized_attrs["value"] = _bounded_value(raw_attrs.get("value", ""))
        index = len(self.elements)
        self.elements.append(
            _Element(
                tag_name=name,
                start=start,
                opening_end=end,
                opening_text=opening,
                attributes=normalized_attrs,
                boolean_attributes=tuple(boolean_attributes),
                parent=parent,
                children=[],
                text_parts=[],
            )
        )
        if parent is not None:
            self.elements[parent].children.append(index)
        if name in {"script", "style", "template"}:
            self._ignored_depth += 1
        if push and name not in _VOID_ELEMENTS:
            self._stack.append(index)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, push=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"script", "style", "template"} and self._ignored_depth:
            self._ignored_depth -= 1
        for position in range(len(self._stack) - 1, -1, -1):
            if self.elements[self._stack[position]].tag_name == name:
                del self._stack[position:]
                return

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data.strip():
            return
        for index in self._stack[-16:]:
            parts = self.elements[index].text_parts
            if sum(len(part) for part in parts) < 512:
                parts.append(data[:512])


def _parse_elements(source: str) -> tuple[_Element, ...]:
    if not isinstance(source, str) or not source:
        raise ValueError("HTML source must not be empty")
    parser = _SourceTreeParser(source)
    try:
        parser.feed(source)
        parser.close()
    except (AssertionError, TypeError, ValueError) as exc:
        raise ValueError("HTML source could not be parsed safely") from exc
    return tuple(parser.elements)


class HtmlAnchorChangedError(ValueError):
    """The browser selection no longer identifies the immutable HTML source."""


_HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
_SVG_HTML_INTEGRATION_POINTS = frozenset({"desc", "foreignobject", "title"})
_HTML_HEAD_ELEMENTS = frozenset(
    {"base", "basefont", "bgsound", "link", "meta", "noframes", "script", "style", "title"}
)
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


def _normalize_browser_html_dom(root: etree._Element, *, source: str | None = None) -> None:
    """Reconcile conservative HTML5 implied nodes that lxml omits."""

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

    if source is None:
        return
    source_elements = _parse_elements(source)
    dom_elements = _dom_elements_preorder(root)
    cursor = 0
    for source_element in source_elements:
        matched: etree._Element | None = None
        while cursor < len(dom_elements):
            candidate = dom_elements[cursor]
            cursor += 1
            if _element_name(candidate)[1] == source_element.tag_name:
                matched = candidate
                break
        if matched is None:
            return
        for name in source_element.boolean_attributes:
            if name in matched.attrib:
                matched.attrib[name] = ""


def canonical_browser_dom_digest(root: etree._Element, *, source: str | None = None) -> str:
    """Return the bounded browser-compatible canonical DOM digest."""

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
    element_namespace, _tag_name = _element_name(element)
    attributes: list[list[str]] = []
    for raw_name, raw_value in element.attrib.items():
        if raw_name.startswith("{"):
            attr_namespace, attr_name = raw_name[1:].split("}", 1)
        else:
            attr_namespace, attr_name = "", raw_name
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


def canonical_element_proof_sha256(
    root: etree._Element,
    *,
    selected: etree._Element,
) -> str:
    """Hash selected element and source-backed ancestor identity."""

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


def parse_element_path(value: str) -> tuple[tuple[str, str, int], ...]:
    """Validate the canonical browser element path wire representation."""

    if not value or len(value) > 4_096 or "\x00" in value:
        raise ValueError("selection element path is invalid")
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("selection element path is invalid") from exc
    if not isinstance(raw, list) or not 1 <= len(raw) <= 128:
        raise ValueError("selection element path is invalid")
    result: list[tuple[str, str, int]] = []
    for segment in raw:
        if (
            not isinstance(segment, list)
            or len(segment) != 3
            or not isinstance(segment[0], str)
            or len(segment[0]) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in segment[0])
            or not isinstance(segment[1], str)
            or not _TAG_RE.fullmatch(segment[1])
            or isinstance(segment[2], bool)
            or not isinstance(segment[2], int)
            or not 1 <= segment[2] <= 9_007_199_254_740_991
        ):
            raise ValueError("selection element path is invalid")
        result.append((segment[0], segment[1].lower(), segment[2]))
    if json.dumps(raw, ensure_ascii=False, separators=(",", ":")) != value:
        raise ValueError("selection element path is invalid")
    return tuple(result)


def canonical_element_at_path(root: etree._Element, path: str) -> etree._Element:
    segments = parse_element_path(path)
    root_namespace, root_tag = _element_name(root)
    if segments[0] != (root_namespace, root_tag, 1):
        raise ValueError("selection element path does not match the canonical DOM")
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
            raise ValueError("selection element path does not match the canonical DOM")
        current = matched
    return current


def canonical_selection_proofs(source: str, *, element_path: str) -> tuple[str, str]:
    """Safely derive the canonical DOM and selected-element proof hashes."""

    parser = lxml_html.HTMLParser(recover=True, no_network=True)
    try:
        root = lxml_html.document_fromstring(source, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("The canonical HTML source cannot be parsed safely") from exc
    selected = canonical_element_at_path(root, element_path)
    dom_sha256 = canonical_browser_dom_digest(root, source=source)
    return dom_sha256, canonical_element_proof_sha256(root, selected=selected)


def _canonical_element_path(root: etree._Element, selected: etree._Element) -> str:
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
    return json.dumps(
        [
            [*_element_name(element), _element_nth_of_type(element)]
            for element in ancestors
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _dom_element_for_source_index(
    root: etree._Element,
    source_elements: tuple[_Element, ...],
    target_index: int,
) -> etree._Element:
    dom_elements = _dom_elements_preorder(root)
    cursor = 0
    for index, source_element in enumerate(source_elements):
        matched: etree._Element | None = None
        while cursor < len(dom_elements):
            candidate = dom_elements[cursor]
            cursor += 1
            if _element_name(candidate)[1] == source_element.tag_name:
                matched = candidate
                break
        if matched is None:
            raise ValueError("HTML source cannot be mapped uniquely to its canonical DOM")
        if index == target_index:
            return matched
    raise ValueError("The source locator does not identify a canonical DOM element")


def canonical_selection_context_for_locator(
    source: str,
    locator: dict[str, Any],
) -> dict[str, Any]:
    """Reconstruct browser path/proof metadata for one exact source locator."""

    source_elements = _parse_elements(source)
    source_index = _element_index_at_locator(source_elements, locator)
    if source_index is None:
        raise ValueError("HTML anchor locator is not uniquely source-backed")
    parser = lxml_html.HTMLParser(recover=True, no_network=True)
    try:
        root = lxml_html.document_fromstring(source, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("The canonical HTML source cannot be parsed safely") from exc
    _normalize_browser_html_dom(root, source=source)
    selected = _dom_element_for_source_index(root, source_elements, source_index)
    opening_tag = source_elements[source_index].opening_text
    return {
        "element_path": _canonical_element_path(root, selected),
        "element_proof_sha256": canonical_element_proof_sha256(root, selected=selected),
        "opening_tag_sha256": hashlib.sha256(opening_tag.encode("utf-8")).hexdigest(),
    }


def _opening_span_for_element(
    source: str,
    *,
    root: etree._Element,
    selected: etree._Element,
) -> tuple[int, int, str]:
    dom_elements = _dom_elements_preorder(root)
    source_elements = _parse_elements(source)
    cursor = 0
    selected_span: tuple[int, int, str] | None = None
    for source_element in source_elements:
        matched: etree._Element | None = None
        while cursor < len(dom_elements):
            candidate = dom_elements[cursor]
            cursor += 1
            if _element_name(candidate)[1] == source_element.tag_name:
                matched = candidate
                break
        if matched is None:
            raise ValueError("HTML source cannot be mapped uniquely to its canonical DOM")
        if matched is selected:
            if selected_span is not None:
                raise ValueError("HTML selection maps to more than one opening tag")
            selected_span = (
                source_element.start,
                source_element.opening_end,
                source_element.tag_name,
            )
    if selected_span is None:
        raise ValueError("The selected DOM element has no editable source opening tag")
    return selected_span


def canonical_opening_anchor(
    source: str,
    *,
    element_path: str,
    expected_element_proof_sha256: str,
    expected_tag_name: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Verify a browser selection and produce the shared source anchor shape."""

    parser = lxml_html.HTMLParser(recover=True, no_network=True)
    try:
        root = lxml_html.document_fromstring(source, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("The canonical HTML source cannot be parsed safely") from exc
    _normalize_browser_html_dom(root, source=source)
    try:
        selected = canonical_element_at_path(root, element_path)
    except ValueError as exc:
        raise HtmlAnchorChangedError("selection path no longer matches the source") from exc
    _namespace, selected_tag = _element_name(selected)
    if selected_tag != expected_tag_name.lower():
        raise HtmlAnchorChangedError("selection tag no longer matches the source")
    actual_element_proof_sha256 = canonical_element_proof_sha256(root, selected=selected)
    if actual_element_proof_sha256 != expected_element_proof_sha256:
        raise HtmlAnchorChangedError("selection proof no longer matches the source")
    start, start_tag_end, _opening_tag_name = _opening_span_for_element(
        source,
        root=root,
        selected=selected,
    )
    opening_tag = source[start:start_tag_end]
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    locator = {
        "start_offset": start,
        "start_tag_end_offset": start_tag_end,
        "tag_name": selected_tag,
        "source_sha256": source_sha256,
        "offset_encoding": SOURCE_OFFSET_ENCODING,
    }
    context = enrich_anchor_context(
        source,
        locator=locator,
        context=canonical_selection_context_for_locator(source, locator),
    )
    return locator, opening_tag, context


def _element_index_at_locator(
    elements: tuple[_Element, ...],
    locator: dict[str, Any],
) -> int | None:
    start = locator.get("start_offset")
    end = locator.get("start_tag_end_offset", locator.get("end_offset"))
    tag = locator.get("tag_name")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not isinstance(tag, str)
    ):
        return None
    matches = [
        index
        for index, element in enumerate(elements)
        if element.start == start
        and element.opening_end == end
        and element.tag_name == tag.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _profile_for_index(elements: tuple[_Element, ...], index: int) -> dict[str, Any]:
    element = elements[index]
    ancestors: list[str] = []
    parent = element.parent
    while parent is not None and len(ancestors) < 4:
        ancestors.append(elements[parent].tag_name)
        parent = elements[parent].parent
    ancestors.reverse()

    previous_hint: dict[str, str] | None = None
    next_hint: dict[str, str] | None = None
    parent_hint: dict[str, str] | None = None
    if element.parent is not None:
        parent_element = elements[element.parent]
        parent_hint = {
            "tag": parent_element.tag_name,
            "text": _normalized_text("".join(parent_element.text_parts), limit=120),
        }
        siblings = parent_element.children
        sibling_position = siblings.index(index)
        if sibling_position:
            sibling = elements[siblings[sibling_position - 1]]
            previous_hint = {
                "tag": sibling.tag_name,
                "text": _normalized_text("".join(sibling.text_parts), limit=80),
            }
        if sibling_position + 1 < len(siblings):
            sibling = elements[siblings[sibling_position + 1]]
            next_hint = {
                "tag": sibling.tag_name,
                "text": _normalized_text("".join(sibling.text_parts), limit=80),
            }

    profile: dict[str, Any] = {
        "version": 1,
        "tag_name": element.tag_name,
        "normalized_text": _normalized_text("".join(element.text_parts)),
        "safe_attributes": dict(element.attributes),
        "ancestor_tags": ancestors,
        "parent_hint": parent_hint,
        "previous_hint": previous_hint,
        "next_hint": next_hint,
    }
    encoded = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_SEMANTIC_PROFILE_BYTES:
        # Fixed field bounds normally keep this below the ceiling.  This final
        # projection makes the bound explicit even for unusual Unicode input.
        profile["normalized_text"] = _normalized_text(
            str(profile["normalized_text"]), limit=80
        )
        profile["parent_hint"] = None
        profile["previous_hint"] = None
        profile["next_hint"] = None
        encoded = json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if len(encoded) > MAX_SEMANTIC_PROFILE_BYTES:
        raise ValueError("HTML semantic profile exceeds its bound")
    return profile


def semantic_profile_for_locator(source: str, locator: dict[str, Any]) -> dict[str, Any]:
    """Build ``semantic_profile_v1`` for one verified source locator."""

    elements = _parse_elements(source)
    index = _element_index_at_locator(elements, locator)
    if index is None:
        raise ValueError("HTML anchor locator is not uniquely source-backed")
    return _profile_for_index(elements, index)


def enrich_anchor_context(
    source: str,
    *,
    locator: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Add a bounded semantic profile while preserving trusted proof fields."""

    enriched = dict(context or {})
    enriched["semantic_profile_v1"] = semantic_profile_for_locator(source, locator)
    encoded = json.dumps(
        enriched,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_BYTES:
        # Native proof/path fields can be recreated from the immutable source;
        # keep only the shared profile if an older client supplied excess data.
        enriched = {"semantic_profile_v1": enriched["semantic_profile_v1"]}
    return enriched


def _target_projection(profile: dict[str, Any]) -> tuple[str, str | None]:
    tag = str(profile.get("tag_name") or "").lower()
    attributes = profile.get("safe_attributes")
    attrs = attributes if isinstance(attributes, dict) else {}
    text = next(
        (
            str(attrs[name])
            for name in ("aria-label", "title", "alt", "value")
            if isinstance(attrs.get(name), str) and str(attrs[name]).strip()
        ),
        str(profile.get("normalized_text") or ""),
    )
    kind = {
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "h4": "heading",
        "h5": "heading",
        "h6": "heading",
        "button": "button",
        "a": "link",
        "img": "image",
        "input": "input",
        "textarea": "input",
        "select": "input",
        "form": "form",
        "ul": "list",
        "ol": "list",
        "li": "list",
        "table": "table",
        "p": "text",
        "span": "text",
        "section": "section",
        "article": "section",
    }.get(tag, "region")
    normalized = _normalized_text(text, limit=120)
    return kind, normalized or None


def target_projection(anchor: Anchor) -> tuple[TargetStatus, TargetReason | None, str, str | None]:
    context = anchor.context if isinstance(anchor.context, dict) else {}
    profile = context.get("semantic_profile_v1")
    if not isinstance(profile, dict):
        profile = {"tag_name": anchor.locator.get("tag_name")}
    kind, text = _target_projection(profile)
    if anchor.state is AnchorState.RESOLVED:
        return "ready", None, kind, text
    raw_reason = context.get("target_reason")
    reason: TargetReason = "ambiguous" if raw_reason == "ambiguous" else "no_match"
    return "contextual", reason, kind, text


@dataclass(frozen=True, slots=True)
class HtmlAnchorResolution:
    status: TargetStatus
    reason: TargetReason | None
    kind: AnchorKind
    locator: dict[str, Any]
    quote: str | None
    context: dict[str, Any]
    state: AnchorState
    target_kind: str
    target_text: str | None


def _unique_candidate(
    elements: tuple[_Element, ...],
    candidate_indexes: list[int],
    old_profile: dict[str, Any],
    old_quote: str | None,
) -> tuple[int | None, TargetReason]:
    def unique(values: list[int]) -> int | None:
        return values[0] if len(values) == 1 else None

    old_attrs = old_profile.get("safe_attributes")
    attrs = old_attrs if isinstance(old_attrs, dict) else {}
    for name in _STRONG_IDENTITY_ATTRIBUTES:
        value = attrs.get(name)
        if not isinstance(value, str) or not value:
            continue
        matches = [
            index
            for index in candidate_indexes
            if elements[index].attributes.get(name) == value
        ]
        selected = unique(matches)
        if selected is not None:
            return selected, "no_match"
        if len(matches) > 1:
            return None, "ambiguous"

    old_text = old_profile.get("normalized_text")
    if isinstance(old_text, str) and old_text:
        matches = [
            index
            for index in candidate_indexes
            if _normalized_text("".join(elements[index].text_parts)) == old_text
        ]
        selected = unique(matches)
        if selected is not None:
            return selected, "no_match"
        if len(matches) > 1:
            return None, "ambiguous"

    stable_attrs = {
        key: value
        for key, value in attrs.items()
        if key not in {"value", "type"} and isinstance(value, str) and value
    }
    if stable_attrs:
        matches = [
            index
            for index in candidate_indexes
            if all(
                elements[index].attributes.get(key) == value
                for key, value in stable_attrs.items()
            )
        ]
        selected = unique(matches)
        if selected is not None:
            return selected, "no_match"
        if len(matches) > 1:
            return None, "ambiguous"

    if isinstance(old_quote, str) and old_quote:
        matches = [
            index
            for index in candidate_indexes
            if elements[index].opening_text == old_quote
        ]
        selected = unique(matches)
        if selected is not None:
            return selected, "no_match"
        if len(matches) > 1:
            return None, "ambiguous"

    structural_matches: list[int] = []
    for index in candidate_indexes:
        profile = _profile_for_index(elements, index)
        if (
            profile.get("ancestor_tags") == old_profile.get("ancestor_tags")
            and profile.get("parent_hint") == old_profile.get("parent_hint")
            and profile.get("previous_hint") == old_profile.get("previous_hint")
            and profile.get("next_hint") == old_profile.get("next_hint")
        ):
            structural_matches.append(index)
    selected = unique(structural_matches)
    if selected is not None:
        return selected, "no_match"
    if len(structural_matches) > 1 or len(candidate_indexes) > 1:
        return None, "ambiguous"
    return None, "no_match"


def remap_html_anchor(
    *,
    old_source: str,
    current_source: str,
    anchor: Anchor,
) -> HtmlAnchorResolution:
    """Resolve one old source anchor against the current head without guessing."""

    if anchor.kind is not AnchorKind.DOM_SOURCE:
        raise ValueError("only DOM source anchors can be remapped")
    old_context = anchor.context if isinstance(anchor.context, dict) else {}
    profile = old_context.get("semantic_profile_v1")
    if not isinstance(profile, dict):
        profile = semantic_profile_for_locator(old_source, anchor.locator)
    tag_name = str(profile.get("tag_name") or anchor.locator.get("tag_name") or "").lower()
    if not _TAG_RE.fullmatch(tag_name):
        raise ValueError("HTML anchor lost its element tag")
    elements = _parse_elements(current_source)
    candidates = [index for index, element in enumerate(elements) if element.tag_name == tag_name]
    selected, reason = _unique_candidate(elements, candidates, profile, anchor.quote)
    target_kind, target_text = _target_projection(profile)
    source_sha256 = hashlib.sha256(current_source.encode("utf-8")).hexdigest()
    if selected is None:
        context = {
            "semantic_profile_v1": profile,
            "target_reason": reason,
        }
        return HtmlAnchorResolution(
            status="contextual",
            reason=reason,
            kind=AnchorKind.DOM_SOURCE,
            locator={
                "tag_name": tag_name,
                "source_sha256": source_sha256,
                "offset_encoding": SOURCE_OFFSET_ENCODING,
            },
            quote=anchor.quote,
            context=context,
            state=AnchorState.ORPHANED,
            target_kind=target_kind,
            target_text=target_text,
        )

    element = elements[selected]
    current_profile = _profile_for_index(elements, selected)
    target_kind, target_text = _target_projection(current_profile)
    locator = {
        "start_offset": element.start,
        "start_tag_end_offset": element.opening_end,
        "tag_name": element.tag_name,
        "source_sha256": source_sha256,
        "offset_encoding": SOURCE_OFFSET_ENCODING,
    }
    current_context = canonical_selection_context_for_locator(current_source, locator)
    current_context["semantic_profile_v1"] = current_profile
    return HtmlAnchorResolution(
        status="ready",
        reason=None,
        kind=AnchorKind.DOM_SOURCE,
        locator=locator,
        quote=element.opening_text[:2048],
        context=current_context,
        state=AnchorState.RESOLVED,
        target_kind=target_kind,
        target_text=target_text,
    )


@dataclass(frozen=True, slots=True)
class ContextualCandidate:
    locator: dict[str, Any]
    tag_name: str
    fingerprint: str


def contextual_candidate(
    *,
    source: str,
    candidate_source: str,
    expected_tag_name: str,
) -> ContextualCandidate:
    """Validate an exact, unique full opening tag proposed from ``document_read``."""

    if (
        not isinstance(candidate_source, str)
        or not candidate_source
        or len(candidate_source.encode("utf-8")) > MAX_CANDIDATE_SOURCE_BYTES
        or not candidate_source.startswith("<")
    ):
        raise ValueError("candidateSource must be a bounded full HTML opening tag")
    if source.count(candidate_source) != 1:
        raise ValueError("candidateSource must occur exactly once in the current source")
    start = source.index(candidate_source)
    end = start + len(candidate_source)
    elements = _parse_elements(source)
    matches = [
        element
        for element in elements
        if element.start == start
        and element.opening_end == end
        and element.opening_text == candidate_source
    ]
    if len(matches) != 1:
        raise ValueError("candidateSource must begin at a complete source-backed opening tag")
    element = matches[0]
    if element.tag_name in UNSUPPORTED_CONTEXTUAL_ELEMENTS:
        raise ValueError("candidateSource identifies an unsupported HTML element")
    if element.tag_name == "input" and element.attributes.get("type", "text") in {
        "hidden",
        "password",
    }:
        raise ValueError("candidateSource identifies a non-annotatable input element")
    if element.tag_name != expected_tag_name.lower():
        raise ValueError("candidateSource must identify the same kind of element")
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        (
            source_sha256
            + "\0"
            + str(start)
            + "\0"
            + str(end)
            + "\0"
            + element.tag_name
            + "\0"
            + candidate_source
        ).encode("utf-8")
    ).hexdigest()
    return ContextualCandidate(
        locator={
            "start_offset": start,
            "start_tag_end_offset": end,
            "tag_name": element.tag_name,
            "source_sha256": source_sha256,
            "offset_encoding": SOURCE_OFFSET_ENCODING,
        },
        tag_name=element.tag_name,
        fingerprint=fingerprint,
    )


__all__ = [
    "ContextualCandidate",
    "HtmlAnchorChangedError",
    "HtmlAnchorResolution",
    "MAX_CONTEXT_BYTES",
    "MAX_SEMANTIC_PROFILE_BYTES",
    "SOURCE_OFFSET_ENCODING",
    "canonical_browser_dom_digest",
    "canonical_element_at_path",
    "canonical_element_proof_sha256",
    "canonical_opening_anchor",
    "canonical_selection_proofs",
    "canonical_selection_context_for_locator",
    "contextual_candidate",
    "enrich_anchor_context",
    "remap_html_anchor",
    "parse_element_path",
    "semantic_profile_for_locator",
    "target_projection",
]
