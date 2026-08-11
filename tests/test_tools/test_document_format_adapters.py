"""Semantic document adapter contracts."""

from __future__ import annotations

import pytest

from opensquilla.tools.builtin.document_format_adapters import (
    DocumentAdapterError,
    DocumentMutationError,
    HtmlDocumentFormatAdapter,
    mutation_error_from_adapter,
    probe_document_format_adapter,
)


def _opening(source: str, tag: str) -> tuple[int, int]:
    start = source.index(f"<{tag}")
    return start, source.index(">", start) + 1


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


def test_html_adapter_removes_void_img_without_requiring_an_end_tag() -> None:
    source = '<main><img class="hero" src="photo.png"><p>Keep me</p></main>'
    start, end = _opening(source, "img")
    adapter = HtmlDocumentFormatAdapter()

    located = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="remove_node",
    )

    assert len(located) == 1
    source_range = located[0]
    assert source_range.start == start
    assert source_range.end == end
    assert source_range.kind.endswith("|remove_node|void|img|-")
    prepared = adapter.prepare_mutation(
        source,
        start=source_range.start,
        end=source_range.end,
        grant_kind=source_range.kind,
        operation="remove_node",
        value=None,
        attribute_name=None,
    )
    assert source[:start] + prepared.replacement + source[end:] == (
        "<main><p>Keep me</p></main>"
    )


def test_html_adapter_removes_only_the_balanced_selected_element() -> None:
    source = "<main><section><div>Nested</div></section><section>Keep</section></main>"
    start, end = _opening(source, "section")
    adapter = HtmlDocumentFormatAdapter()

    source_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="remove_node",
    )[0]
    assert source[source_range.start : source_range.end] == (
        "<section><div>Nested</div></section>"
    )
    prepared = adapter.prepare_mutation(
        source,
        start=source_range.start,
        end=source_range.end,
        grant_kind=source_range.kind,
        operation="remove_node",
        value=None,
        attribute_name=None,
    )
    updated = source[: source_range.start] + prepared.replacement + source[source_range.end :]
    assert updated == "<main><section>Keep</section></main>"


def test_html_adapter_preserves_source_around_attribute_and_style_edits() -> None:
    source = "<button  CLASS='primary' data-x=1>Run</button>"
    start, end = _opening(source, "button")
    adapter = HtmlDocumentFormatAdapter()

    class_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="set_attribute",
        attribute_name="class",
    )[0]
    class_edit = adapter.prepare_mutation(
        source,
        start=class_range.start,
        end=class_range.end,
        grant_kind=class_range.kind,
        operation="set_attribute",
        value="primary danger",
        attribute_name="class",
    )
    assert class_edit.replacement == '<button CLASS="primary danger" data-x=1>'

    style_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="set_style",
    )[0]
    style_edit = adapter.prepare_mutation(
        source,
        start=style_range.start,
        end=style_range.end,
        grant_kind=style_range.kind,
        operation="set_style",
        value="color: red; background: rgb(1, 2, 3)",
        attribute_name=None,
    )
    assert style_edit.replacement == (
        "<button  CLASS='primary' data-x=1 "
        'style="color: red; background: rgb(1, 2, 3)">'
    )
    assert source[end:] == "Run</button>"


def test_html_adapter_replaces_plain_text_and_escapes_new_markup() -> None:
    source = "<h1>Before &amp; now</h1><p>Keep</p>"
    start, end = _opening(source, "h1")
    adapter = HtmlDocumentFormatAdapter()
    source_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="replace_text",
    )[0]
    prepared = adapter.prepare_mutation(
        source,
        start=source_range.start,
        end=source_range.end,
        grant_kind=source_range.kind,
        operation="replace_text",
        value="Cold <brew> & tea",
        attribute_name=None,
    )
    updated = source[: source_range.start] + prepared.replacement + source[source_range.end :]
    assert updated == "<h1>Cold &lt;brew&gt; &amp; tea</h1><p>Keep</p>"


def test_html_adapter_allows_style_removal_but_not_generic_style_setting() -> None:
    source = '<button style="color:red" class="primary">Run</button>'
    start, end = _opening(source, "button")
    adapter = HtmlDocumentFormatAdapter()

    remove_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="remove_attribute",
        attribute_name="style",
    )[0]
    prepared = adapter.prepare_mutation(
        source,
        start=remove_range.start,
        end=remove_range.end,
        grant_kind=remove_range.kind,
        operation="remove_attribute",
        value=None,
        attribute_name="style",
    )
    assert prepared.replacement == '<button class="primary">'

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_ATTRIBUTE_UNSAFE"):
        adapter.locate(
            source,
            opening_start=start,
            opening_end=end,
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
    start, end = _opening(source, "button")
    adapter = HtmlDocumentFormatAdapter()
    source_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="set_style",
    )[0]

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_STYLE_INVALID"):
        adapter.prepare_mutation(
            source,
            start=source_range.start,
            end=source_range.end,
            grant_kind=source_range.kind,
            operation="set_style",
            value=value,
            attribute_name=None,
        )


def test_html_adapter_grant_cannot_be_repurposed_for_another_operation() -> None:
    source = "<h1>Before</h1>"
    start, end = _opening(source, "h1")
    adapter = HtmlDocumentFormatAdapter()
    source_range = adapter.locate(
        source,
        opening_start=start,
        opening_end=end,
        annotation_order=0,
        operation="replace_text",
    )[0]

    with pytest.raises(DocumentAdapterError, match="DOCUMENT_GRANT_OPERATION_MISMATCH"):
        adapter.prepare_mutation(
            source,
            start=source_range.start,
            end=source_range.end,
            grant_kind=source_range.kind,
            operation="remove_node",
            value=None,
            attribute_name=None,
        )


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
