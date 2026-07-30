#!/usr/bin/env python3
"""Validate mixed-provider Router and Ensemble execution through one Gateway.

This is an opt-in, finite live harness.  It reads credentials from an explicit
data file, starts one isolated Gateway on a random loopback port, and mutates
only that Gateway's temporary configuration.  Secret values are passed solely
through the child environment: they are never put in argv, TOML, prompts, or
reports.  Provider endpoints are always taken from the runtime registry.

Example (only providers that passed the probe matrix should be supplied)::

    uv run python scripts/live_mixed_provider_gateway.py \
      --secrets-file /path/to/provider.keys \
      --target deepseek=deepseek-chat \
      --target gemini=gemini-2.5-flash-lite \
      --target dashscope=qwen3.6-flash \
      --target zhipu=glm-4.5-flash \
      --output /tmp/opensquilla-mixed-provider.json

The process exits non-zero when the acceptance floor is not met.  In
particular, fewer than four successful Router deployments or no successful
three-provider Ensemble is reported as ``incomplete`` rather than passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.gateway_client import GatewayRPCClient, GatewayRPCError  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    classify_failure,
    is_premium_model,
    is_temporary_report_path,
    minimal_child_environment,
    parse_provider_keys_file,
    provider_response_model_matches,
    redact_text,
    registry_endpoint,
    report_contains_secret,
    sanitize_report,
    scan_and_remove_temporary_tree,
    write_safe_report,
)
from scripts.smoke_v4_phase3_router import (  # noqa: E402
    _free_port,
    _post_json,
    _read_turn_call_records,
    _stop_gateway,
    _wait_for_assistant_reply,
    _wait_for_gateway_health,
)

ROUTER_MAX_TOKENS_LIMIT = 64
ENSEMBLE_MAX_TOKENS_LIMIT = 256
ROUTER_MIN_TOKENS = 32
ENSEMBLE_MIN_TOKENS = 128
DEFAULT_ROUTER_MAX_TOKENS = 48
DEFAULT_ENSEMBLE_MAX_TOKENS = 192
MIN_ROUTER_PROVIDERS = 4
BAD_MODEL_ID = "opensquilla-live/nonexistent-model"
MISSING_ENV_SENTINEL = "OPENSQUILLA_LIVE_INTENTIONALLY_MISSING_PROVIDER_KEY"
# RFC 2606 reserves ``.invalid`` for names that never resolve.  The negative
# credential cases pair this cross-origin endpoint with an empty profile so
# the resolver cannot silently reacquire the provider's registry env key.  No
# request reaches this endpoint because deployment readiness fails first.
MISSING_CREDENTIAL_BASE_URL = "https://opensquilla-live.invalid/v1"
TEXT_TIERS = ("c0", "c1", "c2", "c3")

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/+-]+$")
_PRIVATE_PROVIDER_FIELDS = frozenset(
    {
        "encrypted_content",
        "provider_state",
        "reasoning_details",
        "signature",
        "thinking_signature",
    }
)
_TRANSIENT_RE = re.compile(
    r"(?:\b429\b|rate.?limit|timeout|timed out|transport|connection|\b5\d\d\b)",
    re.IGNORECASE,
)
_PUBLIC_RESULT_KEYS = frozenset(
    {
        "provider",
        "model",
        "status",
        "failure_class",
        "usage",
        "cost",
        "latency_ms",
    }
)


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    provider: str
    model: str
    env_key: str
    endpoint: str

    @property
    def identity(self) -> tuple[str, str]:
        return (self.provider, self.model)

    def public(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


def _provider_response_identity_matches(
    target: DeploymentTarget,
    identity: Mapping[str, Any],
) -> bool:
    """Match one provider-reported identity without relaxing provider/request identity."""

    return str(identity.get("provider") or "") == target.provider and (
        provider_response_model_matches(
            target.provider,
            target.model,
            str(identity.get("model") or ""),
        )
    )


def parse_target(value: str) -> DeploymentTarget:
    """Parse one registry-backed ``provider=model`` deployment target."""

    provider_raw, separator, model_raw = str(value or "").partition("=")
    provider = provider_raw.strip().lower()
    model = model_raw.strip()
    if not separator or not provider or not model:
        raise ValueError("targets must use provider=model with both values non-empty")
    if not _SAFE_TOKEN_RE.fullmatch(provider) or not _SAFE_TOKEN_RE.fullmatch(model):
        raise ValueError("provider and model may contain only model-id characters")
    if is_premium_model(model):
        raise ValueError(f"premium model rejected for live plumbing matrix: {provider}/{model}")
    spec = get_provider_spec(provider)
    if not spec.runtime_supported:
        raise ValueError(f"provider {provider!r} is not runtime-supported")
    endpoint = registry_endpoint(provider)
    if spec.requires_api_key() and not spec.env_key:
        raise ValueError(f"provider {provider!r} has no registry credential variable")
    return DeploymentTarget(
        provider=provider,
        model=model,
        env_key=str(spec.env_key or ""),
        endpoint=endpoint,
    )


def normalize_targets(values: Iterable[str]) -> list[DeploymentTarget]:
    targets: list[DeploymentTarget] = []
    seen_providers: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    for value in values:
        target = parse_target(value)
        if target.provider in seen_providers:
            raise ValueError(f"provider {target.provider!r} was supplied more than once")
        if target.identity in seen_identities:
            raise ValueError(f"duplicate deployment target {target.provider}/{target.model}")
        seen_providers.add(target.provider)
        seen_identities.add(target.identity)
        targets.append(target)
    if not targets:
        raise ValueError("at least one --target is required")
    return targets


def credential_preflight(
    targets: Sequence[DeploymentTarget],
    secrets: Mapping[str, str],
) -> tuple[list[DeploymentTarget], list[dict[str, Any]]]:
    """Return executable targets and fail-closed missing-key rows.

    This function performs no network or process operation.  It is the first
    executable gate in ``run_live_matrix`` so a missing key cannot reach a
    Gateway or provider adapter.
    """

    ready: list[DeploymentTarget] = []
    blocked: list[dict[str, Any]] = []
    for target in targets:
        spec = get_provider_spec(target.provider)
        if spec.requires_api_key() and not str(secrets.get(target.env_key, "") or ""):
            blocked.append(
                {
                    **target.public(),
                    "ok": False,
                    "failure_class": "missing-credential",
                    "blocked_before_gateway": True,
                    "network_requests": 0,
                }
            )
        else:
            ready.append(target)
    return ready, blocked


def _project_public_result(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one in-memory result onto the only persisted report shape."""

    failure_class = row.get("failure_class")
    status = str(row.get("status") or "")
    if not status:
        if row.get("ok") is True:
            status = "passed"
        elif failure_class == "missing-credential":
            status = "skipped"
        else:
            status = "failed"
    if status == "passed":
        failure_class = None
    elif failure_class is None:
        failure_class = "implementation"

    usage = row.get("usage")
    cost = row.get("cost")
    return {
        "provider": str(row.get("provider") or ""),
        "model": str(row.get("model") or ""),
        "status": status,
        "failure_class": str(failure_class) if failure_class is not None else None,
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "cost": dict(cost) if isinstance(cost, Mapping) else {},
        "latency_ms": int(row.get("latency_ms") or 0),
    }


