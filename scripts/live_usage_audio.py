"""Bounded OpenRouter audio-output probe using the production usage ledger.

Only accounting and protocol metadata leave this helper. Generated audio and
text are neither returned nor written to disk. It never retries a paid POST.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

from opensquilla.engine.usage_http import reserve_direct_usage_call
from scripts.live_harness_security import redact_text

MAX_SSE_BYTES = 8 * 1024 * 1024


class _StreamError(Exception):
    def __init__(self, kind: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.kind = kind


def _error_metadata(payload: Any, key: str) -> dict[str, Any]:
    """Keep a small diagnostic allowlist, never an upstream raw response."""
    if not isinstance(payload, Mapping):
        return {}
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return {}
    result: dict[str, Any] = {}
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for name, value in (
        ("code", error.get("code")),
        ("type", error.get("type")),
        ("error_type", metadata.get("error_type")),
        ("limit_source", metadata.get("limit_source")),
        ("provider_code", metadata.get("provider_code")),
    ):
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            result[name] = redact_text(value, [key])[:120]
    if isinstance(error.get("message"), str):
        result["message"] = redact_text(error["message"], [key])[:500]
    return result


async def stream_audio_case(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Issue one streaming audio request and account for its final usage footer.

    A valid bill remains billable even if audio delivery subsequently fails.
    Cancellation is propagated after accounting cleanup; missing receipts stay
    unknown. The caller supplies its usage scope and owns the client lifetime.
    """
    request_body = {
        **body,
        "stream": True,
        "stream_options": {**(body.get("stream_options") or {}), "include_usage": True},
    }
    reservation = await reserve_direct_usage_call(
        provider="openrouter",
        model=str(body.get("model") or ""),
        base_url=base_url,
        operation_kind="audio_output",
    )
    result: dict[str, Any] = {
        "boundary": "metered_protocol",
        "functional": False,
        "audioObserved": False,
        "audioChunks": 0,
        "audioEncodedBytes": 0,
        "terminal": False,
        "usageObserved": False,
    }
    last_usage: dict[str, Any] | None = None
    last_usage_raw = ""
    finish_reasons: set[str] = set()

    def consume(data: bytes) -> None:
        nonlocal last_usage, last_usage_raw
        if data.strip() == b"[DONE]":
            result["terminal"] = True
            return
        try:
            raw = data.decode("utf-8")
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _StreamError("invalid_sse", "Invalid JSON in audio stream") from exc
        if not isinstance(payload, dict):
            raise _StreamError("invalid_sse", "Audio stream event was not an object")
        if isinstance(payload.get("usage"), dict) and payload["usage"]:
            # Preserve the original JSON spelling of monetary values.
            last_usage, last_usage_raw = payload, raw
            result["usageObserved"] = True
        if payload.get("error"):
            result["errorMetadata"] = _error_metadata(payload, key)
            raise _StreamError("provider_stream_error", "Provider returned an audio stream error")
        choices = payload.get("choices") or []
        if not isinstance(choices, list):
            raise _StreamError("invalid_sse", "Invalid audio stream choices")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("error"):
                result["errorMetadata"] = _error_metadata(choice, key)
                raise _StreamError(
                    "provider_stream_error", "Provider returned an audio choice error"
                )
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                finish_reasons.add(reason[:80])
            delta = choice.get("delta")
            audio = delta.get("audio") if isinstance(delta, dict) else None
            data = audio.get("data") if isinstance(audio, dict) else None
            if isinstance(data, str) and data:
                result["audioObserved"] = True
                result["audioChunks"] += 1
                result["audioEncodedBytes"] += len(data)

    async def settle(reason: str) -> None:
        if last_usage is not None:
            await reservation.finalize_openai_response(last_usage, raw_json=last_usage_raw)
        else:
            await reservation.mark_unknown(reason)

    try:
        async with client.stream(
            "POST",
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json=request_body,
            follow_redirects=False,
        ) as response:
            result["httpStatus"] = response.status_code
            total = 0
            pending = b""
            event_data: list[bytes] = []
            error_body = bytearray()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_SSE_BYTES:
                    raise _StreamError(
                        "response_limit", "Audio response exceeded the 8 MiB limit"
                    )
                if not response.is_success:
                    error_body.extend(chunk)
                    continue
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if not line:
                        if event_data:
                            consume(b"\n".join(event_data))
                            event_data.clear()
                    elif line.startswith(b"data:"):
                        event_data.append(line[5:].lstrip(b" "))
                    if result["terminal"]:
                        break
                if result["terminal"]:
                    break
            if not response.is_success:
                try:
                    error_payload = json.loads(error_body)
                except (ValueError, UnicodeDecodeError):
                    error_payload = None
                result.update(
                    failureKind=f"http_{response.status_code}",
                    diagnostic=f"Audio request returned HTTP {response.status_code}",
                    errorMetadata=_error_metadata(error_payload, key),
                )
                await reservation.mark_unknown("audio_http_error")
                return result
            # Some upstream failures arrive as a non-streaming JSON object
            # after HTTP 200 was already accepted. Keep only error metadata.
            if not result["terminal"] and pending.lstrip().startswith(b"{"):
                consume(pending)
            if not result["terminal"]:
                raise _StreamError("stream_interrupted", "Audio stream ended without [DONE]")
        await settle("audio_usage_footer_missing")
        result["functional"] = result["audioObserved"]
        if not result["functional"]:
            result["failureKind"] = "audio_missing"
    except asyncio.CancelledError:
        await settle("audio_stream_cancelled")
        raise
    except (_StreamError, httpx.HTTPError) as exc:
        await settle("audio_stream_interrupted")
        result.update(
            failureKind=exc.kind if isinstance(exc, _StreamError) else "transport",
            diagnostic=redact_text(str(exc), [key])[:500],
        )
    except BaseException:
        await settle("audio_probe_exception")
        raise
    if finish_reasons:
        result["finishReasons"] = sorted(finish_reasons)
    return result
