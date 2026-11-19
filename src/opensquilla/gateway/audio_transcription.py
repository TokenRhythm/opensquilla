"""HTTP route for WebUI microphone transcription."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.audio.stt_registry import SttConfigError, build_stt_chain
from opensquilla.audio.stt_types import SttAudio, SttProvider, SttResult
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import forbidden_origin_response, request_origin_allowed
from opensquilla.gateway.uploads import _authorization_token_matches

_MAX_TRANSCRIPTION_BYTES = 30 * 1024 * 1024


def _default_stt_chain(config: GatewayConfig) -> Sequence[SttProvider]:
    return build_stt_chain(config)


def register_audio_transcription_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    stt_chain_factory: Callable[[GatewayConfig], Sequence[SttProvider]] = _default_stt_chain,
    usage_event_sink: Any | None = None,
    session_manager: Any | None = None,
) -> None:
    """Register POST /api/audio/transcribe for browser-recorded audio.

    Providers are tried in configured order (``[audio.stt].provider`` plus
    ``fallback``); the first success wins.
    """

    async def transcribe_handler(request: Request) -> JSONResponse:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and not _authorization_token_matches(config, request):
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer ...) required for "
                            "/api/audio/transcribe"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )

        if not getattr(config.audio, "enabled", False):
            return JSONResponse(
                {"error": "audio transcription is disabled", "code": "UNAVAILABLE"},
                status_code=503,
            )

        try:
            chain = list(stt_chain_factory(config))
        except SttConfigError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "STT_PROVIDER_MISCONFIGURED"},
                status_code=503,
            )
        if not chain:
            return JSONResponse(
                {
                    "error": "no speech-to-text provider configured",
                    "code": "STT_PROVIDER_MISCONFIGURED",
                },
                status_code=503,
            )

        try:
            form = await request.form()
        except Exception as exc:
            return JSONResponse(
                {"error": f"multipart/form-data required: {exc}"}, status_code=400
            )

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing 'file' multipart field"}, status_code=400)

        filename = getattr(upload, "filename", None) or "voice.webm"
        mime_type = getattr(upload, "content_type", None) or form.get("mime") or "audio/webm"
        if not isinstance(mime_type, str) or not mime_type.startswith(("audio/", "video/")):
            return JSONResponse(
                {"error": "audio or video upload required", "code": "UNSUPPORTED_MEDIA_TYPE"},
                status_code=415,
            )

        payload = await upload.read()
        if not isinstance(payload, bytes) or len(payload) == 0:
            return JSONResponse({"error": "empty upload"}, status_code=400)
        if len(payload) > _MAX_TRANSCRIPTION_BYTES:
            return JSONResponse(
                {
                    "error": "audio upload exceeds transcription size limit",
                    "code": "TOO_LARGE",
                },
                status_code=413,
            )

        model_value = form.get("model_id")
        request_model_id = str(model_value).strip() if isinstance(model_value, str) else ""
        language_value = form.get("language_code")
        global_language = str(getattr(config.audio.stt, "language", "") or "").strip()
        # Hint resolution: per-request form value → global [audio.stt].language
        # → None (providers apply their own default).
        form_language = (
            language_value.strip()
            if isinstance(language_value, str) and language_value.strip()
            else None
        )
        language = form_language or global_language or None

        from opensquilla.engine.usage_accounting import (
            UsageAccountingScope,
            UsageExecutionContext,
            bind_usage_accounting_scope,
        )
        from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

        # Resolve the recording owner before any provider call. A pre-turn
        # recording has a durable draft identity rather than a fabricated turn.
        owner = form.get("session_key")
        scope = None
        if usage_event_sink is not None:
            if isinstance(owner, str) and owner:
                scope = await build_session_usage_scope(
                    usage_event_sink,
                    session_manager,
                    owner,
                    run_kind="audio_transcription",
                )
                expected_id = form.get("session_id")
                expected_epoch = form.get("session_epoch")
                if (
                    scope is None
                    or not scope.context.session_id
                    or expected_id != scope.context.session_id
                    or expected_epoch != str(scope.context.session_epoch)
                ):
                    return JSONResponse(
                        {"error": "recording session is unavailable", "code": "INVALID_SESSION"},
                        status_code=409,
                    )
                scope = UsageAccountingScope(
                    sink=scope.sink,
                    context=replace(scope.context, turn_id=None),
                )
            else:
                execution_id = uuid.uuid4().hex
                scope = UsageAccountingScope(
                    sink=usage_event_sink,
                    context=UsageExecutionContext(
                        execution_id=execution_id,
                        agent_run_id=execution_id,
                        session_id=f"usage-draft:{uuid.uuid4().hex}",
                        run_kind="audio_transcription",
                        agent_id="main",
                    ),
                )

        audio = SttAudio(data=payload, filename=str(filename), mime_type=mime_type)
        errors: list[str] = []
        result: SttResult | None = None
        for provider in chain:
            accepts_model = bool(getattr(provider, "accepts_request_model", False))
            model_id_arg = (request_model_id or None) if accepts_model else None
            try:
                with bind_usage_accounting_scope(scope):
                    candidate = await provider.transcribe(
                        audio,
                        model_id=model_id_arg,
                        language=language,
                    )
            except Exception as exc:
                errors.append(str(exc) if len(chain) == 1 else f"{provider.provider_id}: {exc}")
                continue
            # A provider that answers with empty text (silent capture, or a
            # local model's punctuation artifact) is a miss, not a result:
            # try the next link in the chain before returning nothing.
            if not (candidate.text or "").strip():
                errors.append(f"{provider.provider_id}: empty transcription")
                continue
            result = candidate
            break
        if result is None:
            if errors and all("empty transcription" in error for error in errors):
                # Not a failure — nothing was said. Keep the response shape so
                # the WebUI can distinguish it and the composer stays clean.
                return JSONResponse(
                    {
                        "text": "",
                        "provider": errors[0].split(":", 1)[0].strip(),
                        "model": "",
                    }
                )
            return JSONResponse(
                {"error": "; ".join(errors) or "transcription failed", "code": "PROVIDER_ERROR"},
                status_code=502,
            )

        response: dict[str, Any] = {
            "text": result.text,
            "provider": result.provider,
            "model": result.model,
        }
        if scope is not None:
            response["usage_owner_id"] = scope.context.session_id
        if result.language_code is not None:
            response["language_code"] = result.language_code
        if result.language_probability is not None:
            response["language_probability"] = result.language_probability
        return JSONResponse(response)

    app.router.routes.append(Route("/api/audio/transcribe", transcribe_handler, methods=["POST"]))

    async def stt_models_handler(request: Request) -> JSONResponse:
        """List models served by a configured OpenAI-compatible STT endpoint.

        The credential is resolved server-side from ``[audio.stt.<engine>]``
        (inline key or its env reference) and never echoed back; the browser
        only receives model ids. Same origin/token guards as transcription.
        """
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and not _authorization_token_matches(config, request):
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer ...) required for "
                            "/api/audio/stt/models"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )
        draft: dict = {}
        if request.method == "POST":
            try:
                parsed = await request.json()
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                draft = parsed
        engine = str(draft.get("engine") or request.query_params.get("engine") or "").strip()
        if engine not in ("openai_transcriptions", "openai_chat"):
            return JSONResponse(
                {
                    "error": "engine must be openai_transcriptions or openai_chat",
                    "code": "INVALID_ENGINE",
                },
                status_code=400,
            )
        section = getattr(config.audio.stt, engine, None)
        if section is None:
            return JSONResponse({"ok": False, "models": [], "failureKind": "unconfigured"})
        import os

        from opensquilla.audio.stt_registry import resolve_stt_api_key_env

        # Draft credentials (typed in the setup form, not yet saved) win so the
        # operator can fetch models before pressing Save; otherwise fall back to
        # the stored section. Sent via POST body to keep keys out of URLs/logs.
        api_key = str(draft.get("api_key") or "").strip() or str(
            getattr(section, "api_key", "") or ""
        ).strip()
        if not api_key:
            env_name = resolve_stt_api_key_env(section)
            api_key = str(os.environ.get(env_name, "") or "").strip() if env_name else ""
        if not api_key:
            return JSONResponse({"ok": False, "models": [], "failureKind": "missing_key"})
        base_url = (
            str(draft.get("base_url") or "").strip()
            or str(getattr(section, "base_url", "") or "").strip()
            or "https://api.openai.com/v1"
        )
        from opensquilla.onboarding.probe import discover_selectable_provider_models

        try:
            result = await discover_selectable_provider_models(
                provider_id="custom",
                api_key=api_key,
                base_url=base_url,
                allow_default_api_key_env=False,
            )
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            return JSONResponse(
                {
                    "ok": False,
                    "models": [],
                    "failureKind": "discover_failed",
                    "detail": str(exc)[:200],
                }
            )
        models = [
            str(row.get("id"))
            for row in (result.models or [])
            if isinstance(row, dict) and row.get("id")
        ]
        return JSONResponse(
            {
                "ok": bool(result.ok),
                "models": models,
                "failureKind": str(getattr(result, "failure_kind", "") or ""),
            }
        )

    app.router.routes.append(
        Route("/api/audio/stt/models", stt_models_handler, methods=["GET", "POST"])
    )

    # --- Local SenseVoice model preparation (pinned download, DSH-style) ---
    import asyncio

    from opensquilla.audio import sensevoice_assets

    prepare_state: dict[str, Any] = {"phase": "idle"}
    prepare_task: asyncio.Task | None = None
    prepare_cancel = asyncio.Event()

    def _prepare_guard(request: Request) -> JSONResponse | None:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and not _authorization_token_matches(config, request):
                return JSONResponse(
                    {"error": "Authorization header (Bearer ...) required", "code": "UNAUTHORIZED"},
                    status_code=401,
                )
        return None

    async def _run_prepare(model_dir: Any) -> None:
        nonlocal prepare_state
        prepare_cancel.clear()

        def report(resource: str, done: int, total: int) -> None:
            nonlocal prepare_state
            prepare_state = {
                "phase": "downloading",
                "resource": resource,
                "completedBytes": done,
                "totalBytes": total,
                "modelDir": str(model_dir),
            }

        try:
            prepare_state = {"phase": "checking", "modelDir": str(model_dir)}
            await sensevoice_assets.prepare_sensevoice_model(
                model_dir,
                progress=report,
                cancelled=prepare_cancel.is_set,
            )
            prepare_state = {"phase": "ready", "modelDir": str(model_dir)}
        except sensevoice_assets.SenseVoiceDownloadError as exc:
            prepare_state = {
                "phase": "cancelled" if exc.reason == "cancelled" else "failed",
                "modelDir": str(model_dir),
                "error": {"resource": exc.resource, "reason": exc.reason, "status": exc.status},
            }
        except Exception as exc:  # noqa: BLE001 - surface a bounded public reason
            prepare_state = {
                "phase": "failed",
                "modelDir": str(model_dir),
                "error": {"resource": "sensevoice", "reason": "unknown", "detail": str(exc)[:200]},
            }

    async def sensevoice_prepare_handler(request: Request) -> JSONResponse:
        nonlocal prepare_task, prepare_state
        guard = _prepare_guard(request)
        if guard is not None:
            return guard
        if request.method == "GET":
            return JSONResponse(prepare_state)
        if request.method == "DELETE":
            if prepare_task is not None and not prepare_task.done():
                prepare_cancel.set()
            return JSONResponse(prepare_state)
        # POST: start (idempotent while a preparation is already running).
        if prepare_task is not None and not prepare_task.done():
            return JSONResponse(prepare_state)
        model_dir = sensevoice_assets.default_model_dir(config.config_path)
        if sensevoice_assets.model_ready(model_dir):
            prepare_state = {"phase": "ready", "modelDir": str(model_dir)}
            return JSONResponse(prepare_state)
        # Publish "checking" synchronously so the caller's first response already
        # reflects an active preparation and begins polling progress.
        prepare_state = {"phase": "checking", "modelDir": str(model_dir)}
        prepare_task = asyncio.create_task(_run_prepare(model_dir))
        return JSONResponse(prepare_state)

    app.router.routes.append(
        Route(
            "/api/audio/stt/sensevoice/prepare",
            sensevoice_prepare_handler,
            methods=["GET", "POST", "DELETE"],
        )
    )