def _assert_public_report_schema(report: Any) -> None:
    """Require an array of exact public rows before and after sanitizing."""

    if not isinstance(report, list):
        raise RuntimeError("public live report must be a JSON array")
    for index, row in enumerate(report):
        if not isinstance(row, dict) or set(row) != _PUBLIC_RESULT_KEYS:
            raise RuntimeError(f"public live report row {index} has an invalid field set")
        if not all(isinstance(row[field], str) for field in ("provider", "model", "status")):
            raise RuntimeError(f"public live report row {index} has an invalid identity")
        if row["failure_class"] is not None and not isinstance(row["failure_class"], str):
            raise RuntimeError(f"public live report row {index} has an invalid failure class")
        if not isinstance(row["usage"], dict) or not isinstance(row["cost"], dict):
            raise RuntimeError(f"public live report row {index} has invalid accounting fields")
        if isinstance(row["latency_ms"], bool) or not isinstance(row["latency_ms"], int | float):
            raise RuntimeError(f"public live report row {index} has an invalid latency")


def _public_identity_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    provider = str(expected.get("provider") or "")
    return provider == str(actual.get("provider") or "") and provider_response_model_matches(
        provider,
        str(expected.get("model") or ""),
        str(actual.get("model") or ""),
    )


def _public_report_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten executed deployments and keep decisions/traces only in memory."""

    public_rows: list[dict[str, Any]] = []
    preflight = report.get("preflight")
    preflight = preflight if isinstance(preflight, Mapping) else {}
    blocked = [row for row in preflight.get("blocked") or [] if isinstance(row, Mapping)]
    ready = [row for row in preflight.get("ready") or [] if isinstance(row, Mapping)]
    if blocked:
        public_rows.extend(
            _project_public_result(
                {
                    **row,
                    "status": "skipped",
                    "failure_class": "blocked-by-preflight",
                }
            )
            for row in ready
        )
        public_rows.extend(_project_public_result(row) for row in blocked)

    router = report.get("router")
    router = router if isinstance(router, Mapping) else {}
    public_rows.extend(
        _project_public_result(row)
        for row in router.get("targets") or []
        if isinstance(row, Mapping)
    )

    ensemble = report.get("ensemble")
    ensemble = ensemble if isinstance(ensemble, Mapping) else {}
    for lineup in ensemble.get("lineups") or []:
        if not isinstance(lineup, Mapping):
            continue
        lineup_ok = lineup.get("ok") is True
        lineup_failure = None if lineup_ok else str(
            lineup.get("failure_class") or "implementation"
        )
        breakdown = [
            row for row in lineup.get("usage_breakdown") or [] if isinstance(row, Mapping)
        ]
        public_rows.extend(
            _project_public_result(
                {
                    **row,
                    "status": "passed" if lineup_ok else "failed",
                    "failure_class": lineup_failure,
                }
            )
            for row in breakdown
        )

        expected_members = [
            row for row in lineup.get("proposers") or [] if isinstance(row, Mapping)
        ]
        aggregator = lineup.get("aggregator")
        if isinstance(aggregator, Mapping):
            expected_members.append(aggregator)
        for member in expected_members:
            if any(_public_identity_matches(member, row) for row in breakdown):
                continue
            public_rows.append(
                _project_public_result(
                    {
                        **member,
                        "status": "failed",
                        "failure_class": lineup_failure or "implementation",
                    }
                )
            )

    if not public_rows and ready:
        public_rows.extend(
            _project_public_result(
                {
                    **row,
                    "status": "failed",
                    "failure_class": report.get("failure_class") or "implementation",
                }
            )
            for row in ready
        )

    _assert_public_report_schema(public_rows)
    return public_rows


def _emit_main_diagnostics(
    report: Mapping[str, Any],
    *,
    file_mode: int,
    permission_warning: str | None,
    ignored_line_numbers: tuple[int, ...],
    secrets: Mapping[str, str],
) -> None:
    """Send source hygiene and coverage metadata to stderr, never the report."""

    preflight = report.get("preflight")
    preflight = preflight if isinstance(preflight, Mapping) else {}
    router = report.get("router")
    router = router if isinstance(router, Mapping) else {}
    ensemble = report.get("ensemble")
    ensemble = ensemble if isinstance(ensemble, Mapping) else {}
    diagnostic = {
        "source_file_mode": f"{file_mode:04o}",
        "permission_warning": permission_warning,
        "ignored_line_numbers": list(ignored_line_numbers),
        "coverage": {
            "status": str(report.get("status") or "incomplete"),
            "ready_provider_count": len(preflight.get("ready") or []),
            "blocked_provider_count": len(preflight.get("blocked") or []),
            "router_successful_provider_count": int(
                router.get("successful_provider_count") or 0
            ),
            "router_required_provider_count": int(router.get("required_provider_count") or 0),
            "ensemble_lineup_count": len(ensemble.get("lineups") or []),
            "ensemble_status": str(ensemble.get("status") or "not-run"),
        },
    }
    message = "live mixed-provider diagnostics: " + json.dumps(
        diagnostic,
        ensure_ascii=False,
        sort_keys=True,
    )
    print(redact_text(message, secrets), file=sys.stderr)


def rotating_triples(
    targets: Sequence[DeploymentTarget],
) -> list[tuple[DeploymentTarget, DeploymentTarget, DeploymentTarget]]:
    """Build deterministic 2-proposer + 1-aggregator groups covering every target."""

    if len(targets) < 3:
        return []
    rows: list[tuple[DeploymentTarget, DeploymentTarget, DeploymentTarget]] = []
    for start in range(0, len(targets), 3):
        group = tuple(targets[(start + offset) % len(targets)] for offset in range(3))
        if len({target.provider for target in group}) == 3:
            rows.append(group)  # type: ignore[arg-type]
    return rows


def build_gateway_environment(
    targets: Sequence[DeploymentTarget],
    secrets: Mapping[str, str],
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create the Gateway child environment with only selected file credentials."""

    env = minimal_child_environment(base_environment)
    for target in targets:
        secret = str(secrets.get(target.env_key, "") or "")
        if secret:
            env[target.env_key] = secret
    env["OPENSQUILLA_LIVE_DISABLE_DOTENV"] = "1"
    return env


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_gateway_config(
    targets: Sequence[DeploymentTarget],
    *,
    router_max_tokens: int,
) -> str:
    """Render secret-free TOML for the isolated Gateway."""

    if not targets:
        raise ValueError("cannot render a Gateway without a primary target")
    if not ROUTER_MIN_TOKENS <= router_max_tokens <= ROUTER_MAX_TOKENS_LIMIT:
        raise ValueError(
            f"router max tokens must be within {ROUTER_MIN_TOKENS}..{ROUTER_MAX_TOKENS_LIMIT}"
        )
    primary = targets[0]
    lines = [
        'host = "127.0.0.1"',
        "debug = false",
        "llm_request_timeout_seconds = 90",
        "agent_runtime_timeout_seconds = 120",
        "agent_max_iterations = 2",
        "agent_max_provider_retries = 0",
        "",
        "[auth]",
        'mode = "none"',
        "",
        "[control_ui]",
        "enabled = false",
        "",
        "[rate_limit]",
        "enabled = false",
        "",
        "[privacy]",
        "disable_network_observability = true",
        "",
        "[tools]",
        'profile = "minimal"',
        'deny = ["*"]',
        "",
        "[memory]",
        'source = "state"',
        "",
        "[llm]",
        f"provider = {_toml_string(primary.provider)}",
        f"model = {_toml_string(primary.model)}",
        f"api_key_env = {_toml_string(primary.env_key)}",
        f"base_url = {_toml_string(primary.endpoint)}",
        f"max_tokens = {router_max_tokens}",
        # Router/Ensemble plumbing has separate thinking-on/off coverage in
        # the baseline matrix.  Keep this bounded marker run non-thinking so
        # controller-selected reasoning cannot consume the 32-64 token budget.
        'thinking = "off"',
        "",
        "[squilla_router]",
        "enabled = false",
        "cross_provider_tiers = true",
        'tier_provider_mismatch = "veto"',
        "",
        "[llm_ensemble]",
        "enabled = false",
        'selection_mode = "router_dynamic"',
    ]
    for target in targets:
        lines.extend(
            [
                "",
                f"[llm_profiles.{target.provider}]",
                f"api_key_env = {_toml_string(target.env_key)}",
                f"base_url = {_toml_string(target.endpoint)}",
            ]
        )
    return "\n".join(lines) + "\n"


