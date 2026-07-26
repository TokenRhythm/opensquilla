"""Best-effort provider request/response trace recorder.

The recorder is intentionally side-effect safe: failures to write traces must
never affect model calls. It records no authorization headers and is enabled by
environment so external harnesses can keep full diagnostics without changing
provider behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any
from uuid import uuid4

from opensquilla.safety.secret_redaction import redact_secret_value

from .tokenrhythm_correlation import (
    TOKENRHYTHM_CALL_KIND_HEADER,
    TOKENRHYTHM_EXECUTION_ID_HEADER,
    TOKENRHYTHM_SESSION_ID_HEADER,
    TOKENRHYTHM_TURN_ID_HEADER,
)

_DEFAULT_TRACE_PATH = "/tmp/opensquilla-llm-calls.jsonl"
_RECORDER_ENV = "OPENSQUILLA_LLM_TRACE_RECORDER"
_PATH_ENV = "OPENSQUILLA_LLM_TRACE_PATH"
_INCLUDE_CHUNKS_ENV = "OPENSQUILLA_LLM_TRACE_INCLUDE_CHUNKS"
_OFF_VALUES = {"0", "false", "no", "off", "disabled", "disable"}
_CALL_COUNTER = count(1)
_PRESENT = "[PRESENT]"
_CORRELATION_HEADER_NAMES = frozenset(
    name.lower()
    for name in (
        TOKENRHYTHM_SESSION_ID_HEADER,
        TOKENRHYTHM_TURN_ID_HEADER,
        TOKENRHYTHM_EXECUTION_ID_HEADER,
        TOKENRHYTHM_CALL_KIND_HEADER,
    )
)


def _env_is_off(value: str | None) -> bool:
    return (value or "").strip().lower() in _OFF_VALUES


def _trace_path_from_env() -> str | None:
    mode = os.environ.get(_RECORDER_ENV)
    if _env_is_off(mode):
        return None
    path = os.environ.get(_PATH_ENV, "").strip()
    if path:
        return path
    if mode and not _env_is_off(mode):
        return _DEFAULT_TRACE_PATH
    return None


def _include_chunks_from_env() -> bool:
    return not _env_is_off(os.environ.get(_INCLUDE_CHUNKS_ENV, "1"))


def _redact(value: Any, *, key: str | None = None) -> Any:
    return redact_secret_value(value, key=key)


def _redact_request_headers(headers: dict[str, Any]) -> dict[str, Any]:
    return {
        str(name): (
            _PRESENT
            if str(name).lower() in _CORRELATION_HEADER_NAMES
            else _redact(value, key=str(name))
        )
        for name, value in headers.items()
    }


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


class LLMTraceRecorder:
    """Append-only JSONL recorder for one provider call."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        endpoint: str,
        stream: bool,
    ) -> None:
        self.path = _trace_path_from_env()
        self.enabled = bool(self.path)
        self.include_chunks = _include_chunks_from_env()
        self.call_index = next(_CALL_COUNTER)
        self.call_id = f"llm-{self.call_index}-{uuid4().hex[:12]}"
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.endpoint = endpoint
        self.stream = stream

    def record_request(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        sanitized_payload = _redact(payload)
        self._append(
            {
                "event": "llm.request",
                "payload_sha256": _sha256(sanitized_payload),
                "payload": sanitized_payload,
                "headers": _redact_request_headers(headers or {}),
                "metadata": _redact(metadata or {}),
            }
        )

    def record_chunk(self, chunk: dict[str, Any]) -> None:
        if not self.include_chunks:
            return
        self._append(
            {
                "event": "llm.response_chunk",
                "chunk": _redact(chunk),
                "chunk_sha256": _sha256(_redact(chunk)),
            }
        )

    def record_response(
        self,
        *,
        response: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        stop_reason: str | None = None,
        actual_model: str | None = None,
        assistant_text: str | None = None,
        reasoning_content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        response_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._append(
            {
                "event": "llm.response",
                "response": _redact(response or {}),
                "response_sha256": _sha256(_redact(response or {})) if response else None,
                "usage": _redact(usage or {}),
                "stop_reason": stop_reason,
                "actual_model": actual_model,
                "assistant_text": _redact(assistant_text),
                "reasoning_content": _redact(reasoning_content),
                "tool_calls": _redact(tool_calls or []),
                "response_ids": response_ids or [],
                "metadata": _redact(metadata or {}),
            }
        )

    def record_error(
        self,
        *,
        code: str,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._append(
            {
                "event": "llm.error",
                "code": code,
                "message": _redact(message),
                "status_code": status_code,
                "response_body": _redact(response_body),
                "metadata": _redact(metadata or {}),
            }
        )

    def _append(self, payload: dict[str, Any]) -> None:
        if not self.enabled or not self.path:
            return
        try:
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(UTC).isoformat()
            row = {
                "created_at": now,
                "call_id": self.call_id,
                "call_index": self.call_index,
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                "endpoint": self.endpoint,
                "stream": self.stream,
                **payload,
            }
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
                handle.write("\n")
        except OSError:
            return
