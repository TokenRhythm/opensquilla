#!/usr/bin/env python3
"""Live TokenRhythm billing audit with a deliberately narrow report surface.

The audit performs one direct call, one call for every curated inline router
tier, and the static TokenRhythm B5 lineup with its default and strict quorum.
It never records response content or prompts. The only persistent artifact is a
sanitized accounting report below an operating-system temporary directory.

This is an opt-in, billable live tool. It reads a rotated credential only from
``TOKENRHYTHM_API_KEY`` and requires explicit live-cost and rotation
attestations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.engine.pricing import estimate_cost, resolve_model_price  # noqa: E402
from opensquilla.gateway.config import GatewayConfig  # noqa: E402
from opensquilla.provider.ensemble import (  # noqa: E402
    EnsembleProvider,
    build_ensemble_provider_from_config,
)
from opensquilla.provider.preset_registry import get_preset  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from opensquilla.provider.selector import ProviderConfig, _build_provider  # noqa: E402
from opensquilla.provider.types import (  # noqa: E402
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderBillingReceipt,
)
from scripts.live_harness_security import (  # noqa: E402
    classify_failure,
    is_temporary_report_path,
    registry_endpoint,
    report_contains_secret,
    sanitize_report,
    write_safe_report,
)

PROVIDER_ID = "tokenrhythm"
KEY_ENV = "TOKENRHYTHM_API_KEY"
BASE_URL_ENV = "TOKENRHYTHM_BASE_URL"
OUTPUT_BUDGET_TOKENS = 1024
FX_NATIVE_PER_USD_NANOS = 6_975_000_000
CNY_PER_USD = Decimal("6.975")
NANO = Decimal("1000000000")
TEXT_TIERS = ("c0", "c1", "c2", "c3")

# Public-dummy prompts are constants and are consumed in memory only. They are
# never returned by any helper or placed in the report.
_DIRECT_PROMPT = "Reply with only the word OK."
_ENSEMBLE_PROMPT = "Reply with one short sentence explaining that water freezes below zero C."

_FORBIDDEN_REPORT_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "content",
        "credential",
        "error",
        "error_message",
        "message",
        "prompt",
        "response",
        "response_text",
        "secret",
        "session",
        "text",
    }
)


def _money_nanos(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return int((parsed * NANO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _non_negative_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _inline_tokenrhythm_tiers() -> dict[str, dict[str, Any]]:
    """Resolve curated tiers inline and prove no non-legacy profile is persisted."""

    preset = get_preset(PROVIDER_ID)
    if preset is None:
        raise RuntimeError("TokenRhythm preset is unavailable")
    tiers = {
        tier: dict(preset.tier_defaults()[tier])
        for tier in TEXT_TIERS
    }
    config = GatewayConfig.model_validate(
        {
            "llm": {"provider": PROVIDER_ID},
            "squilla_router": {"enabled": True, "tiers": tiers},
        }
    )
    persisted_router = config.to_toml_dict()["squilla_router"]
    if config.squilla_router.tier_profile is not None or "tier_profile" in persisted_router:
        raise RuntimeError("TokenRhythm audit refuses a persisted router profile")
    return tiers


def _receipt_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, ProviderBillingReceipt):
        return {
            "currency": value.currency,
            "status": value.status,
            "amount_nanos": value.amount_nanos,
            "usd_equivalent_nanos": value.usd_equivalent_nanos,
            "fx_native_per_usd_nanos": value.fx_native_per_usd_nanos,
            "schema_version": value.schema_version,
        }
    if isinstance(value, Mapping):
        return {
            "currency": value.get("currency"),
            "status": value.get("status"),
            "amount_nanos": value.get("amount_nanos"),
            "usd_equivalent_nanos": value.get("usd_equivalent_nanos"),
            "fx_native_per_usd_nanos": value.get("fx_native_per_usd_nanos"),
            "schema_version": value.get("schema_version", 1),
        }
    return None


def _receipt_summary(value: object) -> tuple[dict[str, Any] | None, list[str]]:
    raw = _receipt_mapping(value)
    if raw is None:
        return None, ["missing_native_receipt"]

    issues: list[str] = []
    currency = str(raw.get("currency") or "").upper()
    status = str(raw.get("status") or "")
    amount = raw.get("amount_nanos")
    usd = raw.get("usd_equivalent_nanos")
    fx = raw.get("fx_native_per_usd_nanos")
    schema_version = raw.get("schema_version")

    if currency != "CNY":
        issues.append("unexpected_receipt_currency")
    if status not in {"confirmed", "pending"}:
        issues.append("invalid_receipt_status")
    if isinstance(amount, bool) or (amount is not None and not isinstance(amount, int)):
        issues.append("invalid_native_amount")
        amount = None
    if isinstance(amount, int) and amount < 0:
        issues.append("negative_native_amount")
    if isinstance(usd, bool) or (usd is not None and not isinstance(usd, int)):
        issues.append("invalid_usd_equivalent")
        usd = None
    if isinstance(usd, int) and usd < 0:
        issues.append("negative_usd_equivalent")
    if fx != FX_NATIVE_PER_USD_NANOS:
        issues.append("unexpected_normalization_rate")
    if isinstance(schema_version, bool) or schema_version != 1:
        issues.append("unsupported_receipt_schema")

    if status == "confirmed":
        if amount is None:
            issues.append("confirmed_native_amount_missing")
        if usd is None:
            issues.append("confirmed_usd_equivalent_missing")
        if isinstance(amount, int) and isinstance(usd, int):
            expected_usd = int(
                (Decimal(amount) / CNY_PER_USD).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if usd != expected_usd:
                issues.append("usd_normalization_mismatch")
    elif status == "pending" and usd is not None:
        issues.append("pending_receipt_has_usd_equivalent")

    summary = {
        "currency": currency,
        "status": status,
        "amountNanos": str(amount) if isinstance(amount, int) else None,
        "usdEquivalentNanos": str(usd) if isinstance(usd, int) else None,
        "normalizationRateNativePerUsdNanos": (
            str(fx) if isinstance(fx, int) and not isinstance(fx, bool) else None
        ),
        "schemaVersion": schema_version if isinstance(schema_version, int) else None,
    }
    return summary, issues


def _physical_item(row: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    provider = str(row.get("provider") or PROVIDER_ID)
    model = str(row.get("model") or "")
    input_tokens = _non_negative_int(row.get("input_tokens"))
    output_tokens = _non_negative_int(row.get("output_tokens"))
    cache_read_tokens = _non_negative_int(
        row.get("cache_read_tokens") or row.get("cached_tokens")
    )
    cache_write_tokens = _non_negative_int(row.get("cache_write_tokens"))
    billed_cost = _non_negative_float(row.get("billed_cost"))
    cost_source = str(row.get("cost_source") or "none")

    resolved = resolve_model_price(model, provider)
    estimate = estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        price=resolved.entry,
    )
    receipt, issues = _receipt_summary(row.get("billing_receipt"))
    billed_nanos = _money_nanos(billed_cost)
    receipt_status = str((receipt or {}).get("status") or "missing")
    receipt_usd = (receipt or {}).get("usdEquivalentNanos")

    if receipt_status == "confirmed":
        if cost_source != "provider_billed":
            issues.append("confirmed_receipt_not_provider_billed")
        if billed_nanos is None or receipt_usd != str(billed_nanos):
            issues.append("canonical_usd_mismatch")
    elif receipt_status == "pending":
        if cost_source == "provider_billed" or billed_nanos != 0:
            issues.append("pending_receipt_counted_as_billed")

    return {
        "ordinal": ordinal,
        "role": str(row.get("role") or "direct"),
        "provider": provider,
        "model": model,
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "reasoningTokens": _non_negative_int(row.get("reasoning_tokens")),
            "cacheReadTokens": cache_read_tokens,
            "cacheWriteTokens": cache_write_tokens,
        },
        "receipt": receipt,
        "cost": {
            "source": cost_source,
            "providerBilledUsdEquivalentNanos": (
                str(billed_nanos) if billed_nanos is not None else None
            ),
            "estimatedUsd": estimate.cost_usd,
            "estimateBasis": estimate.basis,
            "priceSource": resolved.source,
        },
        "billingValid": not issues,
        "reasonCodes": sorted(set(issues)),
    }


def _done_row(done: DoneEvent) -> dict[str, Any]:
    return {
        "role": "direct",
        "provider": done.provider or PROVIDER_ID,
        "model": done.model,
        "input_tokens": done.input_tokens,
        "output_tokens": done.output_tokens,
        "reasoning_tokens": done.reasoning_tokens,
        "cached_tokens": done.cached_tokens,
        "cache_write_tokens": done.cache_write_tokens,
        "billed_cost": done.billed_cost,
        "cost_source": done.cost_source,
        "billing_receipt": done.billing_receipt,
    }


def _failure_class(error: ErrorEvent | None, exception: BaseException | None) -> str | None:
    if error is not None:
        return classify_failure(f"{error.code} {error.message}")
    if exception is not None:
        return classify_failure(f"{type(exception).__name__}: {exception}")
    return None


async def _consume(
    provider: Any,
    *,
    prompt: str,
    request_timeout_seconds: float,
    outer_timeout_seconds: float,
) -> tuple[DoneEvent | None, ErrorEvent | None, BaseException | None, int]:
    started = time.monotonic()
    done: DoneEvent | None = None
    error: ErrorEvent | None = None
    exception: BaseException | None = None
    try:
        async with asyncio.timeout(outer_timeout_seconds):
            async for event in provider.chat(
                [Message(role="user", content=prompt)],
                config=ChatConfig(
                    max_tokens=OUTPUT_BUDGET_TOKENS,
                    temperature=None,
                    timeout=request_timeout_seconds,
                ),
            ):
                if isinstance(event, DoneEvent):
                    done = event
                elif isinstance(event, ErrorEvent):
                    error = event
    except Exception as exc:  # noqa: BLE001 - report only a bounded failure class
        exception = exc
    latency_ms = int((time.monotonic() - started) * 1000)
    return done, error, exception, latency_ms


def _scenario_report(
    *,
    scenario_id: str,
    kind: str,
    done: DoneEvent | None,
    error: ErrorEvent | None,
    exception: BaseException | None,
    latency_ms: int,
    expected_physical_requests: int,
    tier: str | None = None,
    effective_quorum: int | None = None,
) -> dict[str, Any]:
    breakdown: list[dict[str, Any]] = []
    usage_missing_count = 0
    if done is not None:
        breakdown = (
            list(done.model_usage_breakdown)
            if kind == "b5_ensemble"
            else [_done_row(done)]
        )
        usage_missing_count = done.usage_missing_count
    elif error is not None:
        breakdown = list(error.model_usage_breakdown)
        usage_missing_count = error.usage_missing_count

    items = [
        _physical_item(row, ordinal)
        for ordinal, row in enumerate(breakdown)
        if isinstance(row, Mapping)
    ]
    reason_codes: list[str] = []
    observed_requests = len(items) + max(0, int(usage_missing_count or 0))
    if observed_requests != expected_physical_requests:
        reason_codes.append("physical_request_count_mismatch")
    if usage_missing_count:
        reason_codes.append("physical_request_usage_missing")
    if any(not item["billingValid"] for item in items):
        reason_codes.append("physical_receipt_invalid")
    if done is None:
        reason_codes.append("terminal_done_missing")

    confirmed = [item for item in items if (item.get("receipt") or {}).get("status") == "confirmed"]
    pending = [item for item in items if (item.get("receipt") or {}).get("status") == "pending"]
    native_nanos = sum(
        int(item["receipt"]["amountNanos"])
        for item in confirmed
        if item["receipt"].get("amountNanos") is not None
    )
    usd_nanos = sum(
        int(item["receipt"]["usdEquivalentNanos"])
        for item in confirmed
        if item["receipt"].get("usdEquivalentNanos") is not None
    )

    if done is not None:
        outer_billed = _money_nanos(done.billed_cost)
        if outer_billed is None or abs(outer_billed - usd_nanos) > 1:
            reason_codes.append("envelope_billed_cost_mismatch")
        if done.input_tokens != sum(item["usage"]["inputTokens"] for item in items):
            reason_codes.append("envelope_input_tokens_mismatch")
        if done.output_tokens != sum(item["usage"]["outputTokens"] for item in items):
            reason_codes.append("envelope_output_tokens_mismatch")
        if done.reasoning_tokens != sum(
            item["usage"]["reasoningTokens"] for item in items
        ):
            reason_codes.append("envelope_reasoning_tokens_mismatch")
        if done.cached_tokens != sum(
            item["usage"]["cacheReadTokens"] for item in items
        ):
            reason_codes.append("envelope_cache_read_tokens_mismatch")
        if done.cache_write_tokens != sum(
            item["usage"]["cacheWriteTokens"] for item in items
        ):
            reason_codes.append("envelope_cache_write_tokens_mismatch")

    status = "failed"
    if not reason_codes:
        status = "pending" if pending else "passed"
    return {
        "id": scenario_id,
        "kind": kind,
        "tier": tier,
        "effectiveQuorum": effective_quorum,
        "status": status,
        "failureClass": _failure_class(error, exception),
        "outputBudgetTokens": OUTPUT_BUDGET_TOKENS,
        "latencyMs": latency_ms,
        "expectedPhysicalRequests": expected_physical_requests,
        "observedPhysicalRequests": observed_requests,
        "usageMissingCount": usage_missing_count,
        "physicalRequests": items,
        "totals": {
            "confirmedReceiptCount": len(confirmed),
            "pendingReceiptCount": len(pending),
            "nativeCnyAmountNanos": str(native_nanos),
            "usdEquivalentNanos": str(usd_nanos),
        },
        "reasonCodes": sorted(set(reason_codes)),
    }


def _provider_config(api_key: str, base_url: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        provider=PROVIDER_ID,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def _build_tokenrhythm_ensemble(
    *,
    api_key: str,
    base_url: str,
    strict: bool,
    request_timeout_seconds: float,
) -> EnsembleProvider:
    tiers = _inline_tokenrhythm_tiers()
    inherited = _provider_config(api_key, base_url, str(tiers["c1"]["model"]))
    config = GatewayConfig.model_validate(
        {
            "llm": {
                "provider": PROVIDER_ID,
                "model": inherited.model,
                "base_url": base_url,
            },
            "squilla_router": {"enabled": False},
            "llm_ensemble": {
                "enabled": True,
                "selection_mode": "static_tokenrhythm_b5",
                "min_successful_proposers": 4 if strict else 1,
                "proposer_timeout_seconds": request_timeout_seconds,
                "aggregator_timeout_seconds": request_timeout_seconds,
                "shuffle_candidates": False,
                "record_candidates": False,
                "proposer_tools": False,
            },
        }
    )
    return build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=_build_provider(inherited),
    )


async def _run_direct_scenario(
    *,
    scenario_id: str,
    kind: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    tier: str | None = None,
) -> dict[str, Any]:
    provider = _build_provider(_provider_config(api_key, base_url, model))
    done, error, exception, latency_ms = await _consume(
        provider,
        prompt=_DIRECT_PROMPT,
        request_timeout_seconds=timeout_seconds,
        outer_timeout_seconds=timeout_seconds + 15.0,
    )
    return _scenario_report(
        scenario_id=scenario_id,
        kind=kind,
        done=done,
        error=error,
        exception=exception,
        latency_ms=latency_ms,
        expected_physical_requests=1,
        tier=tier,
    )


async def _run_ensemble_scenario(
    *,
    api_key: str,
    base_url: str,
    strict: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    provider = _build_tokenrhythm_ensemble(
        api_key=api_key,
        base_url=base_url,
        strict=strict,
        request_timeout_seconds=timeout_seconds,
    )
    done, error, exception, latency_ms = await _consume(
        provider,
        prompt=_ENSEMBLE_PROMPT,
        request_timeout_seconds=timeout_seconds,
        outer_timeout_seconds=(timeout_seconds * 2) + 30.0,
    )
    return _scenario_report(
        scenario_id="b5_strict_quorum" if strict else "b5_default_quorum",
        kind="b5_ensemble",
        done=done,
        error=error,
        exception=exception,
        latency_ms=latency_ms,
        expected_physical_requests=5,
        effective_quorum=provider.min_successful_proposers,
    )


async def _run_all_scenarios(
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    tiers = _inline_tokenrhythm_tiers()
    scenarios = [
        await _run_direct_scenario(
            scenario_id="single_model",
            kind="single_model",
            api_key=api_key,
            base_url=base_url,
            model=str(tiers["c1"]["model"]),
            timeout_seconds=timeout_seconds,
        )
    ]
    for tier in TEXT_TIERS:
        scenarios.append(
            await _run_direct_scenario(
                scenario_id=f"inline_router_{tier}",
                kind="inline_router_tier",
                api_key=api_key,
                base_url=base_url,
                model=str(tiers[tier]["model"]),
                timeout_seconds=timeout_seconds,
                tier=tier,
            )
        )
    scenarios.append(
        await _run_ensemble_scenario(
            api_key=api_key,
            base_url=base_url,
            strict=False,
            timeout_seconds=timeout_seconds,
        )
    )
    scenarios.append(
        await _run_ensemble_scenario(
            api_key=api_key,
            base_url=base_url,
            strict=True,
            timeout_seconds=timeout_seconds,
        )
    )

    all_items = [
        item
        for scenario in scenarios
        for item in scenario.get("physicalRequests", [])
    ]
    native_total = sum(
        int(item["receipt"]["amountNanos"])
        for item in all_items
        if (item.get("receipt") or {}).get("status") == "confirmed"
        and item["receipt"].get("amountNanos") is not None
    )
    usd_total = sum(
        int(item["receipt"]["usdEquivalentNanos"])
        for item in all_items
        if (item.get("receipt") or {}).get("status") == "confirmed"
        and item["receipt"].get("usdEquivalentNanos") is not None
    )
    return {
        "schemaVersion": 1,
        "provider": PROVIDER_ID,
        "generatedAtMs": int(time.time() * 1000),
        "normalizationRateNativePerUsd": "6.975",
        "inlineRouter": {
            "persistedTierProfile": False,
            "tierModels": {tier: str(tiers[tier]["model"]) for tier in TEXT_TIERS},
        },
        "scenarios": scenarios,
        "summary": {
            "passedScenarioCount": sum(row["status"] == "passed" for row in scenarios),
            "pendingScenarioCount": sum(row["status"] == "pending" for row in scenarios),
            "failedScenarioCount": sum(row["status"] == "failed" for row in scenarios),
            "nativeCnyAmountNanos": str(native_total),
            "usdEquivalentNanos": str(usd_total),
        },
    }


def _assert_report_safe(report: Any, secrets: Mapping[str, str]) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).strip().lower() in _FORBIDDEN_REPORT_FIELDS:
                    raise RuntimeError("unsafe field rejected from billing audit report")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(report)
    if report_contains_secret(report, secrets):
        raise RuntimeError("credential detected in billing audit report")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run up to 15 billable TokenRhythm requests (single, four inline "
            "tiers, and two five-leg B5 scenarios)."
        )
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--confirm-live-cost", action="store_true")
    parser.add_argument("--confirm-rotated-key", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if not is_temporary_report_path(output):
        parser.error("--output must be inside the system temporary directory")
    if not args.confirm_live_cost:
        output.unlink(missing_ok=True)
        print("live billing audit requires --confirm-live-cost", file=sys.stderr)
        return 2
    if not args.confirm_rotated_key:
        output.unlink(missing_ok=True)
        print("live billing audit requires --confirm-rotated-key", file=sys.stderr)
        return 2
    if not 30.0 <= args.timeout_seconds <= 900.0:
        output.unlink(missing_ok=True)
        print("--timeout-seconds must be between 30 and 900", file=sys.stderr)
        return 2

    spec = get_provider_spec(PROVIDER_ID)
    api_key = os.environ.get(spec.env_key, "").strip()
    if not api_key:
        output.unlink(missing_ok=True)
        print(f"{KEY_ENV} must contain a rotated live audit key", file=sys.stderr)
        return 2
    secrets = {KEY_ENV: api_key}

    try:
        requested_base_url = os.environ.get(BASE_URL_ENV, "").strip()
        base_url = registry_endpoint(PROVIDER_ID, requested_base_url or None)
        report = asyncio.run(
            _run_all_scenarios(
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=args.timeout_seconds,
            )
        )
        report = sanitize_report(report, secrets)
        _assert_report_safe(report, secrets)
        report = write_safe_report(output, report, secrets)
        _assert_report_safe(report, secrets)
    except Exception as exc:  # noqa: BLE001 - never emit provider exception bodies
        output.unlink(missing_ok=True)
        # Exception messages can contain upstream bodies. Emit only the local
        # exception class and bounded failure category.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failureClass": classify_failure(type(exc).__name__),
                    "exceptionClass": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    summary = dict(report.get("summary") or {})
    print(json.dumps({"status": "complete", "summary": summary}, sort_keys=True))
    return 0 if summary.get("failedScenarioCount") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