def forced_router_payload(
    target: DeploymentTarget,
    *,
    forced_tier: str = "c0",
) -> dict[str, Any]:
    if forced_tier not in TEXT_TIERS:
        raise ValueError(f"unknown forced Router tier {forced_tier!r}")
    tier = {
        "provider": target.provider,
        "model": target.model,
        "supportsImage": False,
        "imageOnly": False,
        "thinking": False,
        "thinkingLevel": "off",
    }
    tiers = {name: {**tier, "imageOnly": name != forced_tier} for name in TEXT_TIERS}
    # Move the image-only row as well so profile removal tests cannot be held
    # by an unrelated stale reference.  No live image is sent by this harness.
    tiers["image_model"] = {
        **tier,
        "supportsImage": True,
        "imageOnly": True,
    }
    return {
        "mode": "custom",
        "defaultTier": forced_tier,
        "crossProviderTiers": True,
        "tierProviderMismatch": "veto",
        "tiers": tiers,
    }


def _missing_credential_profile_payload(target: DeploymentTarget) -> dict[str, Any]:
    """Build a credentialless cross-origin draft that cannot use registry env fallback."""

    return {
        "providerId": target.provider,
        "apiKeyEnv": "",
        "apiKeyEnvPool": [MISSING_ENV_SENTINEL],
        "baseUrl": MISSING_CREDENTIAL_BASE_URL,
    }


def ensemble_payload(
    group: tuple[DeploymentTarget, DeploymentTarget, DeploymentTarget],
    *,
    bad_proposer: bool = False,
    bad_aggregator: bool = False,
    all_failed_policy: str = "error",
) -> dict[str, Any]:
    first, second, aggregator = group
    return {
        "enabled": True,
        "selectionMode": "custom_b5",
        "candidates": [
            {**first.public(), "role": "primary", "enabled": True},
            {
                "provider": second.provider,
                "model": BAD_MODEL_ID if bad_proposer else second.model,
                "role": "contrast",
                "enabled": True,
            },
            {
                "provider": aggregator.provider,
                "model": BAD_MODEL_ID if bad_aggregator else aggregator.model,
                "role": "aggregator",
                "enabled": True,
            },
        ],
        "minSuccessfulProposers": 1 if bad_proposer else 2,
        "allFailedPolicy": all_failed_policy,
    }


def _contains_private_provider_state(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _PRIVATE_PROVIDER_FIELDS:
                return True
            if _contains_private_provider_state(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_private_provider_state(item) for item in value)
    return False


def _usage_total(usage: Mapping[str, Any]) -> int:
    return sum(
        int(usage.get(name) or 0) for name in ("input_tokens", "output_tokens", "reasoning_tokens")
    )


def _failure_class(error: Any) -> str | None:
    if not str(error or ""):
        return None
    return classify_failure(error)


def _is_transient(observation: Mapping[str, Any]) -> bool:
    return bool(_TRANSIENT_RE.search(str(observation.get("error") or "")))


def _last_record(
    records: Sequence[Mapping[str, Any]],
    *,
    session_key: str,
    kind: str,
) -> Mapping[str, Any]:
    return next(
        (
            record
            for record in reversed(records)
            if record.get("session_key") == session_key and record.get("kind") == kind
        ),
        {},
    )


def _record_identity(record: Mapping[str, Any]) -> dict[str, str]:
    payload = record.get("payload") or {}
    payload = payload if isinstance(payload, Mapping) else {}
    config = payload.get("config") or {}
    config = config if isinstance(config, Mapping) else {}
    return {
        "provider": str(record.get("provider") or ""),
        "model": str(config.get("model") or record.get("model") or ""),
    }


def _request_identities(
    records: Sequence[Mapping[str, Any]],
    *,
    session_key: str,
) -> list[dict[str, str]]:
    """Return every provider request identity for one session in log order."""

    return [
        _record_identity(record)
        for record in records
        if record.get("session_key") == session_key and record.get("kind") == "llm_request"
    ]


def _summarize_ensemble_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for value in trace.get("candidates") or []:
        if not isinstance(value, Mapping):
            continue
        candidates.append(
            {
                "provider": str(value.get("provider") or ""),
                "model": str(value.get("model") or ""),
                "ok": value.get("ok") is True,
                "usage": {
                    "input_tokens": int(value.get("input_tokens") or 0),
                    "output_tokens": int(value.get("output_tokens") or 0),
                },
                "latency_ms": int(value.get("elapsed_ms") or 0),
                "cost": {"billed_cost_usd": float(value.get("billed_cost") or 0.0)},
                "failure_class": _failure_class(value.get("error")),
            }
        )
    final_request = trace.get("final_request") or {}
    execution = final_request.get("execution") if isinstance(final_request, Mapping) else {}
    execution = execution if isinstance(execution, Mapping) else {}
    final_usage = final_request.get("usage") if isinstance(final_request, Mapping) else {}
    final_usage = final_usage if isinstance(final_usage, Mapping) else {}
    return {
        "successful_proposers": int(trace.get("successful_proposers") or 0),
        "total_candidates": int(trace.get("total_candidates") or 0),
        "fallback_used": trace.get("fallback_used") is True,
        "fallback_reason": str(trace.get("fallback_reason") or ""),
        "llm_request_count": int(trace.get("llm_request_count") or 0),
        "candidates": candidates,
        "aggregator": {
            "provider": str(execution.get("provider") or ""),
            "model": str(execution.get("model") or ""),
            "role": str(execution.get("role") or final_request.get("role") or ""),
            "usage": {
                "input_tokens": int(final_usage.get("input_tokens") or 0),
                "output_tokens": int(final_usage.get("output_tokens") or 0),
            },
            "latency_ms": int(final_usage.get("elapsed_ms") or 0),
            "cost": {"billed_cost_usd": float(final_usage.get("billed_cost") or 0.0)},
        },
    }


def _observation_from_records(
    *,
    records: Sequence[Mapping[str, Any]],
    session_key: str,
    marker: str,
    assistant_text: str,
    error: str | None,
    decision: Mapping[str, Any] | None,
    expected_prior_marker: str | None = None,
) -> dict[str, Any]:
    request = _last_record(records, session_key=session_key, kind="llm_request")
    response = _last_record(records, session_key=session_key, kind="llm_response")
    llm_error = _last_record(records, session_key=session_key, kind="llm_error")
    request_payload = request.get("payload") if isinstance(request, Mapping) else {}
    request_payload = request_payload if isinstance(request_payload, Mapping) else {}
    response_payload = response.get("payload") if isinstance(response, Mapping) else {}
    response_payload = response_payload if isinstance(response_payload, Mapping) else {}
    usage = response_payload.get("usage") or {}
    usage = usage if isinstance(usage, Mapping) else {}
    error_payload = llm_error.get("payload") if isinstance(llm_error, Mapping) else {}
    error_payload = error_payload if isinstance(error_payload, Mapping) else {}
    trace = response_payload.get("ensemble_trace") or {}
    trace = trace if isinstance(trace, Mapping) else {}
    effective_error = error or (error_payload.get("error") or {}).get("message")
    request_config = request_payload.get("config") or {}
    request_config = request_config if isinstance(request_config, Mapping) else {}
    request_tools = request_payload.get("tools")
    prior_assistant_context_present = (
        True
        if expected_prior_marker is None
        else expected_prior_marker
        in json.dumps(request_payload.get("messages") or [], ensure_ascii=False, sort_keys=True)
    )
    request_identities = _request_identities(records, session_key=session_key)
    return {
        "session_key": session_key,
        "marker_present": bool(marker and marker in assistant_text),
        "assistant_present": bool(assistant_text.strip()),
        "error": str(effective_error or ""),
        "failure_class": _failure_class(effective_error),
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "billed_cost": float(usage.get("billed_cost") or 0.0),
        },
        "response_model": str(usage.get("model") or ""),
        "request_identity": _record_identity(request),
        "request_identities": request_identities,
        "response_identity": {
            "provider": str(response.get("provider") or ""),
            "model": str(response.get("model") or ""),
        },
        "latency_ms": int(response_payload.get("duration_ms") or 0),
        "cost": {"billed_cost_usd": float(usage.get("billed_cost") or 0.0)},
        "decision": dict(decision or {}),
        "private_provider_state_replayed": _contains_private_provider_state(request_payload),
        "prior_assistant_context_present": prior_assistant_context_present,
        # The turn-call logger omits ``tools`` when no tools are available.
        # Both omission/JSON null and an explicit empty list mean the same
        # thing; a non-empty or malformed value must still fail validation.
        "tools_empty": bool(request) and request_tools in (None, []),
        "ensemble": _summarize_ensemble_trace(trace) if trace else {},
        "usage_breakdown": [
            {
                "provider": str(row.get("provider") or ""),
                "model": str(row.get("model") or ""),
                "role": str(row.get("role") or ""),
                "usage": {
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
                },
                "latency_ms": int(row.get("elapsed_ms") or 0),
                "cost": {"billed_cost_usd": float(row.get("billed_cost") or 0.0)},
            }
            for row in usage.get("model_usage_breakdown") or []
            if isinstance(row, Mapping)
        ],
    }


