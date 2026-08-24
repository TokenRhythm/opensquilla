"""Format-neutral document mutation adapters.

The model-facing document tools express semantic operations.  Adapters turn
those operations into source-preserving replacements only after a turn-scoped
grant has proved the exact immutable source range.  No adapter accepts a path,
document identifier, source offset, or raw OOXML coordinate from the model.
"""

from __future__ import annotations

import hashlib
import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal

from opensquilla.tools.types import SafeToolError

DocumentMutationRetryPolicy = Literal["correctable", "refresh", "forbidden"]


class DocumentMutationError(SafeToolError):
    """Sanitized mutation failure with machine-readable Agent-loop policy."""

    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        retry_policy: DocumentMutationRetryPolicy,
    ) -> None:
        self.code = code
        self.retry_policy = retry_policy
        self.error_class = "document_mutation"
        super().__init__(f"{code}: {user_message}")


def mutation_error_from_adapter(exc: DocumentAdapterError) -> DocumentMutationError:
    """Map adapter validation to one stable Agent-loop retry policy."""

    retry_policy: DocumentMutationRetryPolicy
    if "UNSAFE" in exc.code:
        retry_policy = "forbidden"
    elif any(marker in exc.code for marker in ("STALE", "RANGE", "GRANT")):
        retry_policy = "refresh"
    else:
        retry_policy = "correctable"
    return DocumentMutationError(
        exc.code,
        exc.user_message,
        retry_policy=retry_policy,
    )


class DocumentAdapterError(ValueError):
    """Stable, sanitized adapter failure safe to return through a tool envelope."""

    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message


class DocumentSemanticOperation(StrEnum):
    REPLACE_TEXT = "replace_text"
    SET_ATTRIBUTE = "set_attribute"
    REMOVE_ATTRIBUTE = "remove_attribute"
    SET_STYLE = "set_style"
    REMOVE_NODE = "remove_node"


DOCUMENT_SEMANTIC_OPERATIONS = frozenset(item.value for item in DocumentSemanticOperation)


@dataclass(frozen=True, slots=True)
class DocumentMutationTarget:
    """Model-safe target preview plus one process-local adapter locator."""

    operation: str
    annotation_orders: tuple[int, ...]
    target_fingerprint: str
    current: str
    before: str
    after: str
    adapter_locator: object = dataclass_field(repr=False, compare=False)
    confidence: str = "exact"
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class GrantedMutationInput:
    """One reserved semantic grant passed into an adapter-owned batch prepare."""

    operation: str
    target_fingerprint: str
    annotation_orders: tuple[int, ...]
    has_input: bool
    input_value: object | None
    adapter_locator: object = dataclass_field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedAdapterCandidate:
    """Pure candidate and bounded audit facts produced from opaque grants."""

    candidate_bytes: bytes
    semantic_operations: tuple[dict[str, object], ...]
    audit_facts: tuple[dict[str, object], ...]
    validation_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedAdapterMutation:
    """One adapter-private replacement inside a document proposal."""

    operation: str
    replacement: str
    target_kind: str
    attribute_name: str | None = None
    css_mutation: bool = False


class DocumentFormatAdapter(ABC):
    """Internal adapter contract shared by HTML and future Office formats."""

    format_id: str
    adapter_version: int
    supported_operations: frozenset[str]

    @abstractmethod
    def probe(
        self,
        *,
        name: str,
        media_type: str,
        source: bytes | None = None,
    ) -> bool:
        """Return whether this adapter can safely own the supplied material."""

    @abstractmethod
    def capabilities(self) -> dict[str, object]:
        """Return the adapter's stable, format-level capability contract."""

    @abstractmethod
    def inspect_source(self, source: str) -> dict[str, object]:
        """Return a bounded semantic summary without executing document content."""

    @abstractmethod
    def locate(
        self,
        payload: bytes,
        *,
        anchor_locator: object,
        annotation_order: int,
        operation: str,
        attribute_name: str | None = None,
    ) -> tuple[DocumentMutationTarget, ...]:
        """Derive adapter-owned targets without exposing private coordinates."""

    @abstractmethod
    def prepare_granted_mutations(
        self,
        payload: bytes,
        *,
        mutations: tuple[GrantedMutationInput, ...],
    ) -> PreparedAdapterCandidate:
        """Validate opaque locators and produce one atomic candidate in memory."""

    @abstractmethod
    def validate_candidate(self, source: str) -> dict[str, object]:
        """Validate the complete candidate after all source-preserving splices."""

    def inspect(self, source: str) -> dict[str, object]:
        """Return the format-neutral document inspection view."""

        return self.inspect_source(source)

    def read(self, source: str, *, view: str) -> str | dict[str, object]:
        """Return one canonical adapter view before transport-level pagination."""

        if view == "source":
            return source
        if view == "structure":
            return self.inspect(source)
        raise DocumentAdapterError(
            "DOCUMENT_VIEW_UNSUPPORTED",
            "This document view is not supported by the active format adapter.",
        )

    def validate(self, source: str) -> dict[str, object]:
        """Format-neutral alias for full-candidate validation."""

        return self.validate_candidate(source)

    @abstractmethod
    def preview(self, source: str) -> dict[str, object]:
        """Return a bounded host preview contract without embedding document text."""


