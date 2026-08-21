"""Semantic document adapter contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from opensquilla.tools.builtin.document_format_adapters import (
    DocumentAdapterError,
    DocumentFormatAdapter,
    DocumentMutationError,
    DocumentMutationTarget,
    GrantedMutationInput,
    HtmlDocumentFormatAdapter,
    PreparedAdapterCandidate,
    mutation_error_from_adapter,
    probe_document_format_adapter,
    validate_editable_html_source,
)


def _opening(source: str, tag: str) -> tuple[int, int]:
    start = source.index(f"<{tag}")
    return start, source.index(">", start) + 1


def _anchor_locator(source: str, tag: str) -> dict[str, object]:
    start, end = _opening(source, tag)
    return {
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "start_offset": start,
        "start_tag_end_offset": end,
        "tag_name": tag,
    }


def _locate(
    adapter: HtmlDocumentFormatAdapter,
    source: str,
    tag: str,
    operation: str,
    *,
    attribute_name: str | None = None,
) -> DocumentMutationTarget:
    return adapter.locate(
        source.encode("utf-8"),
        anchor_locator=_anchor_locator(source, tag),
        annotation_order=0,
        operation=operation,
        attribute_name=attribute_name,
    )[0]


def _prepare(
    adapter: HtmlDocumentFormatAdapter,
    source: str,
    target: DocumentMutationTarget,
    *,
    has_input: bool,
    input_value: object | None = None,
    operation: str | None = None,
) -> PreparedAdapterCandidate:
    return adapter.prepare_granted_mutations(
        source.encode("utf-8"),
        mutations=(
            GrantedMutationInput(
                operation=operation or target.operation,
                target_fingerprint=target.target_fingerprint,
                annotation_orders=target.annotation_orders,
                has_input=has_input,
                input_value=input_value,
                adapter_locator=target.adapter_locator,
            ),
        ),
    )


def test_html_adapter_owns_probe_capability_read_and_preview_contracts() -> None:
    source = "<!doctype html><html><body><h1>Private title</h1></body></html>"
    adapter = probe_document_format_adapter(
        name="upload.bin",
        media_type="application/octet-stream",
        source=source.encode("utf-8"),
    )

    assert isinstance(adapter, HtmlDocumentFormatAdapter)
    assert adapter.capabilities() == {
        "adapterId": "html",
        "adapterVersion": 1,
        "preview": True,
        "read": True,
        "manualEdit": True,
        "agentEdit": True,
        "sourceEdit": True,
        "selection": True,
        "promptAnnotations": True,
        "semanticOperations": [
            "remove_attribute",
            "remove_node",
            "replace_text",
            "set_attribute",
            "set_style",
        ],
        "supportedOperations": [
            "remove_attribute",
            "remove_node",
            "replace_text",
            "set_attribute",
            "set_style",
        ],
        "reasonCode": None,
    }
    assert adapter.read(source, view="source") == source
    structure = adapter.read(source, view="structure")
    assert isinstance(structure, dict)
    assert structure["headings"] == [{"level": 1, "text": "Private title"}]
    preview = adapter.preview(source)
    assert preview["sandboxProfile"] == "opaque-offline"
    assert preview["network"] is False
    assert preview["byteSize"] == len(source.encode("utf-8"))
    assert source not in repr(preview)


def test_adapter_probe_fails_closed_for_unrecognized_binary_material() -> None:
    assert probe_document_format_adapter(
        name="upload.bin",
        media_type="application/octet-stream",
        source=b"\x00\xffnot-html",
    ) is None


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("<main>before\x00after</main>", "DOCUMENT_HTML_ENCODING_INVALID"),
        ("x" * (2 * 1024 * 1024 + 1), "DOCUMENT_CANDIDATE_SIZE_INVALID"),
    ],
    ids=("nul-byte", "oversized"),
)
def test_html_adapter_rejects_sources_outside_the_editable_contract(
    source: str,
    code: str,
) -> None:
    with pytest.raises(DocumentAdapterError) as raised:
        validate_editable_html_source(source)

    assert raised.value.code == code


def test_html_adapter_removes_void_img_without_requiring_an_end_tag() -> None:
    source = '<main><img class="hero" src="photo.png"><p>Keep me</p></main>'
    adapter = HtmlDocumentFormatAdapter()

    located = adapter.locate(
        source.encode("utf-8"),
        anchor_locator=_anchor_locator(source, "img"),
        annotation_order=0,
        operation="remove_node",
    )

    assert len(located) == 1
    target = located[0]
    assert not hasattr(target, "start")
    assert not hasattr(target, "end")
    assert not hasattr(target, "kind")
    prepared = _prepare(adapter, source, target, has_input=False)
    assert prepared.candidate_bytes.decode("utf-8") == "<main><p>Keep me</p></main>"


def test_html_adapter_removes_only_the_balanced_selected_element() -> None:
    source = "<main><section><div>Nested</div></section><section>Keep</section></main>"
    adapter = HtmlDocumentFormatAdapter()

    target = _locate(adapter, source, "section", "remove_node")
    assert target.current == "<section><div>Nested</div></section>"
    prepared = _prepare(adapter, source, target, has_input=False)
    assert prepared.candidate_bytes.decode("utf-8") == (
        "<main><section>Keep</section></main>"
    )


def test_html_adapter_preserves_source_around_attribute_and_style_edits() -> None:
    source = "<button  CLASS='primary' data-x=1>Run</button>"
    adapter = HtmlDocumentFormatAdapter()

    class_target = _locate(
        adapter,
        source,
        "button",
        "set_attribute",
        attribute_name="class",
    )
    class_edit = _prepare(
        adapter,
        source,
        class_target,
        has_input=True,
        input_value="primary danger",
    )
    assert class_edit.candidate_bytes.decode("utf-8") == (
        '<button CLASS="primary danger" data-x=1>Run</button>'
    )

    style_target = _locate(adapter, source, "button", "set_style")
    style_edit = _prepare(
        adapter,
        source,
        style_target,
        has_input=True,
        input_value="color: red; background: rgb(1, 2, 3)",
    )
    assert style_edit.candidate_bytes.decode("utf-8") == (
        "<button  CLASS='primary' data-x=1 "
        'style="color: red; background: rgb(1, 2, 3)">Run</button>'
    )


def test_html_adapter_replaces_plain_text_and_escapes_new_markup() -> None:
    source = "<h1>Before &amp; now</h1><p>Keep</p>"
    adapter = HtmlDocumentFormatAdapter()
    target = _locate(adapter, source, "h1", "replace_text")
    prepared = _prepare(
        adapter,
        source,
        target,
        has_input=True,
        input_value="Cold <brew> & tea",
    )
    assert prepared.candidate_bytes.decode("utf-8") == (
        "<h1>Cold &lt;brew&gt; &amp; tea</h1><p>Keep</p>"
    )


def test_html_adapter_allows_style_removal_but_not_generic_style_setting() -> None:
    source = '<button style="color:red" class="primary">Run</button>'
    adapter = HtmlDocumentFormatAdapter()

    remove_target = _locate(
        adapter,
        source,
        "button",
        "remove_attribute",
        attribute_name="style",
    )
    prepared = _prepare(adapter, source, remove_target, has_input=False)
    assert prepared.candidate_bytes.decode("utf-8") == (
        '<button class="primary">Run</button>'
    )

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_ATTRIBUTE_UNSAFE"):
        adapter.locate(
            source.encode("utf-8"),
            anchor_locator=_anchor_locator(source, "button"),
            annotation_order=0,
            operation="set_attribute",
            attribute_name="style",
        )


@pytest.mark.parametrize(
    "value",
    ["color: red; } body { display:none", "background: javascript:alert(1)"],
)
def test_html_adapter_rejects_unsafe_or_structural_inline_css(value: str) -> None:
    source = "<button>Run</button>"
    adapter = HtmlDocumentFormatAdapter()
    target = _locate(adapter, source, "button", "set_style")

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_STYLE_INVALID"):
        _prepare(adapter, source, target, has_input=True, input_value=value)


def test_html_adapter_grant_cannot_be_repurposed_for_another_operation() -> None:
    source = "<h1>Before</h1>"
    adapter = HtmlDocumentFormatAdapter()
    target = _locate(adapter, source, "h1", "replace_text")

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_GRANT_OPERATION_MISMATCH"):
        _prepare(
            adapter,
            source,
            target,
            has_input=False,
            operation="remove_node",
        )


def test_html_adapter_rejects_overlapping_opaque_targets_before_candidate() -> None:
    source = '<button class="primary">Run</button>'
    adapter = HtmlDocumentFormatAdapter()
    style_target = _locate(adapter, source, "button", "set_style")
    class_target = _locate(
        adapter,
        source,
        "button",
        "set_attribute",
        attribute_name="class",
    )

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_MUTATION_OVERLAP"):
        adapter.prepare_granted_mutations(
            source.encode("utf-8"),
            mutations=(
                GrantedMutationInput(
                    operation=style_target.operation,
                    target_fingerprint=style_target.target_fingerprint,
                    annotation_orders=style_target.annotation_orders,
                    has_input=True,
                    input_value="color: red",
                    adapter_locator=style_target.adapter_locator,
                ),
                GrantedMutationInput(
                    operation=class_target.operation,
                    target_fingerprint=class_target.target_fingerprint,
                    annotation_orders=class_target.annotation_orders,
                    has_input=True,
                    input_value="primary danger",
                    adapter_locator=class_target.adapter_locator,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _BinaryLocator:
    marker: bytes


class _SyntheticBinaryAdapter(DocumentFormatAdapter):
    format_id = "synthetic-binary"
    adapter_version = 1
    supported_operations = frozenset({"replace_text"})

    def probe(
        self,
        *,
        name: str,
        media_type: str,
        source: bytes | None = None,
    ) -> bool:
        del name, media_type
        return source is not None and source.startswith(b"\x00\xff")

    def capabilities(self) -> dict[str, object]:
        return {"adapterId": self.format_id, "adapterVersion": self.adapter_version}

    def inspect_source(self, source: str) -> dict[str, object]:
        raise AssertionError(f"binary mutation must not decode source: {source!r}")

    def locate(
        self,
        payload: bytes,
        *,
        anchor_locator: object,
        annotation_order: int,
        operation: str,
        attribute_name: str | None = None,
    ) -> tuple[DocumentMutationTarget, ...]:
        del anchor_locator, attribute_name
        assert payload == b"\x00\xffPK\x00"
        assert operation == "replace_text"
        return (
            DocumentMutationTarget(
                operation=operation,
                annotation_orders=(annotation_order,),
                target_fingerprint="b" * 64,
                current="binary target",
                before="",
                after="",
                adapter_locator=_BinaryLocator(marker=b"PK"),
            ),
        )

    def prepare_granted_mutations(
        self,
        payload: bytes,
        *,
        mutations: tuple[GrantedMutationInput, ...],
    ) -> PreparedAdapterCandidate:
        assert payload == b"\x00\xffPK\x00"
        assert len(mutations) == 1
        mutation = mutations[0]
        assert isinstance(mutation.adapter_locator, _BinaryLocator)
        assert mutation.adapter_locator.marker == b"PK"
        assert mutation.input_value == {"replacement": b"not-json".hex()}
        return PreparedAdapterCandidate(
            candidate_bytes=payload + b"\xfe\x00",
            semantic_operations=({"operation": mutation.operation},),
            audit_facts=(),
            validation_summary={"binaryValidation": "passed"},
        )

    def validate_candidate(self, source: str) -> dict[str, object]:
        raise AssertionError(f"binary mutation must not decode candidate: {source!r}")

    def preview(self, source: str) -> dict[str, object]:
        raise AssertionError(f"binary mutation must not decode preview: {source!r}")


def test_format_neutral_batch_boundary_accepts_non_utf8_bytes_and_opaque_locator() -> None:
    adapter = _SyntheticBinaryAdapter()
    payload = b"\x00\xffPK\x00"
    target = adapter.locate(
        payload,
        anchor_locator={"semantic": "part-1"},
        annotation_order=2,
        operation="replace_text",
    )[0]

    candidate = adapter.prepare_granted_mutations(
        payload,
        mutations=(
            GrantedMutationInput(
                operation=target.operation,
                target_fingerprint=target.target_fingerprint,
                annotation_orders=target.annotation_orders,
                has_input=True,
                input_value={"replacement": b"not-json".hex()},
                adapter_locator=target.adapter_locator,
            ),
        ),
    )

    assert candidate.candidate_bytes == b"\x00\xffPK\x00\xfe\x00"
    assert candidate.validation_summary == {"binaryValidation": "passed"}


@pytest.mark.parametrize(
    ("code", "retry_policy"),
    [
        ("DOCUMENT_OPENING_TAG_STALE", "refresh"),
        ("DOCUMENT_ATTRIBUTE_VALUE_UNSAFE", "forbidden"),
        ("DOCUMENT_STYLE_INVALID", "correctable"),
    ],
)
def test_adapter_errors_have_stable_agent_retry_policy(
    code: str,
    retry_policy: str,
) -> None:
    error = mutation_error_from_adapter(DocumentAdapterError(code, "Safe detail."))
    assert isinstance(error, DocumentMutationError)
    assert error.code == code
    assert error.retry_policy == retry_policy
    assert error.user_message == f"{code}: Safe detail."