def validate_router_observation(
    observation: Mapping[str, Any],
    target: DeploymentTarget,
    *,
    expected_tier: str,
) -> tuple[bool, list[str]]:
    decision = observation.get("decision") or {}
    decision = decision if isinstance(decision, Mapping) else {}
    failures: list[str] = []
    request_identity = observation.get("request_identity") or {}
    request_identity = request_identity if isinstance(request_identity, Mapping) else {}
    response_identity = observation.get("response_identity") or {}
    response_identity = response_identity if isinstance(response_identity, Mapping) else {}
    cost = observation.get("cost") or {}
    cost = cost if isinstance(cost, Mapping) else {}
    checks = {
        "marker_missing": observation.get("marker_present") is True,
        "usage_zero": _usage_total(observation.get("usage") or {}) > 0,
        "final_tier_mismatch": decision.get("finalTier") == expected_tier,
        "requested_provider_mismatch": decision.get("requestedProvider") == target.provider,
        "requested_model_mismatch": decision.get("requestedModel") == target.model,
        "executed_provider_mismatch": decision.get("executedProvider") == target.provider,
        "executed_model_mismatch": decision.get("executedModel") == target.model,
        "fallback_hops_nonzero": int(decision.get("fallbackHops") or 0) == 0,
        "provider_state_replayed": observation.get("private_provider_state_replayed") is False,
        "prior_assistant_context_missing": (
            observation.get("prior_assistant_context_present") is True
        ),
        "tools_not_empty": observation.get("tools_empty") is True,
        "request_provider_mismatch": request_identity.get("provider") == target.provider,
        "request_model_mismatch": request_identity.get("model") == target.model,
        "response_provider_mismatch": response_identity.get("provider") == target.provider,
        "response_model_mismatch": provider_response_model_matches(
            target.provider,
            target.model,
            str(response_identity.get("model") or ""),
        ),
        "provider_response_model_mismatch": provider_response_model_matches(
            target.provider,
            target.model,
            str(observation.get("response_model") or ""),
        ),
        "latency_missing": int(observation.get("latency_ms") or 0) > 0,
        "cost_missing": "billed_cost_usd" in cost,
    }
    failures.extend(name for name, ok in checks.items() if not ok)
    if observation.get("error"):
        failures.append("turn_error")
    return not failures, failures


def validate_ensemble_observation(
    observation: Mapping[str, Any],
    group: tuple[DeploymentTarget, DeploymentTarget, DeploymentTarget],
) -> tuple[bool, list[str]]:
    trace = observation.get("ensemble") or {}
    trace = trace if isinstance(trace, Mapping) else {}
    candidates = trace.get("candidates") or []
    successful_candidates = [
        row for row in candidates if isinstance(row, Mapping) and row.get("ok") is True
    ]
    aggregator = trace.get("aggregator") or {}
    aggregator = aggregator if isinstance(aggregator, Mapping) else {}
    breakdown = [
        row for row in observation.get("usage_breakdown") or [] if isinstance(row, Mapping)
    ]
    breakdown_evidence = all(
        _usage_total(row.get("usage") or {}) > 0
        and int(row.get("latency_ms") or 0) > 0
        and isinstance(row.get("cost"), Mapping)
        and "billed_cost_usd" in (row.get("cost") or {})
        for row in breakdown
        if any(_provider_response_identity_matches(target, row) for target in group)
    )
    breakdown_coverage = all(
        any(_provider_response_identity_matches(target, row) for row in breakdown)
        for target in group
    )
    candidate_evidence = all(
        _usage_total(row.get("usage") or {}) > 0
        and int(row.get("latency_ms") or 0) > 0
        and isinstance(row.get("cost"), Mapping)
        and "billed_cost_usd" in (row.get("cost") or {})
        for row in candidates
        if isinstance(row, Mapping) and row.get("ok") is True
    )
    cost = observation.get("cost") or {}
    cost = cost if isinstance(cost, Mapping) else {}
    failures: list[str] = []
    checks = {
        "marker_missing": observation.get("marker_present") is True,
        "usage_zero": _usage_total(observation.get("usage") or {}) > 0,
        "tools_not_empty": observation.get("tools_empty") is True,
        "latency_missing": int(observation.get("latency_ms") or 0) > 0,
        "cost_missing": "billed_cost_usd" in cost,
        # Candidate trace models are provider responses, not requested
        # deployment identities.  Keep the provider exact while accepting the
        # provider-scoped response aliases used everywhere else in the live
        # harness (for example deepseek-chat -> deepseek-v4-flash).
        "proposer_progress_incomplete": all(
            any(_provider_response_identity_matches(target, row) for row in successful_candidates)
            for target in group[:2]
        ),
        "proposer_evidence_incomplete": candidate_evidence,
        "aggregator_identity_mismatch": (
            str(aggregator.get("provider") or ""),
            str(aggregator.get("model") or ""),
        )
        == group[2].identity,
        "aggregator_response_model_mismatch": provider_response_model_matches(
            group[2].provider,
            group[2].model,
            str(observation.get("response_model") or ""),
        ),
        "usage_breakdown_incomplete": breakdown_coverage,
        "member_evidence_incomplete": breakdown_evidence,
        "request_count_mismatch": int(trace.get("llm_request_count") or 0) == 3,
        "fallback_used": trace.get("fallback_used") is False,
    }
    failures.extend(name for name, ok in checks.items() if not ok)
    if observation.get("error"):
        failures.append("turn_error")
    return not failures, failures