_ATTRIBUTE_NAME_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_.:-]{0,127}$")
_UNSAFE_ATTRIBUTE_NAMES = frozenset({"srcdoc", "xmlns"})
_URL_ATTRIBUTE_NAMES = frozenset(
    {"action", "formaction", "href", "poster", "src", "xlink:href"}
)
_GRANT_PREFIX = "document|html|1|"
_MAX_HTML_CANDIDATE_BYTES = 2 * 1024 * 1024
_MAX_HTML_CONTEXT_CHARS = 160


@dataclass(frozen=True, slots=True)
class _HtmlMutationLocator:
    start: int
    end: int
    grant_kind: str
    expected_slice_sha256: str

HTML_VOID_ELEMENTS = frozenset(
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
_OPTIONAL_END_ELEMENTS = frozenset(
    {
        "colgroup",
        "dd",
        "dt",
        "li",
        "optgroup",
        "option",
        "p",
        "rp",
        "rt",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
    }
)
_UNSUPPORTED_STRUCTURAL_ELEMENTS = frozenset(
    {"iframe", "noembed", "noframes", "plaintext", "script", "style", "template", "xmp"}
)


@dataclass(frozen=True, slots=True)
class _HtmlAttribute:
    name: str
    source_name: str
    full_start: int
    full_end: int


@dataclass(frozen=True, slots=True)
class _HtmlOpeningTag:
    tag_name: str
    attributes: tuple[_HtmlAttribute, ...]
    closing_start: int


def _normalize_attribute_name(value: str | None, *, allow_style: bool = False) -> str:
    if not isinstance(value, str) or _ATTRIBUTE_NAME_RE.fullmatch(value) is None:
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_INVALID",
            "The attribute name is not supported.",
        )
    normalized = value.lower()
    if (
        normalized.startswith("on")
        or normalized in _UNSAFE_ATTRIBUTE_NAMES
        or (normalized == "style" and not allow_style)
    ):
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_UNSAFE",
            "This attribute cannot be changed by an annotated document turn.",
        )
    return normalized


def _parse_opening_tag(value: str) -> _HtmlOpeningTag | None:
    """Parse one start tag while retaining source spans for byte-preserving edits."""

    match = re.match(r"(?is)^\s*<([A-Za-z][A-Za-z0-9:-]*)", value)
    if match is None:
        return None
    tag_name = match.group(1).lower()
    cursor = match.end()
    attributes: list[_HtmlAttribute] = []
    while cursor < len(value):
        whitespace_start = cursor
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if value.startswith("/>", cursor):
            return _HtmlOpeningTag(tag_name, tuple(attributes), cursor)
        if cursor < len(value) and value[cursor] == ">":
            return _HtmlOpeningTag(tag_name, tuple(attributes), cursor)
        if cursor == whitespace_start:
            return None
        name_start = cursor
        while (
            cursor < len(value)
            and not value[cursor].isspace()
            and value[cursor] not in "=/><"
        ):
            cursor += 1
        source_name = value[name_start:cursor]
        if _ATTRIBUTE_NAME_RE.fullmatch(source_name) is None:
            return None
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] == "=":
            cursor += 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
            if cursor >= len(value):
                return None
            if value[cursor] in {"'", '"'}:
                quote = value[cursor]
                cursor += 1
                while cursor < len(value) and value[cursor] != quote:
                    cursor += 1
                if cursor >= len(value):
                    return None
                cursor += 1
            else:
                unquoted_start = cursor
                while (
                    cursor < len(value)
                    and not value[cursor].isspace()
                    and value[cursor] not in "<>'\"=`"
                ):
                    cursor += 1
                if cursor == unquoted_start:
                    return None
        attributes.append(
            _HtmlAttribute(
                name=source_name.lower(),
                source_name=source_name,
                full_start=whitespace_start,
                full_end=cursor,
            )
        )
    return None


def _attribute_matches(opening: _HtmlOpeningTag, name: str) -> tuple[_HtmlAttribute, ...]:
    return tuple(item for item in opening.attributes if item.name == name)


def _safe_attribute_value(name: str, value: str | None) -> str:
    if not isinstance(value, str):
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_VALUE_INVALID",
            "Setting an attribute requires a string value.",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_VALUE_INVALID",
            "The attribute value contains unsupported control characters.",
        )
    compact = "".join(value.split()).lower()
    if name in _URL_ATTRIBUTE_NAMES and (
        compact.startswith("javascript:") or compact.startswith("data:text/html")
    ):
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_VALUE_UNSAFE",
            "Executable URL values are not supported.",
        )
    return html.escape(value, quote=True)


