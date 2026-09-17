#!/usr/bin/env python3
"""OpenRouter audio entrypoint for meta-skill ``skill_exec`` steps."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import wave
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from opensquilla.skills.bundled._provider_http import (
    ProviderHTTPError,
    iter_limited_response_chunks,
    open_authenticated_request,
)

SAFE_NO_SUBMIT_EXIT_CODE = 78
META_CAPABILITY_LEASE_REQUIRED_ENV = "OPENSQUILLA_META_CAPABILITY_LEASE_REQUIRED"
META_CAPABILITY_PROVIDER_ENV = "OPENSQUILLA_META_CAPABILITY_PROVIDER"
META_CAPABILITY_API_KEY_ENV = "OPENSQUILLA_META_CAPABILITY_API_KEY"
META_CAPABILITY_BASE_URL_ENV = "OPENSQUILLA_META_CAPABILITY_BASE_URL"
META_CAPABILITY_PROXY_ENV = "OPENSQUILLA_META_CAPABILITY_PROXY"

SAMPLE_RATE = 24_000
MAX_AUDIO_SSE_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_AUDIO_SSE_LINE_BYTES = 16 * 1024 * 1024
MAX_AUDIO_SSE_EVENT_BYTES = 16 * 1024 * 1024
MAX_AUDIO_PCM_BYTES = 64 * 1024 * 1024


def _safe_filename(value: str, default: str) -> str:
    name = Path(value or default).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not name:
        name = default
    if not name.lower().endswith(".wav"):
        name = re.sub(r"\.[A-Za-z0-9]+$", "", name) + ".wav"
    return name


def _preview(text: str) -> str:
    return " ".join(text.split())[:80]


def _clean_script(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _audio_messages(raw: str) -> tuple[list[dict[str, str]], str]:
    text = raw.strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            script = _clean_script(
                payload.get("script")
                or payload.get("transcript")
                or payload.get("narration")
                or payload.get("text")
            )
            if script:
                return (
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a text-to-speech renderer. Return and speak exactly the "
                                "provided narration transcript. Do not acknowledge the request. "
                                "Do not say you understand. Do not add introductions, titles, "
                                "stage directions, markdown, file names, or closing remarks."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Speak this exact narration transcript and no other words:\n\n"
                                + script
                            ),
                        },
                    ],
                    script,
                )

    prompt = text or "Create a short, clear narration for this webpage."
    return (
        [
            {
                "role": "system",
                "content": (
                    "You produce finished webpage narration audio. Respond only with the "
                    "spoken narration itself. Never acknowledge the request, never say "
                    "you understand, and never describe what you will create."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        prompt,
    )


def _print_record(label: str, payload: dict[str, object]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")


def _failure(label: str, filename: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "replacement_slot": f"project/assets/audio/{filename}",
    }
    payload.update(extra)
    _print_record(label, payload)


def _runtime_connection(args: argparse.Namespace) -> tuple[str, str, str, bool]:
    """Resolve a MetaSkill lease or the direct-CLI compatibility inputs."""

    lease_required = os.environ.get(META_CAPABILITY_LEASE_REQUIRED_ENV) == "1"
    if lease_required:
        provider = os.environ.get(META_CAPABILITY_PROVIDER_ENV, "").strip().lower()
        if provider != "openrouter":
            return "", "", "", True
        return (
            os.environ.get(META_CAPABILITY_API_KEY_ENV, "").strip(),
            os.environ.get(META_CAPABILITY_BASE_URL_ENV, "").strip().rstrip("/"),
            os.environ.get(META_CAPABILITY_PROXY_ENV, "").strip(),
            True,
        )
    api_key_env = args.api_key_env.strip() or "OPENROUTER_API_KEY"
    return (
        str(args.api_key.strip() or os.environ.get(api_key_env, "")),
        args.base_url.strip().rstrip("/"),
        "",
        False,
    )


def _open_url(request: Request, *, timeout: float, proxy: str):
    return open_authenticated_request(
        request,
        timeout=timeout,
        proxy=proxy,
    )


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, URLError):
        return exc.reason.__class__.__name__
    return exc.__class__.__name__


def _iter_bounded_sse_lines(response: object) -> Iterator[bytes]:
    """Split a provider stream into lines without ever asking for a huge line."""

    pending = bytearray()
    chunks = iter_limited_response_chunks(
        response,
        max_bytes=MAX_AUDIO_SSE_RESPONSE_BYTES,
        error_message="provider audio response exceeds size limit",
    )
    for chunk in chunks:
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            end = len(chunk) if newline < 0 else newline
            segment = chunk[cursor:end]
            if len(pending) + len(segment) > MAX_AUDIO_SSE_LINE_BYTES:
                raise ProviderHTTPError("provider audio SSE line exceeds size limit")
            pending.extend(segment)
            if newline < 0:
                break
            line = bytes(pending)
            pending.clear()
            yield line[:-1] if line.endswith(b"\r") else line
            cursor = newline + 1
    if pending:
        yield bytes(pending)


def _iter_sse_data_events(response: object) -> Iterator[bytes]:
    """Yield SSE data payloads while bounding every line and complete event."""

    event = bytearray()
    has_data = False
    for line in _iter_bounded_sse_lines(response):
        if not line:
            if has_data:
                yield bytes(event)
            event.clear()
            has_data = False
            continue
        if line.startswith(b":"):
            continue
        if line == b"data":
            data = b""
        elif line.startswith(b"data:"):
            data = line[5:]
            if data.startswith(b" "):
                data = data[1:]
        else:
            continue
        added = len(data) + (1 if has_data else 0)
        if len(event) + added > MAX_AUDIO_SSE_EVENT_BYTES:
            raise ProviderHTTPError("provider audio SSE event exceeds size limit")
        if has_data:
            event.extend(b"\n")
        event.extend(data)
        has_data = True
    if has_data:
        yield bytes(event)


def _iter_sse_audio_chunks(response: object) -> bytes:
    pcm = bytearray()
    for raw_event in _iter_sse_data_events(response):
        payload = raw_event.decode("utf-8", "replace").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            audio = delta.get("audio") or message.get("audio") or {}
            data_b64 = audio.get("data")
            if isinstance(data_b64, str) and data_b64:
                try:
                    chunk = base64.b64decode(data_b64, validate=True)
                except (ValueError, binascii.Error):
                    raise ProviderHTTPError("provider returned invalid audio data") from None
                if len(pcm) + len(chunk) > MAX_AUDIO_PCM_BYTES:
                    raise ProviderHTTPError("provider audio exceeds size limit")
                pcm.extend(chunk)
    return bytes(pcm)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--filename", default="narration.wav")
    parser.add_argument("--voice", default="cedar")
    args = parser.parse_args()

    filename = _safe_filename(args.filename, "narration.wav")
    messages, script_text = _audio_messages(sys.stdin.read())

    key, base_url, proxy, lease_required = _runtime_connection(args)
    missing = []
    if not key:
        missing.append(
            "provider_connection:openrouter"
            if lease_required
            else (args.api_key_env.strip() or "OPENROUTER_API_KEY")
        )
    if not base_url:
        missing.append("provider_endpoint:openrouter")
    if not args.model:
        missing.append("awesome_webpage.openrouter.models.audio_generation")
    if not args.output_dir:
        missing.append("awesome_webpage.output_dir")
    if missing:
        _failure("AUDIO_CONFIG_NEEDED", filename, missing=missing)
        return SAFE_NO_SUBMIT_EXIT_CODE if lease_required else 0

    output_dir = Path(args.output_dir).expanduser()
    output_path = output_dir / filename
    local_path = f"project/assets/audio/{filename}"
    body = json.dumps(
        {
            "model": args.model,
            "stream": True,
            "modalities": ["text", "audio"],
            "audio": {"voice": args.voice, "format": "pcm16"},
            "messages": messages,
        }
    ).encode("utf-8")
    req = Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    try:
        with _open_url(req, timeout=180, proxy=proxy) as resp:
            pcm = _iter_sse_audio_chunks(resp)
    except HTTPError as exc:
        _failure("AUDIO_GENERATION_FAILED", filename, status=exc.code)
        return 0
    except (URLError, TimeoutError, ProviderHTTPError) as exc:
        _failure("AUDIO_GENERATION_FAILED", filename, reason=_failure_reason(exc))
        return 0

    if not pcm:
        _failure("AUDIO_MODEL_UNSUPPORTED", filename, reason="no_audio_pcm")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)

    _print_record(
        "AUDIO_READY",
        {
            "local_path": local_path,
            "mime": "audio/wav",
            "duration_s": round(len(pcm) / 2 / SAMPLE_RATE, 2),
            "voice": args.voice,
            "script_preview": _preview(script_text),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