def _bad_proposer_quorum_ok(
    observation: Mapping[str, Any],
    group: tuple[DeploymentTarget, DeploymentTarget, DeploymentTarget],
) -> bool:
    """Validate that one unavailable proposer was isolated and quorum continued."""

    trace = observation.get("ensemble") or {}
    trace = trace if isinstance(trace, Mapping) else {}
    candidates = [
        row for row in trace.get("candidates") or [] if isinstance(row, Mapping)
    ]
    aggregator = trace.get("aggregator") or {}
    aggregator = aggregator if isinstance(aggregator, Mapping) else {}
    good_proposer_present = any(
        row.get("ok") is True and _provider_response_identity_matches(group[0], row)
        for row in candidates
    )
    bad_proposer_present = any(
        row.get("provider") == group[1].provider
        and row.get("model") == BAD_MODEL_ID
        and row.get("ok") is False
        for row in candidates
    )
    return bool(
        observation.get("marker_present") is True
        and not observation.get("error")
        and _usage_total(observation.get("usage") or {}) > 0
        and observation.get("tools_empty") is True
        and int(trace.get("successful_proposers") or 0) >= 1
        and int(trace.get("total_candidates") or 0) == 2
        and int(trace.get("llm_request_count") or 0) == 3
        and good_proposer_present
        and bad_proposer_present
        and trace.get("fallback_used") is False
        and (
            str(aggregator.get("provider") or ""),
            str(aggregator.get("model") or ""),
        )
        == group[2].identity
    )


def _forced_tier_for_index(index: int) -> str:
    return TEXT_TIERS[index % len(TEXT_TIERS)]


def _state_isolation_pair(
    targets: Sequence[DeploymentTarget],
) -> tuple[DeploymentTarget, DeploymentTarget] | None:
    if len(targets) < 2:
        return None
    first = targets[0]
    first_backend = get_provider_spec(first.provider).backend
    second = next(
        (
            target
            for target in targets[1:]
            if get_provider_spec(target.provider).backend != first_backend
        ),
        targets[1],
    )
    return (first, second)


def _router_result_row(
    *,
    target: DeploymentTarget,
    expected_tier: str,
    observation: Mapping[str, Any],
    ok: bool,
    failures: Sequence[str],
) -> dict[str, Any]:
    return {
        **target.public(),
        "expected_tier": expected_tier,
        "ok": ok,
        "failures": list(failures),
        "failure_class": (
            None if ok else str(observation.get("failure_class") or "implementation")
        ),
        "attempt_count": observation.get("attempt_count"),
        "decision": observation.get("decision"),
        "request_identity": observation.get("request_identity"),
        "response_identity": observation.get("response_identity"),
        "usage": observation.get("usage"),
        "cost": observation.get("cost"),
        "latency_ms": observation.get("latency_ms"),
        "marker_present": observation.get("marker_present"),
        "tools_empty": observation.get("tools_empty"),
    }


def _router_phase_ok(
    *,
    rows: Sequence[Mapping[str, Any]],
    target_count: int,
    state_switch: Mapping[str, Any],
    fail_closed: Mapping[str, Any],
) -> bool:
    successful_count = sum(row.get("ok") is True for row in rows)
    return bool(
        successful_count == target_count
        and successful_count >= MIN_ROUTER_PROVIDERS
        and state_switch.get("ok") is True
        and fail_closed.get("ok") is True
    )


def _fail_closed_request_audit(
    observation: Mapping[str, Any],
    *,
    primary: DeploymentTarget,
    foreign_model: str,
) -> tuple[bool, bool, list[Mapping[str, Any]]]:
    """Prove every request used the original primary deployment exactly."""

    raw_identities = observation.get("request_identities") or []
    identities = (
        [identity for identity in raw_identities if isinstance(identity, Mapping)]
        if isinstance(raw_identities, list)
        else []
    )
    expected = primary.public()
    all_requests_used_primary = bool(identities) and all(
        dict(identity) == expected for identity in identities
    )
    foreign_model_sent_to_primary = any(
        identity.get("provider") == primary.provider and identity.get("model") == foreign_model
        for identity in identities
    )
    return all_requests_used_primary, foreign_model_sent_to_primary, identities


async def _rpc_call(port: int, method: str, params: dict[str, Any]) -> Any:
    client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=30.0)
    await client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        return await client.call(method, params)
    finally:
        await client.close()


async def _send_turn(
    *,
    port: int,
    turn_log_dir: Path,
    session_key: str,
    marker: str,
    timeout_seconds: float,
    previous_assistant_count: int = 0,
    intent: str = "new_chat",
    expected_prior_marker: str | None = None,
) -> dict[str, Any]:
    prompt = (
        "Do not call tools. Reply with one short sentence containing exactly this synthetic "
        f"test marker: {marker}"
    )
    try:
        await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{port}/api/chat",
            {"sessionKey": session_key, "message": prompt, "intent": intent},
            10.0,
        )
        assistant, _history, error = await asyncio.to_thread(
            _wait_for_assistant_reply,
            port=port,
            session_key=session_key,
            previous_assistant_count=previous_assistant_count,
            timeout_seconds=timeout_seconds,
        )
        assistant_text = str((assistant or {}).get("text") or "")
    except Exception as exc:  # noqa: BLE001 - live harness must keep matrix running
        assistant_text = ""
        error = f"{type(exc).__name__}: {exc}"
    records = await asyncio.to_thread(_read_turn_call_records, turn_log_dir)
    try:
        decision_payload = await _rpc_call(
            port,
            "router.decisions.list",
            {"sessionKey": session_key, "limit": 1},
        )
        decisions = decision_payload.get("decisions") or []
        decision = decisions[0] if decisions else {}
    except Exception:  # Router decision absence is reported by validation.
        decision = {}
    return _observation_from_records(
        records=records,
        session_key=session_key,
        marker=marker,
        assistant_text=assistant_text,
        error=error,
        decision=decision,
        expected_prior_marker=expected_prior_marker,
    )