def _set_attribute(
    opening_text: str,
    *,
    name: str,
    value: str,
    allow_style: bool = False,
) -> str:
    parsed = _parse_opening_tag(opening_text)
    if parsed is None:
        raise DocumentAdapterError(
            "DOCUMENT_OPENING_TAG_INVALID",
            "The selected opening tag could not be parsed safely.",
        )
    normalized = _normalize_attribute_name(name, allow_style=allow_style)
    matches = _attribute_matches(parsed, normalized)
    if len(matches) > 1:
        raise DocumentAdapterError(
            "DOCUMENT_ATTRIBUTE_AMBIGUOUS",
            "The selected element contains a duplicate attribute.",
        )
    encoded = _safe_attribute_value(normalized, value)
    if matches:
        match = matches[0]
        replacement = f' {match.source_name}="{encoded}"'
        return opening_text[: match.full_start] + replacement + opening_text[match.full_end :]
    insertion = f' {normalized}="{encoded}"'
    return opening_text[: parsed.closing_start] + insertion + opening_text[parsed.closing_start :]


def _remove_attribute(opening_text: str, *, name: str) -> str:
    parsed = _parse_opening_tag(opening_text)
    if parsed is None:
        raise DocumentAdapterError(
            "DOCUMENT_OPENING_TAG_INVALID",
            "The selected opening tag could not be parsed safely.",
        )
    normalized = _normalize_attribute_name(name, allow_style=True)
    matches = _attribute_matches(parsed, normalized)
    if len(matches) != 1:
        code = "DOCUMENT_ATTRIBUTE_NOT_FOUND" if not matches else "DOCUMENT_ATTRIBUTE_AMBIGUOUS"
        raise DocumentAdapterError(
            code,
            "The selected element does not contain one uniquely removable attribute.",
        )
    match = matches[0]
    return opening_text[: match.full_start] + opening_text[match.full_end :]


def _valid_css_declarations(value: str) -> bool:
    if not value.strip() or any(char in value for char in "{}<>"):
        return False
    quote: str | None = None
    parentheses = 0
    brackets = 0
    escaped = False
    comment = False
    segment_start = 0
    segments: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        next_char = value[index + 1] if index + 1 < len(value) else ""
        if comment:
            if char == "*" and next_char == "/":
                comment = False
                index += 2
                continue
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char == "/" and next_char == "*":
            comment = True
            index += 2
            continue
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            parentheses += 1
        elif char == ")":
            parentheses -= 1
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1
        elif char == ";" and parentheses == 0 and brackets == 0:
            segments.append(value[segment_start:index])
            segment_start = index + 1
        if parentheses < 0 or brackets < 0:
            return False
        index += 1
    if quote is not None or parentheses or brackets or escaped or comment:
        return False
    segments.append(value[segment_start:])
    property_re = re.compile(r"^(?:--[A-Za-z0-9_-]+|-?[A-Za-z_][A-Za-z0-9_-]*)$")
    declarations = 0
    for segment in segments:
        cleaned = re.sub(r"(?s)/\*.*?\*/", "", segment).strip()
        if not cleaned:
            continue
        property_name, separator, property_value = cleaned.partition(":")
        if (
            not separator
            or property_re.fullmatch(property_name.strip()) is None
            or not property_value.strip()
            or re.search(r"(?i)(?:javascript\s*:|expression\s*\()", property_value)
        ):
            return False
        declarations += 1
    return declarations > 0


