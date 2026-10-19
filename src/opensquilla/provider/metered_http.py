"""Accounting boundary for non-chat provider requests.

Only allowlisted receipt fields are retained. Prompts, audio, search results,
headers containing credentials, and generated media never enter the ledger.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import wave
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from opensquilla.endpoint_identity import base_url_matches_official_api
from opensquilla.provider.fx import settlement_fx_evidence
from opensquilla.usage_metering import UsageMeter, UsageSettlementRevision


def decimal_quantity(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("meter quantity must be a finite non-negative decimal")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid meter quantity") from exc
    if (
        not amount.is_finite()
        or amount < 0
        or amount > Decimal(2**63 - 1)
        or amount.as_tuple().exponent < -30
    ):
        raise ValueError("invalid meter quantity")
    return format(amount, "f")


def _provider_meter(
    metric: str, unit: str, value: Any, *, dimensions: Mapping[str, str]
) -> UsageMeter:
    try:
        quantity = decimal_quantity(value)
    except ValueError:
        return UsageMeter(metric, unit, None, known=False, dimensions=dimensions)
    return UsageMeter(metric, unit, quantity, dimensions=dimensions)


def _official(provider: str, base_url: str) -> bool:
    endpoint = {
        "openrouter": "https://openrouter.ai/api/v1",
        "tokenrhythm": "https://tokenrhythm.studio/v1",
        "xai": "https://api.x.ai/v1",
        "exa": "https://api.exa.ai",
    }.get(provider)
    return bool(endpoint and base_url_matches_official_api(endpoint, base_url))


def response_measurement(
    *,
    provider: str,
    operation: str,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, Any] | None = None,
    base_url: str = "",
    raw_json: str = "",
    requested_meters: tuple[Any, ...] = (),
    revision_id: str = "initial",
    previous_revision_id: str | None = None,
    dimensions: Mapping[str, str] | None = None,
) -> tuple[tuple[UsageMeter, ...], UsageSettlementRevision | None]:
    """Parse money independently of tokens; provider estimates stay estimates."""
    data: Mapping[str, Any] = payload or {}
    if raw_json:
        try:
            exact = json.loads(raw_json, parse_float=Decimal)
            if isinstance(exact, Mapping):
                data = exact
        except (TypeError, ValueError):
            pass
    usage = data.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    metadata = {**(dimensions or {}), "operation": operation}
    request_id = data.get("request_id") or data.get("requestId") or data.get("generation_id")
    response_headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    request_id = request_id or response_headers.get("request-id")
    if isinstance(request_id, str) and len(request_id) <= 256:
        metadata["provider_request_id"] = request_id
    meters = [replace(m, dimensions={**m.dimensions, **metadata}) for m in requested_meters]
    settlement = None
    monetary: tuple[str, str, str, str] | None = None
    if provider == "openrouter" and _official(provider, base_url):
        if "cost" in usage:
            amount = decimal_quantity(usage["cost"])
            monetary = ("provider_confirmed", "USD", amount, amount)
    elif provider == "xai" and _official(provider, base_url):
        if "cost_in_usd_ticks" in usage:
            ticks = Decimal(decimal_quantity(usage["cost_in_usd_ticks"]))
            if ticks != ticks.to_integral_value():
                raise ValueError("xAI cost ticks must be integral")
            amount = format(ticks / Decimal(10**10), "f")
            monetary = ("provider_confirmed", "USD", amount, amount)
            meters.append(
                UsageMeter("cost_in_usd_ticks", "usd_tick_1e-10", str(ticks), dimensions=metadata)
            )
    elif provider == "tokenrhythm" and _official(provider, base_url):
        from opensquilla.provider.openai import (
            _billing_result,
            _ProviderBillingAccumulator,
            _UsageSnapshotAccumulator,
        )

        native_billing = data.get("billing")
        billing_data = data
        if operation == "video_generation" and isinstance(native_billing, Mapping):
            # Video jobs return one total plus explanatory component costs.
            # The components must never be added to the already inclusive total.
            billing_data = {"billing_pending": native_billing.get("pending")}
            if "total_cost_cny" in native_billing:
                billing_data["cost_cny"] = native_billing["total_cost_cny"]
        billing = _ProviderBillingAccumulator()
        billing.update(provider, billing_data)
        _cost, _source, receipt = _billing_result(
            provider_kind=provider,
            base_url=base_url,
            usage=_UsageSnapshotAccumulator(),
            billing=billing,
            model="",
        )
        if receipt is not None:
            native = (
                None
                if receipt.amount_nanos is None
                else decimal_quantity(billing_data.get("cost_cny"))
            )
            usd = None
            if receipt.status == "confirmed" and native is not None:
                usd = (
                    format(
                        Decimal(native) / (Decimal(receipt.fx_native_per_usd_nanos) / 10**9), ".18f"
                    )
                    .rstrip("0")
                    .rstrip(".")
                    or "0"
                )
            settlement = UsageSettlementRevision(
                revision_id=revision_id,
                previous_revision_id=previous_revision_id,
                authority="provider_confirmed" if receipt.status == "confirmed" else "unknown",
                status="final" if receipt.status == "confirmed" else "pending",
                currency=receipt.currency,
                native_amount=native,
                usd_amount=usd,
                reason_code="provider_native_receipt",
                **settlement_fx_evidence(
                    receipt.currency, str(Decimal(receipt.fx_native_per_usd_nanos) / 10**9)
                ),
            )
    elif provider == "exa" and _official(provider, base_url):
        costs = data.get("costDollars")
        if isinstance(costs, Mapping) and "total" in costs:
            amount = decimal_quantity(costs["total"])
            monetary = ("provider_estimate", "USD", amount, amount)
    if monetary is not None:
        authority, currency, native, usd = monetary
        settlement = UsageSettlementRevision(
            revision_id=revision_id,
            previous_revision_id=previous_revision_id,
            authority=authority,
            status="final",
            currency=currency,
            native_amount=native,
            usd_amount=usd,
            reason_code="provider_response",
            **settlement_fx_evidence(currency, "1"),
        )
    if provider == "tavily" and "credits" in usage:
        meters.append(
            _provider_meter("search_credits", "credit", usage["credits"], dimensions=metadata)
        )
    if provider == "iqs" and operation == "web_search":
        credits = data.get("costCredits")
        if isinstance(credits, Mapping):
            for category, components in (
                ("search", ("genericTextSearch", "liteTextSearch", "liteAdvancedTextSearch",
                            "autoTextSearch")),
                ("valueAdded", ("summary", "advanced")),
            ):
                charges = credits.get(category)
                if not isinstance(charges, Mapping):
                    continue
                for component in components:
                    if component in charges:
                        meters.append(
                            _provider_meter(
                                "search_credits", "credit", charges[component],
                                dimensions={
                                    **metadata, "billing_component": f"{category}.{component}",
                                },
                            )
                        )
    if provider == "firecrawl" and operation == "web_fetch":
        document = data.get("data")
        document_metadata = document.get("metadata") if isinstance(document, Mapping) else None
        if isinstance(document_metadata, Mapping) and "creditsUsed" in document_metadata:
            meters.append(
                _provider_meter(
                    "scrape_credits",
                    "credit",
                    document_metadata["creditsUsed"],
                    dimensions=metadata,
                )
            )
    if provider == "elevenlabs" and "character-cost" in response_headers:
        meters.append(
            _provider_meter(
                "character_credits",
                "credit",
                response_headers["character-cost"],
                dimensions=metadata,
            )
        )
    if operation == "video_generation":
        for fields, metric, unit in (
            (("output_seconds", "video_seconds", "duration"), "video_seconds", "second"),
            (("input_seconds",), "input_video_seconds", "second"),
            (("input_image_count",), "input_images", "image"),
            (("input_audio_seconds",), "input_audio_seconds", "second"),
        ):
            field = next((key for key in fields if key in usage), None)
            if field is not None:
                meters.append(_provider_meter(metric, unit, usage[field], dimensions=metadata))
    if operation == "image_generation":
        if "image_count" in usage:
            meters.append(
                _provider_meter(
                    "output_images",
                    "image",
                    usage["image_count"],
                    dimensions=metadata,
                )
            )
        elif isinstance(data.get("data"), list):
            meters.append(
                UsageMeter(
                    "output_images",
                    "image",
                    str(len(data["data"])),
                    dimensions=metadata,
                    source="observed",
                )
            )
        elif isinstance(data.get("choices"), list):
            image_lists = [
                choice["message"].get("images", [])
                for choice in data["choices"]
                if isinstance(choice, Mapping) and isinstance(choice.get("message"), Mapping)
            ]
            if any(not isinstance(images, list) for images in image_lists):
                meters.append(
                    UsageMeter(
                        "output_images", "image", None, known=False,
                        dimensions=metadata, source="observed",
                    )
                )
            elif count := sum(len(images) for images in image_lists):
                meters.append(
                    UsageMeter(
                        "output_images", "image", str(count), dimensions=metadata, source="observed"
                    )
                )
        for detail_name, direction in (
            ("input_tokens_details", "input"),
            ("output_tokens_details", "output"),
        ):
            details = usage.get(detail_name)
            if isinstance(details, Mapping):
                for modality in ("text", "image", "audio"):
                    key = f"{modality}_tokens"
                    if key in details:
                        meters.append(
                            _provider_meter(
                                f"{direction}_{modality}_tokens",
                                "token",
                                details[key],
                                dimensions=metadata,
                            )
                        )
    if not meters and operation != "llm":
        meters.append(
            UsageMeter("requests", "request", "1", dimensions=metadata, source="observed")
        )
    return tuple(meters), settlement


async def accounted_request(
    client: Any,
    method: str,
    url: str,
    *,
    provider: str,
    model: str = "",
    operation: str,
    base_url: str = "",
    requested_meters: tuple[Any, ...] = (),
    **kwargs: Any,
) -> Any:
    """Reserve before each physical request, including retries and fallbacks."""
    from opensquilla.engine.usage_http import reserve_direct_usage_call

    reservation = await reserve_direct_usage_call(
        provider=provider,
        model=model or operation,
        base_url=base_url,
        operation_kind=operation,
    )
    try:
        response = await getattr(client, method.lower())(url, **kwargs)
        if (
            getattr(response, "is_error", False)
            or int(getattr(response, "status_code", 200)) >= 400
        ):
            await reservation.mark_unknown("direct_request_failed")
            return response
        try:
            payload = response.json()
        except (ValueError, TypeError, AttributeError):
            payload = {}
        if provider == "elevenlabs" and operation in {
            "text_to_speech",
            "voice_conversion",
            "music_generation",
        }:
            content = getattr(response, "content", b"")
            duration = (
                await asyncio.to_thread(audio_duration_seconds, content)
                if isinstance(content, bytes) and content
                else None
            )
            requested_meters += (
                UsageMeter(
                    "output_audio_seconds",
                    "second",
                    duration,
                    known=duration is not None,
                    source="observed",
                ),
            )
        try:
            meters, settlement = response_measurement(
                provider=provider,
                operation=operation,
                base_url=base_url,
                payload=payload if isinstance(payload, Mapping) else {},
                headers=getattr(response, "headers", {}),
                raw_json=getattr(response, "text", "")
                if isinstance(payload, Mapping) and payload
                else "",
                requested_meters=requested_meters,
            )
            done = None
            if isinstance(payload, Mapping) and provider in {
                "openrouter",
                "tokenrhythm",
                "openai",
                "xai",
            }:
                from opensquilla.engine.usage_http import openai_compatible_done_event

                try:
                    done = openai_compatible_done_event(
                        payload, default_model=model, provider_kind=provider, base_url=base_url
                    )
                except (TypeError, ValueError):
                    pass
            await reservation.finalize_measurement(
                meters=meters, settlement=settlement, done=done, token_usage_known=done is not None
            )
        except (TypeError, ValueError, OverflowError):
            await reservation.mark_unknown("missing_or_invalid_usage_receipt")
        return response
    except asyncio.CancelledError:
        await reservation.mark_unknown("cancelled")
        raise
    except BaseException:
        await reservation.mark_unknown("direct_request_failed")
        raise


def audio_duration_seconds(payload: bytes) -> str | None:
    """Measure media duration, never infer it from transcript word timings."""
    try:
        with wave.open(io.BytesIO(payload)) as audio:
            return format(Decimal(audio.getnframes()) / audio.getframerate(), "f")
    except (wave.Error, EOFError, ValueError):
        pass
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "pipe:0",
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            return decimal_quantity(result.stdout.decode("ascii").strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


async def accounted_audio_request(
    client: Any, url: str, *, operation: str, model: str, base_url: str, **kwargs: Any
) -> Any:
    meters: list[UsageMeter] = []
    files = kwargs.get("files") or {}
    for value in files.values():
        if isinstance(value, tuple) and len(value) > 1 and isinstance(value[1], bytes):
            duration = await asyncio.to_thread(audio_duration_seconds, value[1])
            meters.append(
                UsageMeter(
                    "input_audio_seconds",
                    "second",
                    duration,
                    known=duration is not None,
                    source="observed",
                )
            )
            break
    body = kwargs.get("json") or {}
    if isinstance(body.get("text"), str):
        meters.append(
            UsageMeter("input_characters", "character", str(len(body["text"])), source="request")
        )
    if body.get("music_length_ms") is not None:
        duration = format(Decimal(decimal_quantity(body["music_length_ms"])) / 1000, "f")
        meters.append(UsageMeter("requested_audio_seconds", "second", duration, source="request"))
    return await accounted_request(
        client,
        "POST",
        url,
        provider="elevenlabs",
        model=model,
        operation=operation,
        base_url=base_url,
        requested_meters=tuple(meters),
        **kwargs,
    )