async def _send_with_retry(
    *,
    port: int,
    turn_log_dir: Path,
    session_prefix: str,
    marker: str,
    timeout_seconds: float,
    transient_retries: int,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(transient_retries + 1):
        observation = await _send_turn(
            port=port,
            turn_log_dir=turn_log_dir,
            session_key=f"{session_prefix}:attempt-{attempt}",
            marker=marker,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(observation)
        if not observation.get("error") or not _is_transient(observation):
            break
    result = dict(attempts[-1])
    result["attempt_count"] = len(attempts)
    return result


async def _router_phase(
    *,
    port: int,
    turn_log_dir: Path,
    targets: Sequence[DeploymentTarget],
    router_max_tokens: int,
    timeout_seconds: float,
    transient_retries: int,
) -> dict[str, Any]:
    await _rpc_call(
        port,
        "onboarding.ensemble.configure",
        {"enabled": False, "selectionMode": "router_dynamic", "candidates": []},
    )
    await _rpc_call(
        port,
        "config.patch",
        {"patches": {"llm.max_tokens": router_max_tokens}},
    )
    rows: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        forced_tier = _forced_tier_for_index(index)
        await _rpc_call(
            port,
            "onboarding.router.configure",
            forced_router_payload(target, forced_tier=forced_tier),
        )
        marker = f"MIXED_ROUTER_{forced_tier.upper()}_{index}_{target.provider.upper()}"
        observation = await _send_with_retry(
            port=port,
            turn_log_dir=turn_log_dir,
            session_prefix=f"mixed-router:{index}:{target.provider}",
            marker=marker,
            timeout_seconds=timeout_seconds,
            transient_retries=transient_retries,
        )
        ok, failures = validate_router_observation(
            observation,
            target,
            expected_tier=forced_tier,
        )
        rows.append(
            _router_result_row(
                target=target,
                expected_tier=forced_tier,
                observation=observation,
                ok=ok,
                failures=failures,
            )
        )

    state_pair = _state_isolation_pair(targets)
    state_switch: dict[str, Any] = {"ok": False, "skipped": state_pair is None}
    if state_pair is not None:
        session = f"mixed-router-state:{int(time.time() * 1000)}"
        observations: list[dict[str, Any]] = []
        markers: list[str] = []
        for index, target in enumerate(state_pair):
            forced_tier = _forced_tier_for_index(index)
            await _rpc_call(
                port,
                "onboarding.router.configure",
                forced_router_payload(target, forced_tier=forced_tier),
            )
            marker = f"MIXED_STATE_{index}_{target.provider.upper()}"
            observation = await _send_turn(
                port=port,
                turn_log_dir=turn_log_dir,
                session_key=session,
                marker=marker,
                timeout_seconds=timeout_seconds,
                previous_assistant_count=index,
                intent="new_chat" if index == 0 else "continue",
                expected_prior_marker=markers[-1] if markers else None,
            )
            markers.append(marker)
            observations.append(observation)
        state_switch = {
            "ok": all(
                validate_router_observation(
                    observation,
                    target,
                    expected_tier=_forced_tier_for_index(index),
                )[0]
                for index, (target, observation) in enumerate(
                    zip(state_pair, observations, strict=True)
                )
            )
            and all(
                observation.get("private_provider_state_replayed") is False
                for observation in observations
            ),
            "sequence": [target.public() for target in state_pair],
            "provider_private_state_replayed": any(
                observation.get("private_provider_state_replayed") is True
                for observation in observations
            ),
            "second_turn_used_first_assistant_context": (
                len(observations) == 2
                and observations[1].get("prior_assistant_context_present") is True
            ),
        }

    fail_closed: dict[str, Any] = {"ok": False, "skipped": len(targets) < 2}
    if len(targets) >= 2:
        primary, missing = targets[0], targets[-1]
        await _rpc_call(
            port,
            "onboarding.router.configure",
            forced_router_payload(primary, forced_tier="c0"),
        )
        await _rpc_call(
            port,
            "onboarding.ensemble.configure",
            {"enabled": False, "selectionMode": "router_dynamic", "candidates": []},
        )
        await _rpc_call(
            port,
            "onboarding.llmProfile.remove",
            {"providerId": missing.provider},
        )
        try:
            # Removing an explicit profile is not enough to make a registry
            # provider unavailable: the documented final credential source
            # is its default env key.  Recreate it as a credentialless draft
            # on a reserved cross-origin endpoint, which prevents that env
            # key from following the endpoint and exercises fail-closed
            # routing without making a negative network request.
            await _rpc_call(
                port,
                "onboarding.llmProfile.upsert",
                _missing_credential_profile_payload(missing),
            )
            await _rpc_call(
                port,
                "onboarding.router.configure",
                forced_router_payload(missing, forced_tier="c0"),
            )
            marker = f"MIXED_FAIL_CLOSED_{missing.provider.upper()}"
            observation = await _send_with_retry(
                port=port,
                turn_log_dir=turn_log_dir,
                session_prefix="mixed-router:fail-closed",
                marker=marker,
                timeout_seconds=timeout_seconds,
                transient_retries=0,
            )
            decision = observation.get("decision") or {}
            decision = decision if isinstance(decision, Mapping) else {}
            request_identity = observation.get("request_identity") or {}
            request_identity = request_identity if isinstance(request_identity, Mapping) else {}
            response_identity = observation.get("response_identity") or {}
            response_identity = response_identity if isinstance(response_identity, Mapping) else {}
            (
                all_requests_used_primary,
                foreign_model_sent_to_primary,
                request_identities,
            ) = _fail_closed_request_audit(
                observation,
                primary=primary,
                foreign_model=missing.model,
            )
            fail_closed_ok = bool(
                decision.get("requestedProvider") == missing.provider
                and decision.get("requestedModel") == missing.model
                and decision.get("executedProvider") == primary.provider
                and decision.get("executedModel") == primary.model
                and int(decision.get("fallbackHops") or 0) == 0
                and bool(decision.get("fallbackReason"))
                and observation.get("marker_present") is True
                and _usage_total(observation.get("usage") or {}) > 0
                and request_identity.get("provider") == primary.provider
                and request_identity.get("model") == primary.model
                and response_identity.get("provider") == primary.provider
                and provider_response_model_matches(
                    primary.provider,
                    primary.model,
                    str(response_identity.get("model") or ""),
                )
                and provider_response_model_matches(
                    primary.provider,
                    primary.model,
                    str(observation.get("response_model") or ""),
                )
                and observation.get("tools_empty") is True
                and all_requests_used_primary
                and not foreign_model_sent_to_primary
            )
            fail_closed = {
                "ok": fail_closed_ok,
                "requested": missing.public(),
                "executed": {
                    "provider": decision.get("executedProvider"),
                    "model": decision.get("executedModel"),
                },
                "fallback_reason": decision.get("fallbackReason"),
                "foreign_model_sent_to_primary": foreign_model_sent_to_primary,
                "all_request_identities": request_identities,
                "all_requests_used_primary": all_requests_used_primary,
                "marker_present": observation.get("marker_present"),
                "usage": observation.get("usage"),
                "cost": observation.get("cost"),
                "latency_ms": observation.get("latency_ms"),
                "request_identity": request_identity,
                "response_identity": response_identity,
                "failure_class": (
                    None
                    if fail_closed_ok
                    else str(observation.get("failure_class") or "implementation")
                ),
            }
        finally:
            await _rpc_call(
                port,
                "onboarding.llmProfile.upsert",
                {
                    "providerId": missing.provider,
                    "apiKeyEnv": missing.env_key,
                    "baseUrl": missing.endpoint,
                },
            )

    successful = [row for row in rows if row.get("ok") is True]
    phase_ok = _router_phase_ok(
        rows=rows,
        target_count=len(targets),
        state_switch=state_switch,
        fail_closed=fail_closed,
    )
    return {
        "ok": phase_ok,
        "status": "passed" if phase_ok else "incomplete",
        "successful_provider_count": len(successful),
        "required_provider_count": MIN_ROUTER_PROVIDERS,
        "targets": rows,
        "a_to_b_state_isolation": state_switch,
        "missing_profile_fail_closed": fail_closed,
    }


async def _ensemble_phase(
    *,
    port: int,
    turn_log_dir: Path,
    targets: Sequence[DeploymentTarget],
    ensemble_max_tokens: int,
    timeout_seconds: float,
    transient_retries: int,
) -> dict[str, Any]:
    groups = rotating_triples(targets)
    if not groups:
        return {
            "ok": False,
            "status": "incomplete",
            "reason": "at_least_three_providers_required",
            "lineups": [],
        }
    await _rpc_call(port, "onboarding.router.configure", {"mode": "disabled"})
    await _rpc_call(
        port,
        "config.patch",
        {
            "patches": {
                "llm.provider": targets[0].provider,
                "llm.model": targets[0].model,
                "llm.max_tokens": ensemble_max_tokens,
            }
        },
    )
    rows: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        await _rpc_call(port, "onboarding.ensemble.configure", ensemble_payload(group))
        marker = f"MIXED_ENSEMBLE_{index}"
        observation = await _send_with_retry(
            port=port,
            turn_log_dir=turn_log_dir,
            session_prefix=f"mixed-ensemble:{index}",
            marker=marker,
            timeout_seconds=timeout_seconds,
            transient_retries=transient_retries,
        )
        ok, failures = validate_ensemble_observation(observation, group)
        rows.append(
            {
                "ok": ok,
                "failures": failures,
                "proposers": [group[0].public(), group[1].public()],
                "aggregator": group[2].public(),
                "trace": observation.get("ensemble"),
                "usage": observation.get("usage"),
                "usage_breakdown": observation.get("usage_breakdown"),
                "cost": observation.get("cost"),
                "latency_ms": observation.get("latency_ms"),
                "marker_present": observation.get("marker_present"),
                "tools_empty": observation.get("tools_empty"),
                "failure_class": (
                    None if ok else str(observation.get("failure_class") or "implementation")
                ),
                "attempt_count": observation.get("attempt_count"),
            }
        )

    negative_group = groups[0]
    missing_key_guard = await _missing_profile_runtime_guard(
        port=port,
        target=negative_group[1],
    )
    await _rpc_call(
        port,
        "onboarding.ensemble.configure",
        ensemble_payload(negative_group, bad_proposer=True),
    )
    bad_proposer_observation = await _send_with_retry(
        port=port,
        turn_log_dir=turn_log_dir,
        session_prefix="mixed-ensemble:bad-proposer",
        marker="MIXED_BAD_PROPOSER_QUORUM",
        timeout_seconds=timeout_seconds,
        transient_retries=0,
    )
    bad_trace = bad_proposer_observation.get("ensemble") or {}
    bad_proposer_ok = _bad_proposer_quorum_ok(bad_proposer_observation, negative_group)

    await _rpc_call(
        port,
        "onboarding.ensemble.configure",
        ensemble_payload(negative_group, bad_aggregator=True),
    )
    bad_aggregator_observation = await _send_with_retry(
        port=port,
        turn_log_dir=turn_log_dir,
        session_prefix="mixed-ensemble:bad-aggregator",
        marker="MIXED_BAD_AGGREGATOR",
        timeout_seconds=timeout_seconds,
        transient_retries=0,
    )
    bad_aggregator_ok = (
        bad_aggregator_observation.get("assistant_present") is False
        and bool(bad_aggregator_observation.get("error"))
        and bad_aggregator_observation.get("failure_class")
        in {
            "model-unavailable",
            "implementation",
        }
    )

    return {
        "ok": any(row.get("ok") is True for row in rows)
        and all(row.get("ok") is True for row in rows)
        and missing_key_guard.get("ok") is True
        and bad_proposer_ok
        and bad_aggregator_ok,
        "status": "passed"
        if rows
        and all(row.get("ok") is True for row in rows)
        and missing_key_guard.get("ok") is True
        and bad_proposer_ok
        and bad_aggregator_ok
        else "incomplete",
        "lineups": rows,
        "negative": {
            "missing_key_runtime_guard": missing_key_guard,
            "bad_proposer_quorum": {
                "ok": bad_proposer_ok,
                "failure_class": bad_proposer_observation.get("failure_class"),
                "trace": bad_trace,
            },
            "bad_aggregator_explicit_error": {
                "ok": bad_aggregator_ok,
                "failure_class": bad_aggregator_observation.get("failure_class"),
                "error_present": bool(bad_aggregator_observation.get("error")),
            },
        },
    }


async def _missing_profile_runtime_guard(
    *,
    port: int,
    target: DeploymentTarget,
) -> dict[str, Any]:
    """Prove the shared profile resolver blocks a credentialless draft before probing.

    ``onboarding.llmProfile.probe`` resolves the deployment before it enters
    the network probe adapter.  A missing pool alone may legally fall through
    to the registry env, so the draft also uses a reserved cross-origin
    endpoint.  Registry credentials cannot follow that endpoint and the
    original official-endpoint profile is restored in ``finally``.
    """

    await _rpc_call(
        port,
        "onboarding.llmProfile.upsert",
        _missing_credential_profile_payload(target),
    )
    blocked = False
    reason = ""
    try:
        await _rpc_call(
            port,
            "onboarding.llmProfile.probe",
            {"providerId": target.provider, "model": target.model},
        )
    except GatewayRPCError as exc:
        reason = str(exc.message or "")
        blocked = "credential" in reason.lower() or "exhaust" in reason.lower()
    finally:
        await _rpc_call(
            port,
            "onboarding.llmProfile.upsert",
            {
                "providerId": target.provider,
                "apiKeyEnv": target.env_key,
                "apiKeyEnvPool": [],
                "baseUrl": target.endpoint,
            },
        )
    return {
        "ok": blocked,
        **target.public(),
        "blocked_before_probe_adapter": blocked,
        "failure_class": "missing-credential" if blocked else "implementation",
        "reason_present": bool(reason),
    }


async def _run_isolated_gateway_in_temp(
    *,
    targets: Sequence[DeploymentTarget],
    secrets: Mapping[str, str],
    router_max_tokens: int,
    ensemble_max_tokens: int,
    timeout_seconds: float,
    transient_retries: int,
    base_environment: Mapping[str, str] | None,
    temp_root: Path,
) -> dict[str, Any]:
    port = _free_port()
    config_path = temp_root / "config.toml"
    state_dir = temp_root / "state"
    turn_log_dir = temp_root / "turn-calls"
    user_state_dir = temp_root / "user-state"
    state_dir.mkdir(mode=0o700)
    turn_log_dir.mkdir(mode=0o700)
    user_state_dir.mkdir(mode=0o700)
    config_text = render_gateway_config(targets, router_max_tokens=router_max_tokens)
    config_path.write_text(config_text, encoding="utf-8")
    os.chmod(config_path, 0o600)

    env = build_gateway_environment(
        targets,
        secrets,
        base_environment=os.environ if base_environment is None else base_environment,
    )
    env["PYTHONPATH"] = str(SRC_DIR)
    env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_USER_STATE_DIR"] = str(user_state_dir)
    env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

    stdout_path = temp_root / "gateway.stdout.log"
    stderr_path = temp_root / "gateway.stderr.log"
    result: dict[str, Any]
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_file,
        stderr_path.open("w", encoding="utf-8") as stderr_file,
    ):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opensquilla.cli.main",
                "gateway",
                "run",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=temp_root,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            shell=False,
        )
        try:
            health, health_error = await asyncio.to_thread(_wait_for_gateway_health, proc, port)
            if health_error:
                result = {
                    "ok": False,
                    "status": "incomplete",
                    "failure_class": _failure_class(health_error) or "implementation",
                    "error_present": True,
                }
            else:
                router = await _router_phase(
                    port=port,
                    turn_log_dir=turn_log_dir,
                    targets=targets,
                    router_max_tokens=router_max_tokens,
                    timeout_seconds=timeout_seconds,
                    transient_retries=transient_retries,
                )
                ensemble = await _ensemble_phase(
                    port=port,
                    turn_log_dir=turn_log_dir,
                    targets=targets,
                    ensemble_max_tokens=ensemble_max_tokens,
                    timeout_seconds=timeout_seconds,
                    transient_retries=transient_retries,
                )
                result = {
                    "ok": router.get("ok") is True and ensemble.get("ok") is True,
                    "status": "passed"
                    if router.get("ok") is True and ensemble.get("ok") is True
                    else "incomplete",
                    "gateway_health_ready": bool(health),
                    "router": router,
                    "ensemble": ensemble,
                }
        except Exception as exc:  # noqa: BLE001 - preserve a safe partial verdict
            result = {
                "ok": False,
                "status": "incomplete",
                "failure_class": _failure_class(f"{type(exc).__name__}: {exc}"),
                "error_present": True,
            }
        finally:
            _stop_gateway(proc)
    return result