class _ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag.lower(), {name.lower(): value or "" for name, value in attrs}))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class _ExplicitElementBoundaryParser(HTMLParser):
    def __init__(self, source: str, opening_start: int, opening_end: int, tag_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source = source
        self._opening_start = opening_start
        self._opening_end = opening_end
        self._tag_name = tag_name
        self._line_starts = [0, *(match.end() for match in re.finditer(r"\n", source))]
        self._active = False
        self._depth = 0
        self.opening_verified = False
        self.close_span: tuple[int, int] | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        if line < 1 or line > len(self._line_starts):
            return -1
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        offset = self._offset()
        raw = self.get_starttag_text() or ""
        if not self._active:
            if (
                offset == self._opening_start
                and offset + len(raw) == self._opening_end
                and raw == self._source[self._opening_start : self._opening_end]
                and tag.lower() == self._tag_name
            ):
                self._active = True
                self._depth = 1
                self.opening_verified = True
            return
        if tag.lower() == self._tag_name:
            self._depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        if not self._active or tag.lower() != self._tag_name:
            return
        self._depth -= 1
        if self._depth:
            return
        offset = self._offset()
        match = re.match(rf"(?is)</{re.escape(self._tag_name)}\s*>", self._source[offset:])
        if match is not None:
            self.close_span = (offset, offset + match.end())
        self._active = False


def _element_boundaries(
    source: str,
    *,
    opening_start: int,
    opening_end: int,
    tag_name: str,
) -> tuple[int, int] | None:
    if (
        tag_name in HTML_VOID_ELEMENTS
        or tag_name in _OPTIONAL_END_ELEMENTS
        or tag_name in _UNSUPPORTED_STRUCTURAL_ELEMENTS
        or source[max(opening_start, opening_end - 2) : opening_end].rstrip().endswith("/>")
    ):
        return None
    parser = _ExplicitElementBoundaryParser(source, opening_start, opening_end, tag_name)
    try:
        parser.feed(source)
        parser.close()
    except (TypeError, ValueError):
        return None
    if not parser.opening_verified or parser.close_span is None:
        return None
    return parser.close_span


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.element_count = 0
        self.headings: list[dict[str, object]] = []
        self._heading: tuple[int, list[str]] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.lower()
        self.element_count += 1
        if name in {"script", "style", "template"}:
            self._ignored_depth += 1
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"} and len(self.headings) < 100:
            self._heading = (int(name[1]), [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if self._heading is not None and name == f"h{self._heading[0]}":
            text = " ".join("".join(self._heading[1]).split())[:500]
            self.headings.append({"level": self._heading[0], "text": text})
            self._heading = None
        if name in {"script", "style", "template"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or self._heading is None:
            return
        if sum(len(item) for item in self._heading[1]) < 500:
            self._heading[1].append(data)


class HtmlDocumentFormatAdapter(DocumentFormatAdapter):
    format_id = "html"
    adapter_version = 1
    supported_operations = DOCUMENT_SEMANTIC_OPERATIONS

    @staticmethod
    def _decode_payload(payload: bytes) -> str:
        if not isinstance(payload, bytes) or len(payload) > _MAX_HTML_CANDIDATE_BYTES:
            raise DocumentAdapterError(
                "DOCUMENT_HTML_PAYLOAD_INVALID",
                "The HTML document payload is invalid or too large.",
            )
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise DocumentAdapterError(
                "DOCUMENT_HTML_ENCODING_INVALID",
                "HTML semantic editing requires UTF-8 content.",
            ) from None
        if not source:
            raise DocumentAdapterError(
                "DOCUMENT_CANDIDATE_EMPTY",
                "The HTML document must not be empty.",
            )
        return source

    def probe(
        self,
        *,
        name: str,
        media_type: str,
        source: bytes | None = None,
    ) -> bool:
        mime = str(media_type or "").split(";", 1)[0].strip().lower()
        suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if mime in {"text/html", "application/xhtml+xml"} or suffix in {
            "htm",
            "html",
            "xhtml",
        }:
            return True
        if source is None:
            return False
        try:
            head = source[:4096].decode("utf-8-sig", errors="strict").lstrip().lower()
        except UnicodeDecodeError:
            return False
        return bool(re.match(r"<(?:!doctype\s+html\b|html\b|head\b|body\b)", head))

    def capabilities(self) -> dict[str, object]:
        return {
            "adapterId": self.format_id,
            "adapterVersion": self.adapter_version,
            "preview": True,
            "read": True,
            "manualEdit": True,
            "agentEdit": True,
            "sourceEdit": True,
            "selection": True,
            "promptAnnotations": True,
            "semanticOperations": sorted(self.supported_operations),
            "supportedOperations": sorted(self.supported_operations),
            "reasonCode": None,
        }

    @staticmethod
    def _grant_kind(
        operation: str,
        *,
        target_kind: str,
        tag_name: str,
        attribute_name: str | None = None,
    ) -> str:
        attribute = attribute_name or "-"
        return f"{_GRANT_PREFIX}{operation}|{target_kind}|{tag_name}|{attribute}"

    def _target_fingerprint(
        self,
        source: str,
        *,
        start: int,
        end: int,
        grant_kind: str,
    ) -> str:
        """Bind a grant to one adapter-private HTML target without exposing offsets."""

        digest = hashlib.sha256()
        digest.update(f"{self.format_id}\0{self.adapter_version}\0".encode())
        digest.update(f"{start}\0{end}\0{grant_kind}\0".encode())
        digest.update(source[start:end].encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _opening(source: str, start: int, end: int) -> _HtmlOpeningTag:
        if start < 0 or end <= start or end > len(source):
            raise DocumentAdapterError(
                "DOCUMENT_RANGE_INVALID",
                "The selected source range is invalid.",
            )
        opening = _parse_opening_tag(source[start:end])
        if opening is None:
            raise DocumentAdapterError(
                "DOCUMENT_OPENING_TAG_INVALID",
                "The selected opening tag could not be parsed safely.",
            )
        return opening

    def inspect_source(self, source: str) -> dict[str, object]:
        return self.validate_candidate(source)

    def locate(
        self,
        payload: bytes,
        *,
        anchor_locator: object,
        annotation_order: int,
        operation: str,
        attribute_name: str | None = None,
    ) -> tuple[DocumentMutationTarget, ...]:
        source = self._decode_payload(payload)
        if not isinstance(anchor_locator, dict):
            raise DocumentAdapterError(
                "DOCUMENT_ANCHOR_INVALID",
                "The selected HTML target is not source-backed.",
            )
        opening_start = anchor_locator.get("start_offset")
        opening_end = anchor_locator.get(
            "start_tag_end_offset",
            anchor_locator.get("end_offset"),
        )
        source_sha256 = anchor_locator.get("source_sha256")
        tag_name_hint = anchor_locator.get("tag_name")
        if (
            isinstance(opening_start, bool)
            or not isinstance(opening_start, int)
            or isinstance(opening_end, bool)
            or not isinstance(opening_end, int)
            or source_sha256 != hashlib.sha256(payload).hexdigest()
            or not isinstance(tag_name_hint, str)
        ):
            raise DocumentAdapterError(
                "DOCUMENT_ANCHOR_STALE",
                "The selected HTML target no longer matches the current document.",
            )
        try:
            semantic_operation = DocumentSemanticOperation(operation)
        except ValueError:
            raise DocumentAdapterError(
                "DOCUMENT_OPERATION_UNSUPPORTED",
                "The requested semantic operation is not supported.",
            ) from None
        opening = self._opening(source, opening_start, opening_end)
        tag_name = opening.tag_name
        if tag_name != tag_name_hint.lower():
            raise DocumentAdapterError(
                "DOCUMENT_ANCHOR_STALE",
                "The selected HTML target no longer matches the current document.",
            )
        if tag_name in _UNSUPPORTED_STRUCTURAL_ELEMENTS:
            return ()

        normalized_attribute: str | None = None
        if semantic_operation in {
            DocumentSemanticOperation.SET_ATTRIBUTE,
            DocumentSemanticOperation.REMOVE_ATTRIBUTE,
        }:
            normalized_attribute = _normalize_attribute_name(
                attribute_name,
                allow_style=semantic_operation is DocumentSemanticOperation.REMOVE_ATTRIBUTE,
            )
        elif attribute_name is not None:
            raise DocumentAdapterError(
                "DOCUMENT_ATTRIBUTE_UNEXPECTED",
                "This operation does not accept an attribute name.",
            )

        if semantic_operation is DocumentSemanticOperation.REPLACE_TEXT:
            close_span = _element_boundaries(
                source,
                opening_start=opening_start,
                opening_end=opening_end,
                tag_name=tag_name,
            )
            if close_span is None:
                return ()
            close_start, _close_end = close_span
            inner = source[opening_end:close_start]
            if not inner or "<" in inner or len(inner.encode("utf-8")) > 16 * 1024:
                return ()
            target_start, target_end, target_kind = opening_end, close_start, "text"
        elif semantic_operation is DocumentSemanticOperation.REMOVE_NODE:
            if tag_name in HTML_VOID_ELEMENTS:
                target_start, target_end, target_kind = opening_start, opening_end, "void"
            else:
                close_span = _element_boundaries(
                    source,
                    opening_start=opening_start,
                    opening_end=opening_end,
                    tag_name=tag_name,
                )
                if close_span is None:
                    return ()
                _close_start, close_end = close_span
                if len(source[opening_start:close_end].encode("utf-8")) > 16 * 1024:
                    return ()
                target_start, target_end, target_kind = opening_start, close_end, "element"
        else:
            if (
                semantic_operation is DocumentSemanticOperation.REMOVE_ATTRIBUTE
                and normalized_attribute is not None
                and len(_attribute_matches(opening, normalized_attribute)) != 1
            ):
                return ()
            target_start, target_end, target_kind = opening_start, opening_end, "opening"

        if len(source[target_start:target_end].encode("utf-8")) > 16 * 1024:
            return ()

        kind = self._grant_kind(
            semantic_operation.value,
            target_kind=target_kind,
            tag_name=tag_name,
            attribute_name=normalized_attribute,
        )
        target_fingerprint = self._target_fingerprint(
            source,
            start=target_start,
            end=target_end,
            grant_kind=kind,
        )
        current = source[target_start:target_end]
        return (
            DocumentMutationTarget(
                operation=semantic_operation.value,
                annotation_orders=(annotation_order,),
                target_fingerprint=target_fingerprint,
                current=current,
                before=source[max(0, target_start - _MAX_HTML_CONTEXT_CHARS) : target_start],
                after=source[target_end : target_end + _MAX_HTML_CONTEXT_CHARS],
                adapter_locator=_HtmlMutationLocator(
                    start=target_start,
                    end=target_end,
                    grant_kind=kind,
                    expected_slice_sha256=hashlib.sha256(current.encode("utf-8")).hexdigest(),
                ),
                detail=(
                    f"{tag_name}[{normalized_attribute}]"
                    if normalized_attribute is not None
                    else tag_name
                ),
            ),
        )

    def _prepare_html_mutation(
        self,
        source: str,
        *,
        start: int,
        end: int,
        grant_kind: str,
        operation: str,
        input_value: object | None,
        attribute_name: str | None,
    ) -> PreparedAdapterMutation:
        if start < 0 or end <= start or end > len(source):
            raise DocumentAdapterError(
                "DOCUMENT_RANGE_INVALID",
                "The granted source range is invalid.",
            )
        parts = grant_kind.split("|")
        if len(parts) != 7 or "|".join(parts[:3]) + "|" != _GRANT_PREFIX:
            raise DocumentAdapterError(
                "DOCUMENT_GRANT_KIND_INVALID",
                "The mutation grant is not valid for this document adapter.",
            )
        granted_operation, target_kind, tag_name, granted_attribute = parts[3:]
        try:
            semantic_operation = DocumentSemanticOperation(operation)
        except ValueError:
            raise DocumentAdapterError(
                "DOCUMENT_OPERATION_UNSUPPORTED",
                "The requested semantic operation is not supported.",
            ) from None
        if semantic_operation.value != granted_operation:
            raise DocumentAdapterError(
                "DOCUMENT_GRANT_OPERATION_MISMATCH",
                "The mutation grant does not authorize this operation.",
            )
        requested_attribute: str | None = None
        if semantic_operation in {
            DocumentSemanticOperation.SET_ATTRIBUTE,
            DocumentSemanticOperation.REMOVE_ATTRIBUTE,
        }:
            requested_attribute = _normalize_attribute_name(
                attribute_name,
                allow_style=semantic_operation is DocumentSemanticOperation.REMOVE_ATTRIBUTE,
            )
            if requested_attribute != granted_attribute:
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_ATTRIBUTE_MISMATCH",
                    "The mutation grant does not authorize this attribute.",
                )
        elif attribute_name is not None:
            raise DocumentAdapterError(
                "DOCUMENT_ATTRIBUTE_UNEXPECTED",
                "This operation does not accept an attribute name.",
            )

        original = source[start:end]
        if semantic_operation is DocumentSemanticOperation.REPLACE_TEXT:
            if (
                target_kind != "text"
                or granted_attribute != "-"
                or not isinstance(input_value, str)
            ):
                raise DocumentAdapterError(
                    "DOCUMENT_TEXT_GRANT_INVALID",
                    "The mutation grant is not valid for text replacement.",
                )
            replacement = html.escape(input_value, quote=False)
            if not replacement:
                raise DocumentAdapterError(
                    "DOCUMENT_TEXT_VALUE_INVALID",
                    "Replacement text must not be empty.",
                )
            return PreparedAdapterMutation(
                operation=semantic_operation.value,
                replacement=replacement,
                target_kind=target_kind,
            )

        if semantic_operation is DocumentSemanticOperation.REMOVE_NODE:
            if (
                input_value is not None
                or requested_attribute is not None
                or target_kind not in {
                    "element",
                    "void",
                }
            ):
                raise DocumentAdapterError(
                    "DOCUMENT_REMOVE_NODE_INVALID",
                    "The mutation grant is not valid for element removal.",
                )
            opening_match = re.match(r"(?is)^\s*<([A-Za-z][A-Za-z0-9:-]*)", original)
            if opening_match is None or opening_match.group(1).lower() != tag_name:
                raise DocumentAdapterError(
                    "DOCUMENT_REMOVE_NODE_STALE",
                    "The selected element no longer matches its mutation grant.",
                )
            if target_kind == "void" and tag_name not in HTML_VOID_ELEMENTS:
                raise DocumentAdapterError(
                    "DOCUMENT_REMOVE_NODE_INVALID",
                    "Only a verified void element may use a void-element deletion grant.",
                )
            return PreparedAdapterMutation(
                operation=semantic_operation.value,
                replacement="",
                target_kind=target_kind,
            )

        parsed = _parse_opening_tag(original)
        if parsed is None or parsed.tag_name != tag_name or target_kind != "opening":
            raise DocumentAdapterError(
                "DOCUMENT_OPENING_TAG_STALE",
                "The selected opening tag no longer matches its mutation grant.",
            )
        if semantic_operation is DocumentSemanticOperation.SET_ATTRIBUTE:
            assert requested_attribute is not None
            if not isinstance(input_value, str):
                raise DocumentAdapterError(
                    "DOCUMENT_ATTRIBUTE_VALUE_INVALID",
                    "Setting an attribute requires a string value.",
                )
            replacement = _set_attribute(
                original,
                name=requested_attribute,
                value=input_value,
            )
            return PreparedAdapterMutation(
                operation=semantic_operation.value,
                replacement=replacement,
                target_kind=target_kind,
                attribute_name=requested_attribute,
            )
        if semantic_operation is DocumentSemanticOperation.REMOVE_ATTRIBUTE:
            if input_value is not None:
                raise DocumentAdapterError(
                    "DOCUMENT_ATTRIBUTE_VALUE_UNEXPECTED",
                    "Removing an attribute does not accept a value.",
                )
            assert requested_attribute is not None
            return PreparedAdapterMutation(
                operation=semantic_operation.value,
                replacement=_remove_attribute(original, name=requested_attribute),
                target_kind=target_kind,
                attribute_name=requested_attribute,
                css_mutation=requested_attribute == "style",
            )
        if semantic_operation is DocumentSemanticOperation.SET_STYLE:
            if not isinstance(input_value, str) or not _valid_css_declarations(input_value):
                raise DocumentAdapterError(
                    "DOCUMENT_STYLE_INVALID",
                    "The style must be a validated CSS declaration list.",
                )
            return PreparedAdapterMutation(
                operation=semantic_operation.value,
                replacement=_set_attribute(
                    original,
                    name="style",
                    value=input_value,
                    allow_style=True,
                ),
                target_kind=target_kind,
                attribute_name="style",
                css_mutation=True,
            )
        raise DocumentAdapterError(
            "DOCUMENT_OPERATION_UNSUPPORTED",
            "The requested semantic operation is not supported.",
        )

    def apply_grant(
        self,
        source: str,
        *,
        start: int,
        end: int,
        grant_kind: str,
        input_value: object | None,
    ) -> PreparedAdapterMutation:
        """Support the pre-existing source-range helper outside the restricted prompt path."""

        parts = grant_kind.split("|")
        if len(parts) != 7 or "|".join(parts[:3]) + "|" != _GRANT_PREFIX:
            raise DocumentAdapterError(
                "DOCUMENT_GRANT_KIND_INVALID",
                "The mutation grant is not valid for the HTML adapter.",
            )
        operation = parts[3]
        attribute_name = (
            parts[6]
            if operation
            in {
                DocumentSemanticOperation.SET_ATTRIBUTE.value,
                DocumentSemanticOperation.REMOVE_ATTRIBUTE.value,
            }
            else None
        )
        return self._prepare_html_mutation(
            source,
            start=start,
            end=end,
            grant_kind=grant_kind,
            operation=operation,
            input_value=input_value,
            attribute_name=attribute_name,
        )

    def prepare_granted_mutations(
        self,
        payload: bytes,
        *,
        mutations: tuple[GrantedMutationInput, ...],
    ) -> PreparedAdapterCandidate:
        source = self._decode_payload(payload)
        if not mutations:
            raise DocumentAdapterError(
                "DOCUMENT_MUTATIONS_INVALID",
                "Mutations must be a non-empty bounded array.",
            )
        prepared_rows: list[
            tuple[int, int, str, PreparedAdapterMutation, GrantedMutationInput]
        ] = []
        validated_css_mutation = False
        for mutation in mutations:
            locator = mutation.adapter_locator
            if not isinstance(locator, _HtmlMutationLocator):
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_LOCATOR_INVALID",
                    "The mutation grant is not valid for the HTML adapter.",
                )
            if locator.start < 0 or locator.end <= locator.start or locator.end > len(source):
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_STALE",
                    "The selected HTML target no longer matches the current document.",
                )
            current = source[locator.start : locator.end]
            if (
                hashlib.sha256(current.encode("utf-8")).hexdigest()
                != locator.expected_slice_sha256
                or self._target_fingerprint(
                    source,
                    start=locator.start,
                    end=locator.end,
                    grant_kind=locator.grant_kind,
                )
                != mutation.target_fingerprint
            ):
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_STALE",
                    "The selected HTML target no longer matches the current document.",
                )
            parts = locator.grant_kind.split("|")
            if len(parts) != 7 or "|".join(parts[:3]) + "|" != _GRANT_PREFIX:
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_KIND_INVALID",
                    "The mutation grant is not valid for the HTML adapter.",
                )
            granted_operation = parts[3]
            if mutation.operation != granted_operation:
                raise DocumentAdapterError(
                    "DOCUMENT_GRANT_OPERATION_MISMATCH",
                    "The mutation grant does not authorize this operation.",
                )
            if granted_operation in {"remove_attribute", "remove_node"}:
                if mutation.has_input:
                    raise DocumentAdapterError(
                        "DOCUMENT_MUTATION_INPUT_UNEXPECTED",
                        "This mutation grant does not accept input. Copy its applyTemplate "
                        "exactly and omit the input field entirely.",
                    )
            elif not mutation.has_input:
                raise DocumentAdapterError(
                    "DOCUMENT_MUTATION_INPUT_REQUIRED",
                    "This mutation grant requires input. Copy its applyTemplate and replace "
                    "the placeholder with the requested value.",
                )
            granted_attribute = parts[6]
            attribute_name = (
                granted_attribute
                if granted_operation
                in {
                    DocumentSemanticOperation.SET_ATTRIBUTE.value,
                    DocumentSemanticOperation.REMOVE_ATTRIBUTE.value,
                }
                else None
            )
            prepared = self._prepare_html_mutation(
                source,
                start=locator.start,
                end=locator.end,
                grant_kind=locator.grant_kind,
                operation=granted_operation,
                input_value=mutation.input_value,
                attribute_name=attribute_name,
            )
            validated_css_mutation = validated_css_mutation or prepared.css_mutation
            prepared_rows.append(
                (locator.start, locator.end, current, prepared, mutation)
            )

        ordered = sorted(prepared_rows, key=lambda row: (row[0], row[1]))
        for previous, current_row in zip(ordered, ordered[1:], strict=False):
            if current_row[0] < previous[1] or current_row[0] == previous[0]:
                raise DocumentAdapterError(
                    "DOCUMENT_MUTATION_OVERLAP",
                    "Document mutation targets must not overlap.",
                )
        updated = source
        for start, end, _current, prepared, _mutation in reversed(ordered):
            updated = updated[:start] + prepared.replacement + updated[end:]
        if updated == source:
            raise DocumentAdapterError(
                "DOCUMENT_MUTATION_NO_OP",
                "The requested document mutation does not change the document.",
            )
        candidate_bytes = updated.encode("utf-8")
        adapter_validation = validate_editable_html_source(updated)
        audit_facts: list[dict[str, object]] = []
        semantic_operations: list[dict[str, object]] = []
        for _start, _end, current, prepared, mutation in ordered:
            audit_facts.append(
                {
                    "expected_chars": len(current),
                    "expected_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "replacement_chars": len(prepared.replacement),
                    "replacement_sha256": hashlib.sha256(
                        prepared.replacement.encode("utf-8")
                    ).hexdigest(),
                }
            )
            semantic_operations.append(
                {
                    "operation": prepared.operation,
                    "target_kind": prepared.target_kind,
                    "attribute_name": prepared.attribute_name,
                    "annotation_orders": list(mutation.annotation_orders),
                    "target_fingerprint": mutation.target_fingerprint,
                }
            )
        return PreparedAdapterCandidate(
            candidate_bytes=candidate_bytes,
            semantic_operations=tuple(semantic_operations),
            audit_facts=tuple(audit_facts),
            validation_summary={
                "semantic_adapter_validation": adapter_validation,
                "css_validation": (
                    "modified_grants_completed"
                    if validated_css_mutation
                    else "not_performed"
                ),
                "script_validation": "not_applicable_no_script_grants",
            },
        )

    def validate_candidate(self, source: str) -> dict[str, object]:
        if not source:
            raise DocumentAdapterError(
                "DOCUMENT_CANDIDATE_EMPTY",
                "The edited HTML document must not be empty.",
            )
        parser = _StructureParser()
        try:
            parser.feed(source)
            parser.close()
        except (TypeError, ValueError):
            raise DocumentAdapterError(
                "DOCUMENT_HTML_INVALID",
                "The edited HTML document could not be parsed safely.",
            ) from None
        return {
            "adapter": self.format_id,
            "adapter_version": self.adapter_version,
            "elements": parser.element_count,
            "headings": parser.headings,
            "semantic_validation": "completed",
        }

    def preview(self, source: str) -> dict[str, object]:
        validation = self.validate(source)
        encoded = source.encode("utf-8")
        return {
            "adapterId": self.format_id,
            "adapterVersion": self.adapter_version,
            "representation": "canonical-source",
            "mediaType": "text/html",
            "sandboxProfile": "opaque-offline",
            "network": False,
            "sourceSha256": hashlib.sha256(encoded).hexdigest(),
            "byteSize": len(encoded),
            "validation": {
                "status": "passed",
                "elements": validation["elements"],
            },
        }


