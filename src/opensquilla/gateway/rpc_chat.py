"""RPC handlers for the chat domain — wired to sessions engine bridge."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import structlog

from opensquilla.chat.conversation import ChatSendRequest, sessions_send_params
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.chat.source import chat_source_metadata
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.context_overflow import apply_context_overflow_policy
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_lock
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import build_compaction_config_from_provider
from opensquilla.session.compaction_lifecycle import new_compaction_id
from opensquilla.session.keys import build_webchat_key, canonicalize_session_key, parse_agent_id

_d = get_dispatcher()
log = structlog.get_logger(__name__)

_WEBCHAT_SESSION_KEY = build_webchat_key()
_CHAT_HISTORY_DEFAULT_LIMIT = 50
_CHAT_HISTORY_MAX_LIMIT = 200


def _canonical_webchat_session_key(value: object = None) -> str:
    """Map legacy WebChat defaults onto the canonical WebChat session."""
    raw = str(value or "").strip()
    if not raw or raw in {"default", "webchat:default", "unknown"}:
        return _WEBCHAT_SESSION_KEY
    if raw.startswith("sess-"):
        return f"agent:main:webchat:{raw[len('sess-') :]}"
    return canonicalize_session_key(raw)


def _require_chat_session_manager(ctx: RpcContext):
    if ctx.session_manager is None:
        raise RpcUnavailableError("Chat session manager not available")
    return ctx.session_manager


def _normalize_chat_history_limit(value: object) -> int:
    try:
        if isinstance(value, int):
            limit = value
        elif isinstance(value, str):
            limit = int(value)
        else:
            limit = _CHAT_HISTORY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _CHAT_HISTORY_DEFAULT_LIMIT
    return max(1, min(limit, _CHAT_HISTORY_MAX_LIMIT))


def _is_webchat_session_key(key: str) -> bool:
    parts = str(key or "").split(":")
    return (
        len(parts) == 4
        and parts[0] == "agent"
        and bool(parts[1])
        and parts[2] == "webchat"
        and bool(parts[3])
    )


def _empty_chat_history_payload(limit: int) -> dict[str, Any]:
    return {
        "messages": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "history_scope": "complete",
        "loaded_count": 0,
        "page_size": limit,
        "canonical_available": False,
        # A missing WebChat key has an empty but complete transcript. Keep
        # canonical_available's compatibility meaning while distinguishing this
        # normal state from a temporary reader failure or lost legacy archive.
        "canonical_complete": True,
        "compaction_summaries": [],
    }


def _chat_history_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _chat_history_cursor(entry: object | None) -> str | None:
    if entry is None:
        return None
    created_at = getattr(entry, "created_at", "")
    stable_id = getattr(entry, "id", None) or getattr(entry, "message_id", "")
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    return f"{created_at}|{stable_id}"


def _chat_history_cursor_index(entries: list[object], cursor: object) -> int | None:
    raw = str(cursor or "").strip()
    if not raw:
        return None
    for idx, entry in enumerate(entries):
        if _chat_history_cursor(entry) == raw:
            return idx
    return None


def _chat_history_cursor_key(cursor: object) -> tuple[int, int] | None:
    raw = str(cursor or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, stable_id = raw.split("|", 1)
    try:
        return int(created_at), int(stable_id)
    except ValueError:
        return None


def _chat_history_page(
    entries: list[object],
    *,
    limit: int,
    before: object = None,
    after: object = None,
) -> tuple[list[object], bool]:
    if not entries:
        return [], False
    before_idx = _chat_history_cursor_index(entries, before)
    if before_idx is not None:
        end = before_idx
        start = max(0, end - limit)
        return entries[start:end], start > 0

    after_idx = _chat_history_cursor_index(entries, after)
    if after_idx is not None:
        start = min(len(entries), after_idx + 1)
        end = min(len(entries), start + limit)
        return entries[start:end], end < len(entries)

    if len(entries) <= limit:
        return entries, False
    return entries[-limit:], True


def _session_summary_to_chat_payload(summary: object) -> dict[str, Any]:
    return {
        "id": getattr(summary, "id", None),
        "compaction_id": getattr(summary, "compaction_id", None),
        "compaction_index": getattr(summary, "compaction_index", None),
        "trigger_reason": getattr(summary, "trigger_reason", None),
        "summary_text": getattr(summary, "summary_text", "") or "",
        "summary_format": getattr(summary, "summary_format", "") or "",
        "coverage_status": getattr(summary, "coverage_status", "") or "",
        "removed_count": getattr(summary, "removed_count", None),
        "kept_count": getattr(summary, "kept_count", None),
        "covered_through_id": getattr(summary, "covered_through_id", None),
        "created_at": getattr(summary, "created_at", None),
    }


def _annotate_transcript_attachment_downloads(
    messages: list[dict[str, Any]],
    *,
    session_key: str,
) -> list[dict[str, Any]]:
    session_qs = quote(session_key, safe="")
    for msg in messages:
        attachments = msg.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            sha = attachment.get("sha256_ref")
            if not isinstance(sha, str) or not sha:
                continue
            if attachment.get("download_url"):
                continue
            name = str(attachment.get("name") or "attachment")
            mime = str(attachment.get("mime") or attachment.get("type") or "")
            attachment["download_url"] = (
                f"/api/v1/attachments/{quote(sha, safe='')}?sessionKey={session_qs}"
                f"&name={quote(name, safe='')}&mime={quote(mime, safe='')}"
            )
    return messages


def _canonical_page_parts(page: object) -> tuple[list[object], bool, bool]:
    if isinstance(page, dict):
        entries = page.get("entries")
        has_more = page.get("has_more", False)
        canonical_complete = page.get("canonical_complete", True)
    elif isinstance(page, tuple):
        entries = page[0] if page else None
        has_more = page[1] if len(page) > 1 else False
        canonical_complete = page[2] if len(page) > 2 else True
    else:
        entries = getattr(page, "entries", None)
        has_more = getattr(page, "has_more", False)
        canonical_complete = getattr(page, "canonical_complete", True)
    if entries is None:
        raise TypeError("canonical transcript page is missing entries")
    return list(entries), bool(has_more), bool(canonical_complete)


async def _load_chat_history_page(
    mgr: object,
    session_key: str,
    *,
    limit: int,
    before: object = None,
    after: object = None,
    include_canonical: bool,
) -> tuple[list[object], bool, bool, bool]:
    if include_canonical:
        page_getter = getattr(mgr, "get_canonical_transcript_page", None)
        if callable(page_getter):
            try:
                page = await page_getter(
                    session_key,
                    limit=limit,
                    before=_chat_history_cursor_key(before),
                    after=_chat_history_cursor_key(after),
                )
                entries, has_more, canonical_complete = _canonical_page_parts(page)
                return entries, has_more, True, canonical_complete
            except Exception:  # noqa: BLE001 - fall back to active transcript
                pass
        else:
            getter = getattr(mgr, "get_canonical_transcript", None)
            if callable(getter):
                try:
                    transcript = list(await getter(session_key))
                    entries, has_more = _chat_history_page(
                        transcript,
                        limit=limit,
                        before=before,
                        after=after,
                    )
                    return entries, has_more, True, True
                except Exception:  # noqa: BLE001 - fall back to active transcript
                    pass
    transcript_getter = getattr(mgr, "get_transcript", None)
    if not callable(transcript_getter):
        return [], False, False, False
    transcript = await transcript_getter(session_key)
    entries, has_more = _chat_history_page(
        list(transcript or []),
        limit=limit,
        before=before,
        after=after,
    )
    return entries, has_more, False, False


async def _chat_history_summaries(
    mgr: object,
    session_key: str,
    *,
    include_summaries: bool,
) -> list[dict[str, Any]]:
    if not include_summaries:
        return []
    getter = getattr(mgr, "get_summaries", None)
    if not callable(getter):
        return []
    try:
        summaries = await getter(session_key)
    except Exception:  # noqa: BLE001 - summaries are optional display metadata
        return []
    return [_session_summary_to_chat_payload(summary) for summary in summaries or []]


def _effective_compaction_model(session: object | None) -> str | None:
    if session is None:
        return None
    return getattr(session, "model_override", None) or getattr(session, "model", None)


def _resolve_compaction_provider(ctx: RpcContext, session: object | None) -> object | None:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return None

    resolved_selector = selector
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            resolved_selector = clone()
        except Exception:  # noqa: BLE001
            resolved_selector = selector

    model = _effective_compaction_model(session)
    if model and resolved_selector is not selector:
        override = getattr(resolved_selector, "override_model", None)
        if callable(override):
            try:
                override(model)
            except Exception:  # noqa: BLE001
                pass

    resolver = getattr(resolved_selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return cast(object | None, resolver())
    except Exception:  # noqa: BLE001
        return None


async def _build_context_overflow_compaction_config(ctx: RpcContext, session_key: str):
    session = None
    storage = getattr(getattr(ctx, "session_manager", None), "_storage", None)
    if storage is not None:
        try:
            session = await storage.get_session(session_key)
        except Exception:  # noqa: BLE001
            session = None
    return build_compaction_config_from_provider(
        _resolve_compaction_provider(ctx, session),
        model_override=_effective_compaction_model(session),
        compaction_config=getattr(getattr(ctx, "config", None), "compaction", None),
    )


async def _enforce_context_overflow(
    ctx: RpcContext,
    session_key: str,
    message: str,
) -> dict | None:
    """Apply the configured context-overflow policy before a turn runs.

    Returns a stable error envelope when the policy is REFUSE and the
    payload exceeds the budget; returns ``None`` for every other path
    (policy consults pass, HARD_TRUNCATE dropped some history in place,
    AUTO_SUMMARIZE kicked off a compaction). The caller short-circuits
    on a non-None return.
    """

    config = ctx.config if isinstance(ctx.config, GatewayConfig) else GatewayConfig()

    transcript: list = []
    if ctx.session_manager is not None:
        try:
            transcript = list(await ctx.session_manager.get_transcript(session_key))
        except Exception:  # noqa: BLE001 — missing transcript just means "no history"
            transcript = []

    # Per-session context-budget overrides are independent from runtime/request
    # timeout resolution, which happens in TurnRunner.
    # A session-scoped context_budget_tokens override is supported via
    # ctx.session_manager.get_config(session_key) if present.
    budget_override = None
    policy_override = None
    if ctx.session_manager is not None and hasattr(ctx.session_manager, "get_session_config"):
        try:
            session_cfg = await ctx.session_manager.get_session_config(session_key)
            if session_cfg is not None:
                budget_override = getattr(session_cfg, "context_budget_tokens", None)
                policy_override = getattr(session_cfg, "context_overflow_policy", None)
        except Exception:  # noqa: BLE001
            pass

    from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
    from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope

    usage_scope = await build_session_usage_scope(
        getattr(ctx, "usage_event_sink", None),
        ctx.session_manager,
        session_key,
        run_kind="session_compaction",
    )
    root_operation_id = new_compaction_id()
    provider_request_correlation = None
    if not provider_request_correlation_disabled(config=config):
        try:
            session = await ctx.session_manager.get_session(session_key)
        except Exception:  # noqa: BLE001 - observability is best-effort
            session = None
        durable_session_id = getattr(session, "session_id", None)
        if isinstance(durable_session_id, str) and durable_session_id:
            provider_request_correlation = ProviderRequestCorrelation(
                session_id=durable_session_id,
                turn_id=root_operation_id,
                execution_id=uuid4().hex,
                call_kind="auxiliary.compaction",
            )
    with bind_usage_accounting_scope(usage_scope):
        outcome = await apply_context_overflow_policy(
            config=config,
            message=message,
            transcript=transcript,
            session_key=session_key,
            session_manager=ctx.session_manager,
            compaction_config=await _build_context_overflow_compaction_config(
                ctx, session_key
            ),
            flush_service=getattr(ctx, "flush_service", None),
            compaction_marker=getattr(ctx, "turn_runner", None),
            policy_override=policy_override,
            budget_override=budget_override,
            provider_request_correlation=provider_request_correlation,
            root_operation_id=root_operation_id,
        )

    if outcome.refusal is not None:
        log.warning(
            "chat_send.context_overflow_refused",
            session_key=session_key,
            estimated_tokens=outcome.estimated_tokens,
            budget_tokens=outcome.budget_tokens,
        )
        return outcome.refusal

    if outcome.compacted_this_turn:
        marker = getattr(ctx, "turn_runner", None)
        mark = getattr(marker, "mark_compacted_this_turn", None)
        if callable(mark):
            mark(session_key)

    return None


@_d.method("chat.send", scope="operator.write")
async def _handle_chat_send(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")

    message = params["message"]
    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    agent_id = parse_agent_id(session_key)

    # Fresh-WebUI / smoke path: when no session manager is wired (webui
    # simulator, dispatcher-only boot), instant-accept without kicking off a
    # turn. This matches the roundtrip the WebUI observes on first paint
    # before the sessions engine is attached.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "instant_accept": True}

    mgr = _require_chat_session_manager(ctx)
    intent = params.get("intent")
    intent_was_provided = intent is not None
    requested_intent = intent

    # WebChat must accept the turn even when existing history is oversized.
    # Context shaping happens inside TurnRunner so it can produce a request-scoped
    # sendable view instead of making the RPC layer a terminal overflow gate.

    try:
        if intent != "new_chat":
            # Detect a draft without creating it yet. sessions.send folds the
            # session row into the same durable acceptance transaction as the
            # first message/task/receipt.
            storage = getattr(mgr, "storage", None) or getattr(mgr, "_storage", None)
            get_session = getattr(storage, "get_session", None)
            if callable(get_session):
                try:
                    if await get_session(session_key) is None:
                        intent = "new_chat"
                except Exception as exc:
                    raise RpcUnavailableError(
                        f"Failed to inspect chat session: {exc}"
                    ) from exc
            else:
                # Compatibility for minimal test/simulator managers that do
                # not expose storage: retain the historical initializer.
                try:
                    await mgr.get_or_create(
                        session_key=session_key,
                        agent_id=agent_id,
                        display_name="WebChat",
                    )
                except Exception as exc:
                    raise RpcUnavailableError(
                        f"Failed to initialize chat session: {exc}"
                    ) from exc

        from opensquilla.gateway.rpc_sessions import _handle_sessions_send

        incoming_source = params.get("_source")
        if not isinstance(incoming_source, dict):
            incoming_source = {}

        elevated_hint = incoming_source.get("elevated")
        run_mode_hint = incoming_source.get("runMode") or incoming_source.get("run_mode")
        attachments = params.get("attachments")
        extra: dict = {}
        for source_key, target_key in (
            ("noMemoryCapture", "noMemoryCapture"),
            ("no_memory_capture", "no_memory_capture"),
            ("inputProvenance", "inputProvenance"),
            ("input_provenance", "input_provenance"),
            ("inputProvenanceKind", "inputProvenanceKind"),
            ("input_provenance_kind", "input_provenance_kind"),
            ("provenance_kind", "provenance_kind"),
            ("runKind", "runKind"),
            ("run_kind", "run_kind"),
            ("queueMode", "queueMode"),
            ("queue_mode", "queue_mode"),
            ("forkBeforeMessageId", "forkBeforeMessageId"),
            ("fork_before_message_id", "fork_before_message_id"),
            ("clientRequestId", "clientRequestId"),
            ("client_request_id", "client_request_id"),
            ("clientMessageId", "clientMessageId"),
            ("client_message_id", "client_message_id"),
            ("surfaceId", "surfaceId"),
            ("surface_id", "surface_id"),
        ):
            if source_key in params:
                extra[target_key] = params[source_key]
        send_params = sessions_send_params(
            ChatSendRequest(
                session_key=session_key,
                message=message,
                attachments=attachments if isinstance(attachments, list) else [],
                display_text=params.get("displayText") if "displayText" in params else None,
                intent=cast(str, intent) if intent is not None else None,
                extra=extra,
            ),
            chat_source_metadata(
                caller_kind="web",
                channel_kind="webchat",
                channel_id=f"webchat:{session_key}",
                sender_id=ctx.principal.role,
                source_kind="webui",
                source_name="WebChat",
                elevated=elevated_hint if isinstance(elevated_hint, str) else None,
                run_mode=run_mode_hint if isinstance(run_mode_hint, str) else None,
            ),
        )
        # Keep the public handler params free of fingerprint-control fields.
        # The logical request fingerprint uses the caller's original intent,
        # while the actual send may use the internal ``continue`` ->
        # ``new_chat`` strategy to create a first session atomically.
        fingerprint_params = dict(send_params)
        if intent_was_provided:
            fingerprint_params["intent"] = requested_intent
        else:
            fingerprint_params.pop("intent", None)
        result = await _handle_sessions_send(
            send_params,
            ctx,
            fingerprint_params=fingerprint_params,
        )
        result_session_key = result.get("sessionKey") or result.get("key") or session_key
        return {"ok": True, "sessionKey": result_session_key, **result}
    except Exception:
        marker = getattr(ctx, "turn_runner", None)
        clear = getattr(marker, "clear_compacted_this_turn", None)
        if callable(clear):
            clear(session_key)
        raise


@_d.method("chat.abort", scope="operator.write")
async def _handle_chat_abort(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    # Fresh-WebUI / smoke path: abort always returns an ok envelope keyed by
    # sessionKey, regardless of whether a live task exists to cancel.
    if ctx.session_manager is None:
        return {"ok": True, "sessionKey": session_key, "aborted": False}
    _require_chat_session_manager(ctx)
    from opensquilla.gateway.rpc_sessions import _handle_sessions_abort

    abort_params = {
        "key": session_key,
        "source": raw_params.get("source") or "webui_abort",
    }
    task_id = raw_params.get("taskId") or raw_params.get("task_id")
    source = str(abort_params["source"])
    if source != "webui_stop" and isinstance(task_id, str) and task_id.strip():
        abort_params["task_id"] = task_id.strip()
    result = await _handle_sessions_abort(
        abort_params,
        ctx,
    )
    return {"sessionKey": session_key, **result}


@_d.method("chat.history", scope="operator.read")
async def _handle_chat_history(params: dict | None, ctx: RpcContext) -> dict:
    raw_params = params or {}
    session_key = _canonical_webchat_session_key(raw_params.get("sessionKey"))
    limit = _normalize_chat_history_limit(raw_params.get("limit"))
    before = raw_params.get("before")
    after = raw_params.get("after")
    include_canonical = _chat_history_bool(
        raw_params.get("includeCanonical"),
        default=True,
    )
    include_summaries = _chat_history_bool(
        raw_params.get("includeSummaries"),
        default=True,
    )

    mgr = _require_chat_session_manager(ctx)

    async def _load_page() -> tuple[list[object], bool, bool, bool]:
        return await _load_chat_history_page(
            mgr,
            session_key,
            limit=limit,
            before=before,
            after=after,
            include_canonical=include_canonical,
        )

    try:
        history_lock = get_session_lock(ctx.turn_runner, session_key)
        if history_lock is None:
            page_entries, has_more, canonical_available, canonical_complete = (
                await _load_page()
            )
        else:
            # Canonical reads and compaction rewrites share one aiosqlite
            # connection.  SQLite statements are snapshots, but a statement on
            # that same connection can still observe the connection's own
            # uncommitted archive/delete/reinsert work.  Use the short session
            # mutation lock so the page and its coverage metadata are read only
            # before or after a rewrite, never from its intermediate state.
            async with history_lock:
                page_entries, has_more, canonical_available, canonical_complete = (
                    await _load_page()
                )
    except KeyError:
        if _is_webchat_session_key(session_key):
            return _empty_chat_history_payload(limit)
        raise
    summaries = await _chat_history_summaries(
        mgr,
        session_key,
        include_summaries=include_summaries,
    )
    if summaries:
        history_scope = "compacted"
    elif has_more:
        history_scope = "latest_window"
    else:
        history_scope = "complete"

    messages = transcript_entries_to_chat_messages(page_entries, limit=None)
    return {
        "messages": _annotate_transcript_attachment_downloads(
            messages,
            session_key=session_key,
        ),
        "has_more": has_more,
        "oldest_cursor": _chat_history_cursor(page_entries[0]) if page_entries else None,
        "newest_cursor": _chat_history_cursor(page_entries[-1]) if page_entries else None,
        "history_scope": history_scope,
        "loaded_count": len(page_entries),
        "page_size": limit,
        "canonical_available": canonical_available,
        "canonical_complete": canonical_complete,
        "compaction_summaries": summaries,
    }


def _clarify_fields_to_text(fields: dict[str, object]) -> str:
    """Serialise a clarify-form submission into a ``key: value\\n`` reply.

    The synthetic message is fed back through ``chat.send`` so it
    traverses the regular meta-resolution pipeline:
      peek_awaiting → parse_clarify_reply (key:value mode) →
      try_claim_resume → DAG continues.

    Bools are rendered as ``true``/``false``; everything else uses
    Python's natural string representation. Empty / None values are
    skipped — they signal "optional field omitted".
    """
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


@_d.method("chat.clarify_submit", scope="operator.write")
async def _handle_chat_clarify_submit(params: dict | None, ctx: RpcContext) -> dict:
    """Accept a structured clarify-form submission from a Web UI surface.

    Params:
      ``sessionKey``  (str)  — same WebChat session that triggered the pause
      ``fields``      (dict) — ``{field_name: value}`` collected by the form
      ``run_id``      (str, optional) — awaiting run id for trace/log only;
                                          the awaiting branch in meta_resolution
                                          uses ``session_key`` for the CAS

    The fields dict is serialised into ``key: value\\n`` text and fed
    into the regular ``chat.send`` path so meta_resolution's awaiting
    branch picks it up. This is purely a convenience surface — clients
    can equivalently call ``chat.send`` with the same text.
    """
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, fields")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("params.fields must be a non-empty mapping")

    session_key = _canonical_webchat_session_key(params.get("sessionKey"))
    text = _clarify_fields_to_text(fields)

    run_id = params.get("run_id")
    log.info(
        "chat.clarify_submit.params",
        session_key=session_key,
        field_count=len(fields),
        run_id=run_id if isinstance(run_id, str) and run_id else None,
    )

    send_params: dict = {
        "message": text,
        "sessionKey": session_key,
        # meta_resolution's awaiting branch keys off session_key, not
        # intent — so we deliberately stay on the default "continue"
        # intent (SessionIntent enum rejects unknown values). The
        # provenance tag is the observability hook for distinguishing
        # form submits from typed replies downstream.
        "inputProvenance": {"kind": "clarify_form", "source": "webui"},
    }
    if isinstance(run_id, str) and run_id:
        send_params["_source"] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
            "channel_id": f"webchat:{session_key}",
            "source_kind": "webui",
            "source_name": "WebChat",
            "clarify_run_id": run_id,
        }
    result = await _handle_chat_send(send_params, ctx)
    return cast(dict, result)


@_d.method("chat.inject", scope="operator.admin")
async def _handle_chat_inject(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: sessionKey, role, content")
    for field in ("sessionKey", "role", "content"):
        if field not in params:
            raise ValueError(f"params.{field} is required")

    role = params["role"]
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role}")

    session_key = _canonical_webchat_session_key(params["sessionKey"])

    if ctx.session_manager is None:
        raise KeyError("No session manager available")

    storage = getattr(ctx.session_manager, "_storage", None)
    if storage is not None:
        existing = await storage.get_session(session_key)
        if existing is None:
            raise KeyError(f"Session not found: {session_key}")

    await ctx.session_manager.append_message(session_key, role=role, content=params["content"])
    return {"ok": True, "sessionKey": session_key}
