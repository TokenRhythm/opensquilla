from __future__ import annotations

import base64
from dataclasses import asdict
from typing import Any

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentStage,
    AttachmentStageInput,
)
from opensquilla.telemetry.contracts.common import ResultOutcome
from opensquilla.telemetry.contracts.reliability import (
    FileParseErrorCode,
    FileSizeBucket,
    FileType,
)
from opensquilla.telemetry.file_parse_facts import FileParseReliabilityFacts


def _attachment(media_type: str, payload: bytes) -> dict[str, str]:
    return {
        "type": media_type,
        "name": "PRIVATE-file-name.txt",
        "data": base64.b64encode(payload).decode("ascii"),
    }


def test_text_success_fact_contains_no_file_content_or_name() -> None:
    facts: list[FileParseReliabilityFacts] = []

    TurnRunner._build_attachment_messages(
        "PRIVATE prompt",
        [_attachment("text/plain", b"PRIVATE file body")],
        file_parse_fact_sink=facts.append,
    )

    assert len(facts) == 1
    assert facts[0].file_type is FileType.TEXT
    assert facts[0].size_bucket is FileSizeBucket.LT_100_KIB
    assert facts[0].outcome is ResultOutcome.SUCCESS
    assert facts[0].error_code is None
    serialized = repr(asdict(facts[0]))
    assert "PRIVATE" not in serialized
    assert "name" not in serialized
    assert "body" not in serialized


def test_invalid_utf8_and_malformed_pdf_use_closed_error_codes() -> None:
    facts: list[FileParseReliabilityFacts] = []

    TurnRunner._build_attachment_messages(
        "prompt",
        [
            _attachment("text/plain", b"\xff\xfe"),
            _attachment("application/pdf", b"not a pdf"),
        ],
        file_parse_fact_sink=facts.append,
    )

    assert [(fact.file_type, fact.outcome, fact.error_code) for fact in facts] == [
        (FileType.TEXT, ResultOutcome.FAIL, FileParseErrorCode.INVALID_UTF8),
        (FileType.PDF, ResultOutcome.FAIL, FileParseErrorCode.MALFORMED_PDF),
    ]


async def test_attachment_stage_returns_worker_facts_to_event_loop() -> None:
    expected = FileParseReliabilityFacts(
        file_type=FileType.DOCX,
        size_bucket=FileSizeBucket.KIB_100_TO_1_MIB,
        outcome=ResultOutcome.FAIL,
        error_code=FileParseErrorCode.INVALID_OFFICE_CONTAINER,
        duration_ms=3,
    )

    class Builder:
        supports_file_parse_facts = True

        def build_cancellable(
            self,
            _message: str,
            _attachments: list[dict],
            **kwargs: Any,
        ) -> None:
            kwargs["file_parse_fact_sink"](expected)
            return None

    outcome = await AttachmentStage(builder=Builder()).run(  # type: ignore[arg-type]
        AttachmentStageInput(
            effective_runtime_message="prompt",
            attachments=[_attachment("text/plain", b"body")],
        )
    )

    assert outcome.require_output().file_parse_facts == (expected,)