_ADAPTERS: dict[str, DocumentFormatAdapter] = {"html": HtmlDocumentFormatAdapter()}


def validate_editable_html_source(source: str) -> dict[str, object]:
    """Validate the bounded NUL-free source contract shared by every HTML editor."""

    if not source:
        raise DocumentAdapterError(
            "DOCUMENT_CANDIDATE_EMPTY",
            "The edited HTML document must not be empty.",
        )
    if "\x00" in source:
        raise DocumentAdapterError(
            "DOCUMENT_HTML_ENCODING_INVALID",
            "HTML semantic editing requires NUL-free UTF-8 content.",
        )
    if len(source.encode("utf-8")) > _MAX_HTML_CANDIDATE_BYTES:
        raise DocumentAdapterError(
            "DOCUMENT_CANDIDATE_SIZE_INVALID",
            "The edited HTML document is too large.",
        )
    return _ADAPTERS["html"].validate(source)


def probe_document_format_adapter(
    *,
    name: str,
    media_type: str,
    source: bytes | None = None,
) -> DocumentFormatAdapter | None:
    """Find a format adapter without treating a filename as editing authority."""

    matches = [
        adapter
        for adapter in _ADAPTERS.values()
        if adapter.probe(name=name, media_type=media_type, source=source)
    ]
    return matches[0] if len(matches) == 1 else None


def get_document_format_adapter(artifact_format: str) -> DocumentFormatAdapter:
    """Return the registered adapter or fail closed with a stable reason."""

    adapter = _ADAPTERS.get(str(artifact_format or "").strip().lower())
    if adapter is None:
        raise DocumentAdapterError(
            "DOCUMENT_FORMAT_UNSUPPORTED",
            "No semantic editing adapter is available for this document format.",
        )
    return adapter


__all__ = [
    "DOCUMENT_SEMANTIC_OPERATIONS",
    "DocumentAdapterError",
    "DocumentFormatAdapter",
    "DocumentSemanticOperation",
    "DocumentMutationTarget",
    "GrantedMutationInput",
    "HTML_VOID_ELEMENTS",
    "HtmlDocumentFormatAdapter",
    "PreparedAdapterCandidate",
    "PreparedAdapterMutation",
    "get_document_format_adapter",
    "probe_document_format_adapter",
]
