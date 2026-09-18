from __future__ import annotations

import base64
from pathlib import Path
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


def test_unparsed_upload_does_not_claim_parse_success(tmp_path: Path) -> None:
    facts: list[FileParseReliabilityFacts] = []
    payload = b"SYNTHETIC file body"
    TurnRunner._build_attachment_messages(
        "Inspect when needed", [_attachment("text/plain", payload)],
        workspace_dir=tmp_path, session_id="synthetic-session",
        file_parse_fact_sink=facts.append,
    )
    assert facts == []
    path = next(tmp_path.rglob("*-PRIVATE-file-name.txt"))
    assert path.read_bytes() == payload


def test_unparsed_upload_does_not_claim_parser_failure(tmp_path: Path) -> None:
    facts: list[FileParseReliabilityFacts] = []
    payloads = [b"\xff\xfe", b"not a pdf"]
    TurnRunner._build_attachment_messages(
        "Inspect when needed", [
            _attachment("text/plain", payloads[0]),
            _attachment("application/pdf", payloads[1]),
        ], workspace_dir=tmp_path, session_id="synthetic-session",
        file_parse_fact_sink=facts.append,
    )
    assert facts == []
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert sorted(path.read_bytes() for path in files) == sorted(payloads)


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
