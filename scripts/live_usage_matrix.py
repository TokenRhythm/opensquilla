"""Budgeted, opt-in provider capability/ledger acceptance matrix.

Credentials are supplied on stdin by live_usage_worker, never persisted. The
catalog and report contain only model/accounting metadata, no user content.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import subprocess
import sys
import time
import uuid
import wave
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

ENDPOINTS = {
    "tokenrhythm": "https://tokenrhythm.studio/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

MAX_OUTPUT = 512


def _decimal(value: Any) -> Decimal | None:
    try:
        return Budget._amount(value)
    except (ValueError, TypeError):
        return None


def build_matrix(catalogs: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def add(
        provider: str, row: dict[str, Any], kind: str, bound: Decimal | None, **options: Any
    ) -> None:
        model = str(row["id"])
        reason = None
        if provider == "openrouter":
            price = row.get("effective_pricing") or row.get("pricing") or {}
            # A catalog rate is not proof that every billable quantity is capped.
            # Sonar Deep Research separately bills research reasoning/searches:
            # https://openrouter.ai/perplexity/sonar-deep-research
            internal = _decimal(price.get("internal_reasoning", "0"))
            request_fee = _decimal(price.get("request", "0"))
            all_rates_zero = (
                _decimal(price.get("prompt")) == 0
                and _decimal(price.get("completion")) == 0
                and all(_decimal(value) == 0 for value in price.values())
            )
            if kind == "video":
                # The live Wan 2.7 receipt exceeded rate * requested duration * 2.
                # Generic SKUs do not prove minimum billed duration or job price.
                reason = "video_billed_quantity_bound_unverified"
            elif all_rates_zero:
                # Explicitly free rates need no quantity cap to bound the bill.
                bound = Decimal("0.007") if kind == "web_search" else Decimal(0)
            elif internal is None or internal > 0:
                reason = "internal_reasoning_bound_unverified"
            elif "max_tokens" not in (row.get("supported_parameters") or []):
                reason = "output_token_cap_not_declared"
            elif model == "morph/morph-v3-fast":
                # Live receipt: max_tokens=2048 returned 16384 output tokens.
                reason = "output_token_cap_not_enforced"
            elif (
                model.startswith("perplexity/sonar")
                or ":online" in model
                or (model.startswith("openai/") and "search" in model)
            ):
                reason = "native_search_bound_unverified"
            elif kind == "web_search":
                # The runner pins Exa auto, max_results=1: one plugin execution,
                # $0.007 per request. Bound augmented input by the model's entire
                # context window, including the highest published tier price.
                # https://openrouter.ai/docs/guides/features/plugins/web-search
                window = row.get("context_length")
                tiers = price.get("overrides", [])
                if not isinstance(tiers, list) or any(not isinstance(t, dict) for t in tiers):
                    reason = "search_price_tiers_unavailable"
                else:
                    input_rates = [_decimal(price.get("prompt"))] + [
                        _decimal(t.get("prompt", price.get("prompt"))) for t in tiers
                    ]
                    output_rates = [_decimal(price.get("completion"))] + [
                        _decimal(t.get("completion", price.get("completion"))) for t in tiers
                    ]
                    if (
                        isinstance(window, bool)
                        or not isinstance(window, int)
                        or window <= 0
                        or request_fee is None
                        or any(rate is None for rate in [*input_rates, *output_rates])
                    ):
                        reason = "search_input_token_bound_unverified"
                    else:
                        bound = (
                            max(input_rates) * window
                            + max(output_rates) * MAX_OUTPUT
                            + request_fee
                            + Decimal("0.007")
                        )
            elif request_fee is None:
                reason = "request_fee_unavailable"
            elif bound is not None:
                # One physical dispatch; request pricing is USD per request.
                bound += request_fee
            if reason is not None:
                bound = None
        case_id = hashlib.sha256(f"{provider}:{model}:{kind}".encode()).hexdigest()[:20]
        cases.append(
            {
                "id": case_id,
                "provider": provider,
                "model": model,
                "kind": kind,
                "reservationUsd": None if bound is None else str(bound),
                **({"reservationReason": reason} if reason is not None else {}),
                "options": options,
            }
        )

    for provider in ("tokenrhythm", "openrouter"):
        for row in catalogs.get(provider, {}).get("models", []):
            price = row.get("effective_pricing") or row.get("pricing") or {}
            inp, out = _decimal(price.get("prompt")), _decimal(price.get("completion"))
            if provider == "tokenrhythm" and inp is not None and out is not None:
                inp, out = (
                    inp / Decimal(1_000_000) / Decimal("6.975"),
                    out / Decimal(1_000_000) / Decimal("6.975"),
                )
            bound = None if inp is None or out is None else (inp * 2048 + out * MAX_OUTPUT) * 2
            arch = row.get("architecture") or {}
            outputs = arch.get("output_modalities", ["text"])
            inputs = arch.get("input_modalities", ["text"])
            if "text" in outputs:
                add(provider, row, "text", bound)
            tool_support = row.get("supports_tools") or "tools" in row.get(
                "supported_parameters", []
            )
            vision = row.get("supports_vision") or "image" in inputs
            if tool_support:
                add(
                    provider,
                    row,
                    "tools_vision" if vision else "tools",
                    None
                    if inp is None or out is None
                    else (inp * (8192 if vision else 2048) + out * MAX_OUTPUT) * 2,
                )
            elif vision and "text" in outputs:
                add(
                    provider,
                    row,
                    "vision",
                    None if inp is None or out is None else (inp * 8192 + out * MAX_OUTPUT) * 2,
                )
            if "image" in outputs:
                # A finite output-token bound requires the adapter to expose a cap.
                image_rate = _decimal(price.get("image_output"))
                image_bound = (
                    None
                    if image_rate is None or inp is None or out is None
                    else (image_rate * 8192 + inp * 2048 + out * 8192) * 2
                )
                add(provider, row, "image", image_bound, max_output_tokens=8192)
            if "audio" in inputs:
                audio_rate = _decimal(price.get("audio", price.get("prompt")))
                add(
                    provider,
                    row,
                    "audio_input",
                    None
                    if audio_rate is None or out is None
                    else (audio_rate * 4096 + out * MAX_OUTPUT) * 2,
                )
            if "audio" in outputs:
                audio_rate = _decimal(price.get("audio_output"))
                add(
                    provider,
                    row,
                    "audio_output",
                    None
                    if audio_rate is None or inp is None
                    else (inp * 2048 + audio_rate * 2048) * 2,
                )
            search_rate = _decimal(price.get("web_search"))
            if search_rate is not None and search_rate > 0:
                # Keep the candidate visible even when its total cannot be bounded.
                add(provider, row, "web_search", None)

    for row in catalogs.get("tokenrhythm_public", {}).get("models", []):
        if row.get("type") == "image":
            rate = _decimal(row.get("pricePerImage"))
            add("tokenrhythm", row, "image", None if rate is None else rate / Decimal("6.975") * 2)
        elif row.get("type") == "video":
            rates = (row.get("videoPricing") or {}).get("outputPerSecondCny", {})
            rate = _decimal(rates.get("720P"))
            add(
                "tokenrhythm",
                row,
                "video",
                None if rate is None else rate * 2 / Decimal("6.975") * 2,
                duration=2,
                resolution="720p",
            )

    for row in catalogs.get("openrouter_video", {}).get("models", []):
        durations = row.get("supported_durations") or []
        resolutions = row.get("supported_resolutions") or []
        supported = [r for r in resolutions if r in {"480p", "720p", "1080p", "1K", "2K", "4K"}]
        resolution = (
            min(
                supported,
                key=lambda r: {"480p": 0, "720p": 1, "1080p": 2, "1K": 2, "2K": 3, "4K": 4}[r],
            )
            if supported
            else None
        )
        duration = min(durations) if durations else None
        add("openrouter", row, "video", None, duration=duration, resolution=resolution)
    return cases


def _test_image() -> str:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 0, 0)).save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def error_metadata(payload: Any, key: str) -> dict[str, Any]:
    from scripts.live_harness_security import redact_text

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {}
    result = {}
    for name in ("code", "type", "message"):
        if isinstance(error.get(name), (str, int)):
            result[name] = redact_text(str(error[name]), [key])[:500]
    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        for name in ("error_type", "limit_source", "provider_code"):
            if isinstance(metadata.get(name), (str, int)):
                result[name] = redact_text(str(metadata[name]), [key])[:100]
    return result


async def execute_case(case: dict[str, Any], key: str, output: Path) -> dict[str, Any]:
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        account_provider_stream,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.gateway.usage_query import query_usage_ledger
    from opensquilla.provider.selector import ProviderConfig, _build_provider
    from opensquilla.provider.types import (
        ChatConfig,
        ContentBlockImage,
        ContentBlockText,
        Message,
        ToolDefinition,
    )
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    class ReplaySink(SessionUsageEventSink):
        def __init__(self, storage: Any) -> None:
            super().__init__(storage)
            self.completions: list[Any] = []

        async def finalize(self, call: Any, result: Any) -> None:
            await super().finalize(call, result)
            self.completions.append((call, result))

    provider, model, kind = case["provider"], case["model"], case["kind"]
    db = output / "ledgers" / (case["id"] + ".db")
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = await SessionStorage.open(str(db))
    sink = ReplaySink(storage)
    execution = uuid.uuid4().hex
    root_id, child_id = "root-" + case["id"], "child-" + case["id"]
    root_key = "agent:main:webchat:" + root_id
    await storage.upsert_session(
        SessionNode(
            session_key=root_key, session_id=root_id, display_name="Live billing acceptance"
        )
    )
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:" + child_id,
            session_id=child_id,
            display_name=model,
            parent_session_key=root_key,
            origin={"sidechat": {"parentSessionId": root_id, "parentEpoch": 0}},
        )
    )
    await storage.initialize_usage_ledger(int(time.time() * 1000))
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id=execution,
            agent_run_id=execution,
            turn_id=execution,
            session_id=child_id,
            session_epoch=0,
            agent_id="main",
            run_kind="agent",
        ),
    )
    result: dict[str, Any] = {
        "status": "failed",
        "functional": False,
        "boundary": "production_adapter",
    }
    try:
        with bind_usage_accounting_scope(scope):
            if kind in {"text", "tools", "tools_vision", "vision"}:
                prompt = "Reply with only OK."
                tools = []
                if kind in {"tools", "tools_vision"}:
                    prompt = "Call report_color once with color red. Do not write text."
                    tools = [
                        ToolDefinition(
                            name="report_color",
                            description="Report the color.",
                            input_schema={
                                "type": "object",
                                "properties": {"color": {"type": "string"}},
                                "required": ["color"],
                                "additionalProperties": False,
                            },
                        )
                    ]
                if "vision" in kind:
                    prompt = (
                        "Call report_color with the color of this square."
                        if tools
                        else "Name the color of this square in one word."
                    )
                    content: Any = [
                        ContentBlockText(text=prompt),
                        ContentBlockImage(media_type="image/png", data=_test_image()),
                    ]
                else:
                    content = prompt
                llm = _build_provider(
                    ProviderConfig(
                        provider=provider, model=model, api_key=key, base_url=ENDPOINTS[provider]
                    )
                )
                config = ChatConfig(
                    max_tokens=case.get("options", {}).get("max_output_tokens", MAX_OUTPUT),
                    timeout=60,
                    physical_attempt_limit=1,
                    tool_choice=case.get("options", {}).get("tool_choice", "auto")
                    if tools
                    else None,
                )
                text_length = 0
                response_text = ""
                tool_valid = False
                terminal = False
                async for event in account_provider_stream(
                    lambda: llm.chat([Message(role="user", content=content)], tools, config),
                    provider=provider,
                    model=model,
                ):
                    if event.kind == "text_delta":
                        text_length += len(event.text)
                        response_text += event.text
                    elif event.kind == "tool_use_end":
                        tool_valid = (
                            event.tool_name == "report_color"
                            and str(event.arguments.get("color", "")).lower() == "red"
                        )
                    elif event.kind == "done":
                        terminal = True
                        stop_reason = getattr(event, "stop_reason", None)
                        if isinstance(stop_reason, str):
                            result["stopReason"] = stop_reason[:80]
                    elif event.kind == "error":
                        from scripts.live_harness_security import classify_failure, redact_text

                        error = getattr(event, "error", "") or getattr(event, "message", "")
                        result["failureKind"] = classify_failure(error)
                        result["diagnostic"] = redact_text(error, [key])[:500]
                visible_ok = (
                    ("red" in response_text.lower() or "红" in response_text)
                    if kind == "vision"
                    else text_length > 0
                )
                result.update(
                    functional=terminal and (tool_valid if tools else visible_ok), terminal=terminal
                )
            elif kind == "image":
                from opensquilla.provider.image_generation import (
                    ImageGenerationRequest,
                    OpenRouterImageGenerationProvider,
                    TokenRhythmImageGenerationProvider,
                )

                adapter_cls = (
                    TokenRhythmImageGenerationProvider
                    if provider == "tokenrhythm"
                    else OpenRouterImageGenerationProvider
                )
                request = ImageGenerationRequest(
                    prompt="A single red circle on a plain white background.",
                    model=model,
                    size="1024x1024",
                    timeout_seconds=180,
                )
                if provider == "openrouter":
                    if "max_output_tokens" not in getattr(request, "__dataclass_fields__", {}):
                        raise RuntimeError("Image output budget is not enforceable by this adapter")
                    request.max_output_tokens = case["options"]["max_output_tokens"]
                generated = await adapter_cls(api_key=key, base_url=ENDPOINTS[provider]).generate(
                    request
                )
                result.update(
                    functional=bool(generated.image_bytes), bytes=len(generated.image_bytes)
                )
            elif kind == "video":
                from opensquilla.provider.tokenrhythm_video_generation import (
                    generate_tokenrhythm_video,
                )
                from opensquilla.provider.video_generation import generate_openrouter_video

                generate = (
                    generate_tokenrhythm_video
                    if provider == "tokenrhythm"
                    else generate_openrouter_video
                )
                media_path = output / "media" / (case["id"] + "-" + execution + ".mp4")
                media_path.parent.mkdir(parents=True, exist_ok=True)
                generated = await generate(
                    base_url=ENDPOINTS[provider],
                    api_key=key,
                    model=model,
                    prompt="A red ball rolls slowly across a white table. Static camera.",
                    duration=case["options"]["duration"],
                    resolution=case["options"]["resolution"],
                    aspect_ratio="16:9",
                    output_path=media_path,
                    timeout_seconds=420,
                    poll_interval_seconds=5,
                )
                result.update(
                    functional=generated.bytes_written > 0,
                    bytes=generated.bytes_written,
                    jobId=generated.job_id,
                )
            elif kind in {"audio_input", "audio_output", "web_search"}:
                from opensquilla.provider.metered_http import accounted_request

                body: dict[str, Any] = {
                    "model": model,
                    "stream": False,
                    "max_tokens": case.get("options", {}).get("max_output_tokens", MAX_OUTPUT)
                    if kind in {"audio_input", "web_search"}
                    else MAX_OUTPUT,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                }
                if kind == "audio_input":
                    audio = io.BytesIO()
                    with wave.open(audio, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes(bytes(32000))
                    body["messages"] = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Is this recording silent? Reply in one word.",
                                },
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": base64.b64encode(audio.getvalue()).decode("ascii"),
                                        "format": "wav",
                                    },
                                },
                            ],
                        }
                    ]
                elif kind == "audio_output":
                    body.update(
                        modalities=["text", "audio"],
                        audio={"voice": "alloy", "format": "wav"},
                        max_tokens=2048,
                    )
                    body["messages"] = [{"role": "user", "content": "Say the word hello, once."}]
                else:
                    body["plugins"] = [{"id": "web", "max_results": 1, "engine": "exa"}]
                    body["messages"] = [
                        {
                            "role": "user",
                            "content": (
                                "Find the official website of the Python programming language. "
                                "Give only its URL."
                            ),
                        }
                    ]
                async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
                    if kind == "audio_output":
                        from scripts.live_usage_audio import stream_audio_case

                        result.update(
                            await stream_audio_case(client, ENDPOINTS[provider], key, body)
                        )
                        response = None
                    else:
                        response = await accounted_request(
                            client,
                            "POST",
                            ENDPOINTS[provider] + "/chat/completions",
                            provider=provider,
                            model=model,
                            operation=kind,
                            base_url=ENDPOINTS[provider],
                            headers={"Authorization": "Bearer " + key},
                            json=body,
                        )
                    if response is not None:
                        result.update(boundary="metered_protocol", httpStatus=response.status_code)
                    if response is not None and response.is_success:
                        payload = response.json()
                        messages = [c.get("message", {}) for c in payload.get("choices", [])]
                        result["finishReasons"] = [
                            c.get("finish_reason") for c in payload.get("choices", [])
                        ]
                        result["contentLength"] = sum(
                            len(str(m.get("content") or "")) for m in messages
                        )
                        result["errorMetadata"] = error_metadata(payload, key)
                        result["functional"] = (
                            any(bool(m.get("audio", {}).get("data")) for m in messages)
                            if kind == "audio_output"
                            else any(bool(m.get("content")) for m in messages)
                        )
                        if kind == "web_search":
                            result["searchCitationObserved"] = any(
                                bool(m.get("annotations")) for m in messages
                            )
                    elif response is not None:
                        result["failureKind"] = "http_" + str(response.status_code)
                        try:
                            result["errorMetadata"] = error_metadata(response.json(), key)
                        except ValueError:
                            pass
            else:
                result.update(status="unsupported_in_runner", boundary="not_executed")
        await sink.close(drain_timeout=10)
        query_now = int(time.time() * 1000) + 1
        query_params = {"range": {"preset": "all"}, "timezone": "UTC"}
        before = await query_usage_ledger(storage, query_params, now_ms=query_now)
        for call, completion in sink.completions:
            await SessionUsageEventSink.finalize(sink, call, completion)
        await sink.close(drain_timeout=10)
        after = await query_usage_ledger(storage, query_params, now_ms=query_now)
        result["replayIdempotent"] = before["totals"] == after["totals"]
        if not result["replayIdempotent"]:
            result["replayChangedFields"] = [
                k for k in after["totals"] if before["totals"].get(k) != after["totals"][k]
            ]
        result["totals"] = after["totals"]
        result["coverage"] = after["coverage"]
        result["taskGroups"] = after.get("taskGroups", [])
        groups = after.get("taskGroups", [])
        result["parentRollupConserved"] = (
            len(groups) == 1
            and groups[0]["rootSessionId"] == root_id
            and groups[0]["totals"]["costNanos"] == after["totals"]["costNanos"]
            and groups[0]["branches"]["sideChats"]["costNanos"] == after["totals"]["costNanos"]
        )
        result["meters"] = after.get("meters", [])
        result["settlementSummary"] = after.get("settlementSummary", {})
        events = await storage.query_usage_events(
            None, int(time.time() * 1000) + 1000, statuses=("started", "finalized", "unknown")
        )
        result["eventCount"] = len(events)
        result["ledgerStatuses"] = dict(Counter(e.status for e in events))
        if result["functional"]:
            result["status"] = (
                "passed"
                if result["replayIdempotent"]
                and result["parentRollupConserved"]
                and len(events) == 1
                else "accounting_failed"
            )
            if result["status"] == "passed" and after["totals"]["missingCostEntries"]:
                result["status"] = "functional_pass_cost_unavailable"
    except Exception as exc:
        from scripts.live_harness_security import classify_failure, redact_text

        result.update(
            errorType=type(exc).__name__,
            failureKind=classify_failure(str(exc)),
            diagnostic=redact_text(str(exc), [key])[:500],
        )
        if isinstance(getattr(exc, "job_id", None), str):
            result["jobId"] = exc.job_id
        # Preserve completed accounting even when publication/download failed.
        try:
            snapshot = await query_usage_ledger(
                storage, {"range": {"preset": "all"}, "timezone": "UTC"}
            )
            result["totals"] = snapshot["totals"]
            result["coverage"] = snapshot["coverage"]
        except Exception as query_error:
            result["queryErrorType"] = type(query_error).__name__
    finally:
        await sink.close(drain_timeout=10)
        await storage.close()
    return result


async def run_matrix(
    keys: dict[str, str], output: Path, limit: str, command: dict[str, Any]
) -> dict[str, Any]:
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    logging.disable(logging.CRITICAL)
    cases = build_matrix(json.loads((output / "catalogs.json").read_text(encoding="utf-8")))
    safe_write(output / "matrix.json", cases, keys)
    report_path = output / "report.json"
    report = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.exists()
        else {"budgetUsedUsd": "0", "results": []}
    )
    budget = Budget(limit, report["budgetUsedUsd"])
    report["budgetLimitUsd"] = str(budget.limit)
    selected = select_cases(cases, report["results"], command)
    selected = selected[: int(command.get("max_cases", len(selected)))]
    semaphore = asyncio.Semaphore(min(4, max(1, int(command.get("concurrency", 2)))))
    pacing = asyncio.Lock()
    next_dispatch = 0.0

    async def one(case: dict[str, Any]) -> None:
        nonlocal next_dispatch
        async with semaphore:
            if case["model"].endswith(":batch"):
                report["results"].append({**case, "status": "skipped_transport_unsupported"})
            elif case["reservationUsd"] is None:
                report["results"].append({**case, "status": "skipped_no_price_bound"})
            else:
                reservation = budget.reserve(case["reservationUsd"])
                if reservation is None:
                    report["results"].append({**case, "status": "skipped_budget"})
                else:
                    # Persist the reservation before any irreversible request.
                    pending = {
                        **case,
                        "status": "in_flight",
                        "startedAtMs": int(time.time() * 1000),
                    }
                    report["results"].append(pending)
                    report["budgetUsedUsd"] = str(budget.used)
                    safe_write(report_path, report, keys)
                    async with pacing:
                        await asyncio.sleep(max(0, next_dispatch - time.monotonic()))
                        next_dispatch = time.monotonic() + float(command.get("interval_seconds", 0))
                    result = await execute_case(case, keys[case["provider"]], output)
                    pending.update(result, finishedAtMs=int(time.time() * 1000))
                    totals = result.get("totals", {})
                    # Unknown/pending spends retain their full reservation.
                    confirmed = None
                    if (
                        totals.get("costSource") == "provider_billed"
                        and not totals.get("missingCostEntries")
                        and not totals.get("pendingBillingReceiptCount")
                    ):
                        confirmed = str(
                            totals.get(
                                "billedCostUsdExact",
                                totals.get("billedCostUsd", totals.get("costUsd", "0")),
                            )
                        )
                    budget.settle(reservation, confirmed)
                    pending["budgetAccountedUsd"] = str(
                        reservation if confirmed is None else confirmed
                    )
            report["budgetUsedUsd"] = str(budget.used)
            safe_write(report_path, report, keys)
            print(
                json.dumps(
                    {
                        "completed": len(report["results"]),
                        "provider": case["provider"],
                        "kind": case["kind"],
                        "model": case["model"],
                        "budgetReservedOrSpentUsd": str(budget.used),
                    }
                ),
                flush=True,
            )

    await asyncio.gather(*(one(case) for case in selected))
    return {
        "matrixCases": len(cases),
        "statusCounts": dict(Counter(r["status"] for r in report["results"])),
        "budgetReservedOrSpentUsd": str(budget.used),
    }


def _apply_reconciled_snapshot(
    row: dict[str, Any], snapshot: dict[str, Any], events: list[Any]
) -> None:
    """Recompute accounting evidence without inventing delivery/replay evidence."""
    totals, groups = snapshot["totals"], snapshot.get("taskGroups", [])
    parent_conserved = False
    if len(groups) == 1 and groups[0].get("rootSessionId") == "root-" + row["id"]:
        group = groups[0]
        branches = group.get("branches", {}).get("sideChats", {})
        parent_conserved = all(
            group.get("totals", {}).get(field) == totals.get(field)
            and branches.get(field) == totals.get(field)
            for field in ("costNanos", "billedCostNanos", "estimatedCostNanos", "eventCount")
        ) and all(
            group.get("totals", {}).get(field) == totals[field]
            and branches.get(field) == totals[field]
            for field in ("costUsdExact", "billedCostUsdExact", "estimatedCostUsdExact")
            if field in totals
        )
    row.update(
        totals=totals,
        coverage=snapshot["coverage"],
        taskGroups=groups,
        meters=snapshot.get("meters", []),
        settlementSummary=snapshot.get("settlementSummary", {}),
        eventCount=len(events),
        ledgerStatuses=dict(Counter(event.status for event in events)),
        parentRollupConserved=parent_conserved,
    )
    if row.get("status") == "in_flight":
        row["status"] = "interrupted"
    if row.get("functional"):
        if len(events) != 1 or not parent_conserved or row.get("replayIdempotent") is False:
            row["status"] = "accounting_failed"
        elif row.get("replayIdempotent") is True:
            row["status"] = (
                "functional_pass_cost_unavailable"
                if totals.get("missingCostEntries")
                or totals.get("pendingBillingReceiptCount")
                or totals.get("nativeBillingMissingConfirmedReceiptCount")
                else "passed"
            )
        else:
            row["status"] = "accounting_unverified"


async def reconcile_local(keys: dict[str, str], output: Path, limit: str) -> dict[str, Any]:
    """Reconcile committed receipts after worker interruption; no network calls."""
    from opensquilla.gateway.usage_query import query_usage_ledger
    from opensquilla.session.storage import SessionStorage

    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    budget = Budget(limit, report["budgetUsedUsd"])
    report["budgetLimitUsd"] = str(budget.limit)
    updated = 0
    errors = []
    for row in report["results"]:
        db = output / "ledgers" / (row["id"] + ".db")
        if not db.exists():
            if row.get("status") == "in_flight":
                row["status"] = "interrupted"
                # There is no durable receipt to release the pre-dispatch hold.
                # Missing storage does not prove that no upstream charge occurred.
            continue
        storage = None
        try:
            storage = await SessionStorage.open(str(db))
            query_now = int(time.time() * 1000) + 1
            snapshot = await query_usage_ledger(
                storage,
                {"range": {"preset": "all"}, "timezone": "UTC"},
                now_ms=query_now,
            )
            events = await storage.query_usage_events(
                None, query_now, statuses=("started", "finalized", "unknown")
            )
        except Exception as exc:
            errors.append({"id": row["id"], "errorType": type(exc).__name__})
            continue
        finally:
            if storage is not None:
                await storage.close()
        totals = snapshot["totals"]
        old = row.get("totals", {})
        prior = row.get("budgetAccountedUsd")
        if prior is None:
            prior = (
                old.get("billedCostUsdExact", old.get("billedCostUsd", "0"))
                if old.get("costSource") == "provider_billed"
                and not old.get("missingCostEntries")
                and not old.get("pendingBillingReceiptCount")
                else row.get("reservationUsd")
            )
        confirmed = (
            totals.get("billedCostUsdExact", totals.get("billedCostUsd"))
            if totals.get("costSource") == "provider_billed"
            and not totals.get("missingCostEntries")
            and not totals.get("pendingBillingReceiptCount")
            else None
        )
        if confirmed is not None and prior is not None:
            budget.settle(Decimal(str(prior)), str(confirmed))
            row["budgetAccountedUsd"] = str(confirmed)
        elif prior is not None:
            row["budgetAccountedUsd"] = str(prior)
        _apply_reconciled_snapshot(row, snapshot, events)
        updated += 1
    report["budgetUsedUsd"] = str(budget.used)
    safe_write(report_path, report, keys)
    return {
        "reconciledLocalLedgers": updated,
        "budgetReservedOrSpentUsd": str(budget.used),
        "reconciliationErrors": errors,
    }


def select_cases(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], command: dict[str, Any]
) -> list[dict[str, Any]]:
    """Retries are distinct paid attempts; previous costs and evidence survive."""
    selected = []
    cap = command.get("max_output_tokens")
    if cap is not None and (
        isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 8192
    ):
        raise ValueError("max_output_tokens must be an integer from 1 through 8192")
    for case in cases:
        if any(
            command.get(plural) and case[field] not in command[plural]
            for plural, field in (("providers", "provider"), ("kinds", "kind"), ("models", "model"))
        ):
            continue
        if cap is not None and case["kind"] in {
            "text",
            "tools",
            "tools_vision",
            "vision",
            "audio_input",
            "web_search",
        }:
            case = {**case, "options": {**case.get("options", {}), "max_output_tokens": cap}}
            if case.get("reservationUsd") is not None:
                # The catalog bound was built for MAX_OUTPUT. Scaling the full
                # bound conservatively covers the higher output cap; a smaller
                # cap never reduces the original input/output reservation.
                multiplier = max(Decimal(1), Decimal(cap) / MAX_OUTPUT)
                case["reservationUsd"] = str(Budget._amount(case["reservationUsd"]) * multiplier)
        attempts = [r for r in results if r.get("caseId", r["id"]) == case["id"]]
        if attempts:
            latest = attempts[-1]
            retry = latest["status"] in command.get("retry_statuses", []) or latest.get(
                "failureKind"
            ) in command.get("retry_failure_kinds", [])
            if not command.get("repeat") and not retry:
                continue
            number = len(attempts) + 1
            selected.append(
                {**case, "caseId": case["id"], "attempt": number, "id": f"{case['id']}-a{number}"}
            )
        else:
            selected.append(case)
    return selected


class Budget:
    """Reserve a conservative maximum before dispatch; unknown stays reserved."""

    def __init__(self, limit: str, used: str = "0") -> None:
        self.limit = self._amount(limit)
        self.used = self._amount(used)

    @staticmethod
    def _amount(value: Any) -> Decimal:
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid budget amount") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid budget amount")
        return amount

    def reserve(self, upper_bound: str) -> Decimal | None:
        amount = self._amount(upper_bound)
        if self.used + amount > self.limit:
            return None
        self.used += amount
        return amount

    def settle(self, reservation: Decimal, confirmed: str | None) -> None:
        if confirmed is None:
            return
        actual = self._amount(confirmed)
        self.used += actual - reservation


def safe_write(path: Path, value: Any, keys: dict[str, str]) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if any(secret and secret in text for secret in keys.values()):
        raise RuntimeError("Refusing credential-bearing report")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows readers/antivirus can briefly hold the destination without
    # delete-sharing. Keep the old durable reservation until atomic replace wins.
    for attempt in range(20):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def audit_credentials(keys: dict[str, str], output: Path) -> dict[str, Any]:
    """Check durable test evidence and the review diff without printing secrets."""
    needles = [value.encode() for value in keys.values() if value]
    scanned = 0
    matches = []
    for path in output.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".db", ".sqlite3", ".log"}:
            continue
        scanned += 1
        if any(secret in path.read_bytes() for secret in needles):
            matches.append(str(path.relative_to(output)))
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    if any(secret in diff for secret in needles):
        matches.append("tracked diff")
    result = {"scannedEvidenceFiles": scanned, "credentialMatches": matches}
    safe_write(output / "credential-audit.json", result, keys)
    return result


def discover(keys: dict[str, str], output: Path) -> dict[str, Any]:
    targets = {
        "tokenrhythm_public": ("https://tokenrhythm.studio/api/models", None),
        "tokenrhythm": (ENDPOINTS["tokenrhythm"] + "/models", "tokenrhythm"),
        "openrouter": (ENDPOINTS["openrouter"] + "/models", "openrouter"),
        "openrouter_video": (ENDPOINTS["openrouter"] + "/videos/models", "openrouter"),
    }
    results: dict[str, Any] = {}
    allowed = {
        "id",
        "name",
        "model",
        "modelId",
        "type",
        "category",
        "capabilities",
        "architecture",
        "pricing",
        "supported_parameters",
        "context_length",
        "endpoints",
        "modalities",
        "features",
        "parameters",
        "output_modalities",
        "input_modalities",
        "supported_endpoint_types",
        "default_parameters",
        "billingMode",
        "billingUnit",
        "currency",
        "inputPrice",
        "outputPrice",
        "effectiveInputPrice",
        "effectiveOutputPrice",
        "cacheReadPrice",
        "effectiveCacheReadPrice",
        "hasDiscount",
        "pricePerImage",
        "videoPricing",
        "effectiveAvailable",
        "status",
        "maxOutputTokens",
        "supportsStream",
        "supportsTools",
        "supportsVision",
        "supportsReasoning",
        "supportsEmbeddings",
        "supportsImageEditing",
        "supportsResponses",
        "protocolCapabilities",
        "abilities",
        "input_price_per_million",
        "output_price_per_million",
        "effective_input_price_per_million",
        "effective_output_price_per_million",
        "max_completion_tokens",
        "supports_tools",
        "supports_vision",
        "supports_reasoning",
        "effective_pricing",
        "pricing_skus",
        "supported_durations",
        "supported_resolutions",
        "supported_sizes",
        "supported_aspect_ratios",
        "generate_audio",
        "supported_frame_images",
        "supported_voices",
        "per_request_limits",
        "top_provider",
    }
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for name, (url, provider) in targets.items():
            headers = {"Authorization": "Bearer " + keys[provider]} if provider else {}
            try:
                response = client.get(url, headers=headers)
                body = response.json() if response.is_success else {}
                rows = body.get("data", body.get("models", []))
                if isinstance(rows, dict):
                    rows = rows.get("models", [])
                rows = rows if isinstance(rows, list) else []
                results[name] = {
                    "status": response.status_code,
                    "count": len(rows),
                    "rowKeys": sorted(
                        set().union(*(r.keys() for r in rows if isinstance(r, dict)))
                    ),
                    "models": [
                        {k: v for k, v in row.items() if k in allowed}
                        for row in rows
                        if isinstance(row, dict)
                    ],
                }
            except (httpx.HTTPError, ValueError) as exc:
                results[name] = {"status": "error", "errorType": type(exc).__name__, "models": []}
    safe_write(output / "catalogs.json", results, keys)
    return {
        name: {k: v for k, v in result.items() if k != "models"} for name, result in results.items()
    }


def inspect_video(keys: dict[str, str], output: Path) -> dict[str, Any]:
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    evidence = []
    for row in report["results"]:
        if row["kind"] != "video" or not row.get("jobId"):
            continue
        provider = row["provider"]
        response = httpx.get(
            ENDPOINTS[provider] + "/videos/" + row["jobId"],
            headers={"Authorization": "Bearer " + keys[provider]},
            timeout=30,
        )
        body = response.json() if response.is_success else {}
        fields: dict[str, Any] = {}

        def walk(value: Any, path: str = "") -> None:
            if isinstance(value, dict):
                fields[path + "<keys>"] = sorted(value)
                for key, child in value.items():
                    if isinstance(child, dict):
                        walk(child, path + key + ".")
                    elif any(
                        s in key.lower()
                        for s in (
                            "cost",
                            "bill",
                            "price",
                            "duration",
                            "currency",
                            "status",
                            "token",
                            "pending",
                            "seconds",
                            "count",
                        )
                    ) and isinstance(child, (int, float, bool, str, type(None))):
                        fields[path + key] = child

        walk(body)
        evidence.append(
            {
                "provider": provider,
                "model": row["model"],
                "httpStatus": response.status_code,
                "fields": fields,
            }
        )
    safe_write(output / "video-receipt-evidence.json", evidence, keys)
    return {"videoReceiptEvidence": evidence}


def _case_owned_job_records(
    case: dict[str, Any], records: list[Any], events: list[Any]
) -> list[Any]:
    provider = case["provider"]
    owned = {
        event.event_id: event
        for event in events
        if event.session_id == "child-" + case["id"]
        and event.provider == provider
        and event.model == case["model"]
    }
    return [
        (identity, record)
        for identity, record in records
        if record.get("provider") == provider
        and record.get("base_url", "").rstrip("/") == ENDPOINTS[provider]
        and record.get("call", {}).get("event_id") in owned
        and record.get("call", {}).get("session_id") == "child-" + case["id"]
        and record.get("call", {}).get("model") == case["model"]
        and record.get("call", {}).get("provider") == provider
        and record.get("call", {}).get("session_epoch", 0)
        == owned[record["call"]["event_id"]].session_epoch
        and (not case.get("jobId") or record.get("job_id") == case["jobId"])
    ]


async def reconcile_videos(keys: dict[str, str], output: Path) -> dict[str, Any]:
    """GET-only recovery of already-submitted acceptance jobs, never a new POST."""
    import re
    import sqlite3

    import structlog

    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.gateway.usage_query import query_usage_ledger
    from opensquilla.provider.media_jobs import _key, observe_video_job
    from opensquilla.provider.video_generation import _safe_job_id, read_video_json
    from opensquilla.session.storage import SessionStorage

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    recovered = []
    for case in report["results"]:
        if case["kind"] != "video":
            continue
        provider = case["provider"]
        db = output / "ledgers" / (case["id"] + ".db")
        sidecar = db.with_name(db.name + ".media-jobs.sqlite3")
        evidence: dict[str, Any] = {
            "id": case["id"],
            "provider": provider,
            "model": case["model"],
            "getOnly": True,
        }
        recovered.append(evidence)
        if not db.exists() or not sidecar.exists():
            evidence["recoveryStatus"] = "no_durable_job"
            continue
        if provider not in ENDPOINTS or not keys.get(provider):
            evidence["recoveryStatus"] = "provider_credentials_unavailable"
            continue
        snapshots = []
        query_now = None
        try:
            # Read only this case's sidecar; never adopt another case/profile's
            # jobs or create a new database when the original dispatch is absent.
            with sqlite3.connect(sidecar.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                records = [
                    (key, json.loads(raw))
                    for key, raw in connection.execute(
                        "SELECT job_key,payload FROM media_jobs WHERE namespace='usage'"
                    )
                ]
            for pass_no in range(2):
                storage = await SessionStorage.open(str(db))
                sink = SessionUsageEventSink(storage)
                observer = uuid.uuid4().hex
                scope = UsageAccountingScope(
                    sink=sink,
                    context=UsageExecutionContext(
                        execution_id=observer,
                        agent_run_id=observer,
                        session_id="different-observer",
                        agent_id="main",
                        run_kind="agent",
                    ),
                )
                try:
                    events = await storage.query_usage_events(
                        None, None, statuses=("started", "finalized", "unknown")
                    )
                    matching = _case_owned_job_records(case, records, events)
                    if len(matching) != 1:
                        evidence["recoveryStatus"] = "durable_job_owner_missing_or_ambiguous"
                        break
                    identity, record = matching[0]
                    job_id = _safe_job_id(record.get("job_id"))
                    with bind_usage_accounting_scope(scope):
                        if identity != _key(provider, ENDPOINTS[provider], job_id, keys[provider]):
                            evidence["recoveryStatus"] = "durable_job_payer_mismatch"
                            break
                        case["jobId"] = evidence["jobId"] = job_id
                        evidence["jobIdRecoveredFromSidecar"] = True
                        safe_write(report_path, report, keys)
                        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                            async with client.stream(
                                "GET",
                                ENDPOINTS[provider] + "/videos/" + job_id,
                                headers={"Authorization": "Bearer " + keys[provider]},
                            ) as response:
                                evidence["httpStatus"] = response.status_code
                                body = await read_video_json(
                                    response, max_bytes=1024 * 1024, label="Video recovery"
                                )
                                if not response.is_success:
                                    error = body.get("error")
                                    error = error if isinstance(error, dict) else {}
                                    code = error.get("code", body.get("code"))
                                    if isinstance(code, (str, int)) and re.fullmatch(
                                        r"[A-Za-z0-9_.:-]{1,100}", str(code)
                                    ):
                                        evidence["errorCode"] = str(code)
                                    evidence["recoveryStatus"] = "http_error"
                                    break
                        raw_status = str(body.get("status", "")).lower()
                        outcome = (
                            "completed"
                            if raw_status in {"completed", "succeeded", "success", "done"}
                            else "failed"
                            if raw_status in {"failed", "error", "cancelled", "canceled"}
                            else "pending"
                        )
                        evidence["jobOutcome"] = outcome
                        await observe_video_job(
                            provider,
                            ENDPOINTS[provider],
                            job_id,
                            body,
                            api_key=keys[provider],
                            outcome=outcome,
                        )
                    # Receipt retries must drain before reading its projection.
                    await sink.close(drain_timeout=10)
                    if query_now is None:
                        query_now = int(time.time() * 1000) + 1
                    snapshot = await query_usage_ledger(
                        storage, {"range": {"preset": "all"}, "timezone": "UTC"}, now_ms=query_now
                    )
                    events = await storage.query_usage_events(
                        None, query_now, statuses=("started", "finalized", "unknown")
                    )
                    # Local reconciliation will release this original hold.
                    # Preserve it before replacing a stale unpriced snapshot.
                    if case.get("budgetAccountedUsd") is None:
                        old_totals = case.get("totals", {})
                        old_confirmed = (
                            old_totals.get("billedCostUsdExact", old_totals.get("billedCostUsd"))
                            if old_totals.get("costSource") == "provider_billed"
                            and not old_totals.get("missingCostEntries")
                            and not old_totals.get("pendingBillingReceiptCount")
                            else None
                        )
                        old_accounted = (
                            old_confirmed
                            if old_confirmed is not None
                            else case.get("reservationUsd")
                        )
                        if old_accounted is not None:
                            case["budgetAccountedUsd"] = str(old_accounted)
                    _apply_reconciled_snapshot(case, snapshot, events)
                    snapshots.append(
                        {
                            "totals": snapshot["totals"],
                            "taskGroups": snapshot.get("taskGroups", []),
                            "meters": snapshot.get("meters", []),
                            "eventCount": len(events),
                            "parentRollupConserved": case["parentRollupConserved"],
                        }
                    )
                finally:
                    await sink.close(drain_timeout=10)
                    await storage.close()
            if snapshots:
                evidence.update(snapshots[-1])
                if len(snapshots) == 2:
                    verified = snapshots[0] == snapshots[1]
                    evidence["restartReplayIdempotent"] = verified
                    case["restartReplayIdempotent"] = verified
                    if verified:
                        case["replayIdempotent"] = True
                        case["replayEvidence"] = "get_only_restart"
                        _apply_reconciled_snapshot(case, snapshot, events)
                    evidence["recoveryStatus"] = "reconciled"
        except Exception as exc:
            evidence.update(recoveryStatus="error", errorType=type(exc).__name__)
        finally:
            case["videoRecovery"] = {
                key: value
                for key, value in evidence.items()
                if key not in {"totals", "taskGroups", "meters"}
            }
            safe_write(report_path, report, keys)
            safe_write(output / "video-reconciliation.json", recovered, keys)
    safe_write(output / "video-reconciliation.json", recovered, keys)
    return {
        "videoRecovery": [
            {k: v for k, v in r.items() if k not in {"taskGroups", "meters"}} for r in recovered
        ]
    }


async def resume_video_delivery(
    keys: dict[str, str], output: Path, command: dict[str, Any]
) -> dict[str, Any]:
    """Explicit GET/download recovery only; never submit or alter budget holds."""
    import re
    import sqlite3

    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.gateway.usage_query import query_usage_ledger
    from opensquilla.provider.media_jobs import _key
    from opensquilla.provider.tokenrhythm_video_generation import (
        _safe_task_id,
        resume_tokenrhythm_video,
    )
    from opensquilla.session.storage import SessionStorage

    case_ids = command.get("case_ids", [])
    if not isinstance(case_ids, list) or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        for value in case_ids
    ):
        raise ValueError("case_ids must be an explicit list of safe attempt IDs")
    if not case_ids:
        return {"deliveryRecovery": []}
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    recovered = []
    for case_id in dict.fromkeys(case_ids):
        evidence: dict[str, Any] = {"id": case_id, "getOnly": True, "budgetUnchanged": True}
        recovered.append(evidence)
        matches = [row for row in report["results"] if row.get("id") == case_id]
        if len(matches) != 1:
            evidence["status"] = "case_missing_or_ambiguous"
            continue
        case = matches[0]
        if (
            case.get("provider") != "tokenrhythm"
            or case.get("kind") != "video"
            or (case.get("videoRecovery") or {}).get("recoveryStatus") != "reconciled"
            or not isinstance(case.get("jobId"), str)
        ):
            evidence["status"] = "case_not_reconciled_tokenrhythm_video"
            continue
        db = output / "ledgers" / (case_id + ".db")
        sidecar = db.with_name(db.name + ".media-jobs.sqlite3")
        if not db.exists() or not sidecar.exists() or not keys.get("tokenrhythm"):
            evidence["status"] = "durable_job_or_credentials_unavailable"
            continue
        storage = sink = None
        try:
            with sqlite3.connect(sidecar.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                records = [
                    (identity, json.loads(raw))
                    for identity, raw in connection.execute(
                        "SELECT job_key,payload FROM media_jobs WHERE namespace='usage'"
                    )
                ]
            storage = await SessionStorage.open(str(db))
            sink = SessionUsageEventSink(storage)
            events_before = await storage.query_usage_events(
                None, None, statuses=("started", "finalized", "unknown")
            )
            matching = _case_owned_job_records(case, records, events_before)
            if len(matching) != 1:
                evidence["status"] = "durable_job_owner_missing_or_ambiguous"
                continue
            identity, record = matching[0]
            job_id = _safe_task_id(record.get("job_id"))
            observer = "delivery-observer-" + uuid.uuid4().hex
            scope = UsageAccountingScope(
                sink=sink,
                context=UsageExecutionContext(
                    execution_id=observer,
                    agent_run_id=observer,
                    session_id=observer,
                    agent_id="main",
                    run_kind="agent",
                ),
            )
            with bind_usage_accounting_scope(scope):
                if identity != _key(
                    "tokenrhythm", ENDPOINTS["tokenrhythm"], job_id, keys["tokenrhythm"]
                ):
                    evidence["status"] = "durable_job_payer_mismatch"
                    continue
                await sink.close(drain_timeout=10)
                query_now = int(time.time() * 1000) + 1
                params = {"range": {"preset": "all"}, "timezone": "UTC"}
                before = await query_usage_ledger(storage, params, now_ms=query_now)
                projection_before = dict(case)
                _apply_reconciled_snapshot(projection_before, before, events_before)
                if len(events_before) != 1 or not projection_before["parentRollupConserved"]:
                    evidence["status"] = "initial_accounting_invariant_failed"
                    continue
                destination = (
                    output / "media" / (case_id + "-delivery-" + uuid.uuid4().hex + ".mp4")
                )
                generated = await resume_tokenrhythm_video(
                    base_url=ENDPOINTS["tokenrhythm"],
                    api_key=keys["tokenrhythm"],
                    job_id=job_id,
                    model=case["model"],
                    output_path=destination,
                    timeout_seconds=90,
                    max_bytes=16 * 1024 * 1024,
                    poll_interval_seconds=5,
                )
                await sink.close(drain_timeout=10)
                after = await query_usage_ledger(storage, params, now_ms=query_now)
                # Count the whole ledger, including an unexpected newer event
                # that a fixed query window might otherwise conceal.
                events_after = await storage.query_usage_events(
                    None, None, statuses=("started", "finalized", "unknown")
                )
                projection_after = dict(case)
                _apply_reconciled_snapshot(projection_after, after, events_after)
                unchanged = (
                    before["totals"] == after["totals"]
                    and before.get("taskGroups", []) == after.get("taskGroups", [])
                    and len(events_after) == 1
                    and events_after[0].event_id == events_before[0].event_id
                    and projection_after["parentRollupConserved"]
                )
                evidence.update(
                    ledgerUnchanged=unchanged,
                    eventCount=len(events_after),
                    parentRollupConserved=projection_after["parentRollupConserved"],
                    bytes=generated.bytes_written,
                )
                if not unchanged or generated.bytes_written <= 0:
                    evidence["status"] = "delivery_accounting_check_failed"
                    continue
                case.update(functional=True, bytes=generated.bytes_written)
                _apply_reconciled_snapshot(case, after, events_after)
                evidence.update(status="delivered", artifact=str(destination.relative_to(output)))
        except Exception as exc:
            evidence.update(status="failed", errorType=type(exc).__name__)
        finally:
            if sink is not None:
                await sink.close(drain_timeout=10)
            if storage is not None:
                await storage.close()
            case["deliveryRecovery"] = evidence
            safe_write(report_path, report, keys)
    return {"deliveryRecovery": recovered}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-usd", required=True)
    args = parser.parse_args()
    payload = json.loads(sys.stdin.read())
    keys, command = payload["keys"], payload["command"]
    Budget(args.budget_usd)
    if command.get("action") == "discover":
        result = discover(keys, args.output)
    elif command.get("action") == "inspect_video":
        result = inspect_video(keys, args.output)
    elif command.get("action") == "reconcile_video":
        result = asyncio.run(reconcile_videos(keys, args.output))
    elif command.get("action") == "resume_video_delivery":
        result = asyncio.run(resume_video_delivery(keys, args.output, command))
    elif command.get("action") == "reconcile_local":
        result = asyncio.run(reconcile_local(keys, args.output, args.budget_usd))
    elif command.get("action") == "audit_credentials":
        result = audit_credentials(keys, args.output)
    elif command.get("action") == "plan":
        cases = build_matrix(
            json.loads((args.output / "catalogs.json").read_text(encoding="utf-8"))
        )
        safe_write(args.output / "matrix.json", cases, keys)
        result = {
            "cases": len(cases),
            "byKind": dict(Counter(c["kind"] for c in cases)),
            "unpriced": sum(c["reservationUsd"] is None for c in cases),
        }
    elif command.get("action") == "run":
        result = asyncio.run(run_matrix(keys, args.output, args.budget_usd, command))
    else:
        result = {"status": "not_ready", "action": command.get("action")}
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
