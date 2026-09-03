from __future__ import annotations

import pytest

from opensquilla.telemetry.contracts.common import ResultOutcome
from opensquilla.telemetry.contracts.reliability import (
    FileParseErrorCode,
    FileSizeBucket,
    FileType,
)
from opensquilla.telemetry.file_parse_facts import (
    FileParseReliabilityFacts,
    file_size_bucket,
    file_type_for_media_type,
)


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("application/pdf", FileType.PDF),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            FileType.DOCX,
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            FileType.XLSX,
        ),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            FileType.PPTX,
        ),
        ("message/rfc822", FileType.EMAIL),
        ("application/mbox", FileType.EMAIL),
        ("application/vnd.ms-outlook", FileType.EMAIL),
        ("text/plain", FileType.TEXT),
        ("application/json", FileType.TEXT),
        ("image/png", None),
        ("application/octet-stream", None),
        ("text/plain; charset=utf-8", None),
        (None, None),
    ],
)
def test_file_type_uses_only_normalized_media_type(
    media_type: object,
    expected: FileType | None,
) -> None:
    assert file_type_for_media_type(media_type) is expected


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, FileSizeBucket.LT_100_KIB),
        (100 * 1024 - 1, FileSizeBucket.LT_100_KIB),
        (100 * 1024, FileSizeBucket.KIB_100_TO_1_MIB),
        (1024 * 1024, FileSizeBucket.MIB_1_TO_10),
        (10 * 1024 * 1024, FileSizeBucket.MIB_10_TO_50),
        (50 * 1024 * 1024, FileSizeBucket.GTE_50_MIB),
    ],
)
def test_file_size_buckets_have_exact_boundaries(
    size_bytes: int,
    expected: FileSizeBucket,
) -> None:
    assert file_size_bucket(size_bytes) is expected


def test_fact_shape_has_no_filename_path_content_or_exception() -> None:
    fact = FileParseReliabilityFacts(
        file_type=FileType.PDF,
        size_bucket=FileSizeBucket.KIB_100_TO_1_MIB,
        outcome=ResultOutcome.FAIL,
        error_code=FileParseErrorCode.MALFORMED_PDF,
        duration_ms=17,
    )

    assert set(fact.__slots__) == {
        "file_type",
        "size_bucket",
        "outcome",
        "error_code",
        "duration_ms",
    }


@pytest.mark.parametrize(
    "fact",
    [
        FileParseReliabilityFacts(
            file_type=FileType.TEXT,
            size_bucket=FileSizeBucket.LT_100_KIB,
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            duration_ms=0,
        ),
        FileParseReliabilityFacts(
            file_type=FileType.EMAIL,
            size_bucket=FileSizeBucket.MIB_1_TO_10,
            outcome=ResultOutcome.CANCEL,
            error_code=FileParseErrorCode.CANCELLED,
            duration_ms=1,
        ),
    ],
)
def test_valid_success_and_cancel_facts(fact: FileParseReliabilityFacts) -> None:
    assert fact.duration_ms >= 0


def test_invalid_success_error_pair_is_rejected() -> None:
    with pytest.raises(ValueError, match="success requires"):
        FileParseReliabilityFacts(
            file_type=FileType.TEXT,
            size_bucket=FileSizeBucket.LT_100_KIB,
            outcome=ResultOutcome.SUCCESS,
            error_code=FileParseErrorCode.INVALID_UTF8,
            duration_ms=0,
        )