async def _run_isolated_gateway(
    *,
    targets: Sequence[DeploymentTarget],
    secrets: Mapping[str, str],
    router_max_tokens: int,
    ensemble_max_tokens: int,
    timeout_seconds: float,
    transient_retries: int,
    base_environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Run the Gateway and remove every raw artifact even if setup fails."""

    temp_root = Path(tempfile.mkdtemp(prefix="opensquilla-live-mixed-provider-"))
    os.chmod(temp_root, 0o700)
    try:
        return await _run_isolated_gateway_in_temp(
            targets=targets,
            secrets=secrets,
            router_max_tokens=router_max_tokens,
            ensemble_max_tokens=ensemble_max_tokens,
            timeout_seconds=timeout_seconds,
            transient_retries=transient_retries,
            base_environment=base_environment,
            temp_root=temp_root,
        )
    finally:
        # Includes turn-call, decision, stdout, stderr, state, and config logs.
        scan_and_remove_temporary_tree(temp_root, secrets)


def run_live_matrix(
    *,
    targets: Sequence[DeploymentTarget],
    secrets: Mapping[str, str],
    router_max_tokens: int = DEFAULT_ROUTER_MAX_TOKENS,
    ensemble_max_tokens: int = DEFAULT_ENSEMBLE_MAX_TOKENS,
    timeout_seconds: float = 120.0,
    transient_retries: int = 1,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not ROUTER_MIN_TOKENS <= router_max_tokens <= ROUTER_MAX_TOKENS_LIMIT:
        raise ValueError(
            f"router max tokens must be within {ROUTER_MIN_TOKENS}..{ROUTER_MAX_TOKENS_LIMIT}"
        )
    if not ENSEMBLE_MIN_TOKENS <= ensemble_max_tokens <= ENSEMBLE_MAX_TOKENS_LIMIT:
        raise ValueError(
            f"ensemble max tokens must be within {ENSEMBLE_MIN_TOKENS}..{ENSEMBLE_MAX_TOKENS_LIMIT}"
        )
    if transient_retries not in (0, 1):
        raise ValueError("transient retries must be 0 or 1")
    ready, blocked = credential_preflight(targets, secrets)
    if blocked:
        payload: dict[str, Any] = {
            "ok": False,
            "status": "incomplete",
            "reason": "one_or_more_selected_providers_missing_credentials",
            "preflight": {"ready": [row.public() for row in ready], "blocked": blocked},
        }
    else:
        result = asyncio.run(
            _run_isolated_gateway(
                targets=ready,
                secrets=secrets,
                router_max_tokens=router_max_tokens,
                ensemble_max_tokens=ensemble_max_tokens,
                timeout_seconds=timeout_seconds,
                transient_retries=transient_retries,
                base_environment=base_environment,
            )
        )
        payload = {
            "ok": result.get("ok") is True,
            "status": result.get("status", "incomplete"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "limits": {
                "router_max_tokens": router_max_tokens,
                "ensemble_max_tokens": ensemble_max_tokens,
                "transient_retries": transient_retries,
            },
            "preflight": {
                "ready": [row.public() for row in ready],
                "blocked": [],
                "missing_key_negative_verified_before_network": True,
            },
            **result,
        }
    safe_payload = sanitize_report(payload, secrets)
    if not isinstance(safe_payload, dict):  # defensive: reports are always objects
        raise RuntimeError("sanitized live report was not an object")
    if report_contains_secret(safe_payload, secrets):
        raise RuntimeError("refusing to return a report containing provider credentials")
    return safe_payload


def _write_safe_report(path: Path, payload: Any, secrets: Mapping[str, str]) -> Any:
    return write_safe_report(path, payload, secrets)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--target", action="append", required=True, metavar="PROVIDER=MODEL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--router-max-tokens", type=int, default=DEFAULT_ROUTER_MAX_TOKENS)
    parser.add_argument("--ensemble-max-tokens", type=int, default=DEFAULT_ENSEMBLE_MAX_TOKENS)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--transient-retries", type=int, choices=(0, 1), default=1)
    args = parser.parse_args(argv)
    if not is_temporary_report_path(args.output):
        parser.error("--output must be inside the system temporary directory")
    if not ROUTER_MIN_TOKENS <= args.router_max_tokens <= ROUTER_MAX_TOKENS_LIMIT:
        parser.error(
            f"--router-max-tokens must be within {ROUTER_MIN_TOKENS}..{ROUTER_MAX_TOKENS_LIMIT}"
        )
    if not ENSEMBLE_MIN_TOKENS <= args.ensemble_max_tokens <= ENSEMBLE_MAX_TOKENS_LIMIT:
        parser.error(
            "--ensemble-max-tokens must be within "
            f"{ENSEMBLE_MIN_TOKENS}..{ENSEMBLE_MAX_TOKENS_LIMIT}"
        )
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.output.resolve() == args.secrets_file.resolve():
        parser.error("--output must not overwrite --secrets-file")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        targets = normalize_targets(args.target)
        inventory = parse_provider_keys_file(args.secrets_file)
        secrets = inventory.secrets
        payload = run_live_matrix(
            targets=targets,
            secrets=secrets,
            router_max_tokens=args.router_max_tokens,
            ensemble_max_tokens=args.ensemble_max_tokens,
            timeout_seconds=args.timeout_seconds,
            transient_retries=args.transient_retries,
        )
        _emit_main_diagnostics(
            payload,
            file_mode=inventory.file_mode,
            permission_warning=inventory.permission_warning,
            ignored_line_numbers=inventory.ignored_line_numbers,
            secrets=secrets,
        )
        public_payload = _public_report_rows(payload)
        safe_payload = _write_safe_report(args.output, public_payload, secrets)
        _assert_public_report_schema(safe_payload)
    except (OSError, RuntimeError, ValueError) as exc:
        args.output.unlink(missing_ok=True)
        known_secrets = inventory.secrets if "inventory" in locals() else {}
        print(
            redact_text(f"live mixed-provider matrix failed: {exc}", known_secrets),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(safe_payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
