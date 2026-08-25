"""Stable Artifact product error vocabulary and compatibility mappings."""

from __future__ import annotations

import pytest

from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    artifact_product_error,
    canonical_artifact_product_code,
    logged_artifact_product_error,
)


@pytest.mark.parametrize(
    ("legacy", "expected"),
    (
        ("ARTIFACT_ANNOTATION_NOT_DRAFT", ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE),
        ("ARTIFACT_CHANGE_NOT_APPLIED", ArtifactProductErrorCode.MUTATION_NOT_APPLIED),
        ("ARTIFACT_CONFLICT", ArtifactProductErrorCode.DOCUMENT_CHANGED),
        ("ARTIFACT_FOCUS_UNAVAILABLE", ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE),
        ("ARTIFACT_FOCUS_UNSUPPORTED", ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE),
        ("ARTIFACT_PREVIEW_CHANGED", ArtifactProductErrorCode.DOCUMENT_CHANGED),
        ("ARTIFACT_SELECTION_CHANGED", ArtifactProductErrorCode.DOCUMENT_CHANGED),
        ("ARTIFACT_SELECTION_UNAVAILABLE", ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE),
        ("ARTIFACT_SELECTION_UNSUPPORTED", ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE),
        ("ARTIFACT_SOURCE_ENCODING", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("ARTIFACT_SOURCE_TOO_LARGE", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("ARTIFACT_SOURCE_UNSUPPORTED", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("DOCUMENT_PUBLISH_FORMAT_UNSUPPORTED", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("WORKBENCH_CURSOR_STALE", ArtifactProductErrorCode.DOCUMENT_CHANGED),
        ("WORKBENCH_PREVIEW_ENCODING_UNSUPPORTED", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("WORKBENCH_PREVIEW_UNSUPPORTED", ArtifactProductErrorCode.RESOURCE_UNSUPPORTED),
        ("INVALID_PARAMS", ArtifactProductErrorCode.INVALID_REQUEST),
        ("BAD_REQUEST", ArtifactProductErrorCode.INVALID_REQUEST),
        ("UNAVAILABLE", ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE),
    ),
)
def test_legacy_artifact_codes_have_stable_product_recovery_categories(
    legacy: str,
    expected: ArtifactProductErrorCode,
) -> None:
    assert canonical_artifact_product_code(legacy) is expected


def test_safe_product_messages_do_not_expose_consistency_protocol_terms() -> None:
    banned = (
        "revision",
        "sha256",
        "editsession",
        "receipt",
        "lease",
        "changeset",
        "protocol-v3",
    )
    for code in ArtifactProductErrorCode:
        error = artifact_product_error(code)
        message = error.message.lower()
        assert all(term not in message for term in banned), (code, error.message)


def test_logged_product_error_keeps_raw_exception_out_of_the_wire() -> None:
    error = logged_artifact_product_error(
        ArtifactProductErrorCode.DOCUMENT_CHANGED,
        RuntimeError("revision rev-private failed sha256=secret receipt=private"),
        operation="synthetic.test",
    )

    assert error.code == "DOCUMENT_CHANGED"
    assert "rev-private" not in error.message
    assert "secret" not in error.message
    assert error.details is not None
    assert error.details["correlationId"]
