"""Model-facing guidance for ordinary bound Document turns."""

from __future__ import annotations

import re

from opensquilla.tools.registry import get_default_registry


def test_document_read_schema_requires_server_issued_continuation_cursor() -> None:
    registered = get_default_registry().get("document_read")
    assert registered is not None

    description = registered.spec.description
    properties = registered.spec.parameters["properties"]
    cursor_description = properties["cursor"]["description"]
    cursor_pattern = re.compile(properties["cursor"]["pattern"])

    assert "current bound Document" in description
    assert "first source read" in description
    assert 'cursor=""' in description
    assert "exact nextCursor" in description
    assert "Omit this property entirely" in cursor_description
    assert "provider adapter requires every schema field" in cursor_description
    assert "immediately preceding document_read" in cursor_description
    assert "never invent" in cursor_description
    assert cursor_pattern.fullmatch("") is not None
    assert cursor_pattern.fullmatch(" \t\r\n") is not None
    assert cursor_pattern.fullmatch("hcur_" + ("a" * 43)) is not None
    assert cursor_pattern.fullmatch("hcur_" + ("a" * 42)) is None
    assert cursor_pattern.fullmatch("invented") is None


def test_document_patch_schema_requires_bound_read_result_not_workspace_copy() -> None:
    registered = get_default_registry().get("document_patch")
    assert registered is not None

    description = registered.spec.description
    properties = registered.spec.parameters["properties"]
    edit_properties = properties["edits"]["items"]["properties"]

    assert "only tool that updates the current bound HTML Document" in description
    assert "view=source and no cursor" in description
    assert "write_file, edit_file, and apply_patch" in description
    assert "cannot replace this operation" in description
    assert "exact sha256 returned by document_read" in properties["expectedSha256"][
        "description"
    ]
    assert "Never invent it" in properties["expectedSha256"]["description"]
    assert "occurs exactly once" in edit_properties["expectedText"]["description"]
