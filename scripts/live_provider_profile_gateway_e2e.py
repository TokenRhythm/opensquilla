#!/usr/bin/env python3
"""Run live gateway E2E checks for direct provider tier profiles.

The check starts a temporary OpenSquilla gateway per provider, enables the
matching legacy ``squilla_router.tier_profile`` or curated inline tier map,
sends one turn for each text tier, and records routed model, response usage,
and local cost estimates. Secrets are kept in environment variables and are
not written to the output artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
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
from opensquilla.provider.preset_registry import (  # noqa: E402
    LEGACY_PROVIDER_PRESET_IDS,
    get_preset,
)
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    child_environment,
    classify_failure,
    is_temporary_report_path,
    parse_secrets_file,
    provider_secret_names,
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
    _usage_from_llm_responses,
    _wait_for_assistant_reply,
    _wait_for_gateway_health,
)

DEFAULT_PROVIDERS = [
    "openrouter",
    "dashscope",
    "deepseek",
    "gemini",
    "volcengine",
    "byteplus",
    "openai",
    "zhipu",
    "moonshot",
    "tokenrhythm",
]
BASE_ENV = {
    "openrouter": "OPENROUTER_BASE_URL",
    "openai": "OPENAI_BASE_URL",
    "dashscope": "DASHSCOPE_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "gemini": "GEMINI_BASE_URL",
    "volcengine": "VOLCENGINE_BASE_URL",
    "byteplus": "BYTEPLUS_BASE_URL",
    "moonshot": "MOONSHOT_BASE_URL",
    "zhipu": "ZAI_BASE_URL",
    "tokenrhythm": "TOKENRHYTHM_BASE_URL",
}
TEXT_PROFILE_SLOTS = ("c0", "c1", "c2", "c3")
LIVE_AGENT_MAX_ITERATIONS = 6
LIVE_AGENT_RUNTIME_TIMEOUT_SECONDS = 75.0
LIVE_TURN_HARD_DEADLINE_SECONDS = 90.0
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

TIER_CASES = [
    {
        "tier": "c0",
        "id": "r0_short_ack",
        "message": "谢谢。不要调用工具，请只回复一个短句，包含 {marker}。",
    },
    {
        "tier": "c1",
        "id": "r1_structured_compare",
        "message": (
            "不要调用工具，只输出 Markdown 表格和 marker。用不超过 4 行的表格比较 "
            "PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，每格不超过 12 个字。"
            "最后一行单独写 {marker}。"
        ),
    },
    {
        "tier": "c2",
        "id": "r2_debugging",
        "message": (
            "下面是异步服务偶发超时的日志片段：连接池耗尽、慢查询、重试风暴、队列积压。"
            "不要调用工具，请用不超过三条短句定位可能原因并给出排查动作。"
            "最后一行单独写 {marker}。"
        ),
    },
    {
        "tier": "c3",
        "id": "r3_architecture",
        "message": (
            "请设计跨机房分布式任务调度系统，解释一致性、故障恢复和容量评估。"
            "不要调用工具，回答不超过五句，并包含 {marker}。"
        ),
    },
]


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _marker_component(value: str) -> str:
    raw = "".join(ch if ch.isalnum() else "_" for ch in value.upper())
    return "_".join(part for part in raw.split("_") if part)


def _case_marker(provider: str, slot: str, case_id: str) -> str:
    return (
        f"E2E_{_marker_component(provider)}_"
        f"{_marker_component(slot)}_{_marker_component(case_id)}"
    )


def _load_env_quietly(path: Path = REPO_ROOT / ".env") -> None:
    if not path.exists():
        return
    for key, value in parse_secrets_file(path).items():
        os.environ.setdefault(key, value)


def _profile_tiers(provider: str) -> dict[str, dict[str, Any]]:
    if provider not in LEGACY_PROVIDER_PRESET_IDS:
        preset = get_preset(provider)
        if preset is None:
            raise ValueError(f"no provider preset for {provider!r}")
        return {
            name: dict(tier)
            for name, tier in preset.tier_defaults().items()
            if isinstance(tier, dict) and not tier.get("image_only")
        }
    cfg = GatewayConfig.model_validate(
        {
            "llm": {"provider": provider},
            "squilla_router": {"tier_profile": provider},
        }
    )
    return {
        name: dict(tier)
        for name, tier in cfg.squilla_router.tiers.items()
        if isinstance(tier, dict) and not tier.get("image_only")
    }


def _profile_slot_targets(tiers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        slot: dict(tiers[slot])
        for slot in TEXT_PROFILE_SLOTS
        if isinstance(tiers.get(slot), dict) and not tiers[slot].get("image_only")
    }


def _covered_profile_slots(rows: list[dict[str, Any]]) -> list[str]:
    covered: list[str] = []
    for row in rows:
        slot = str(row.get("actual_slot_covered") or "")
        if row.get("ok") is True and slot and slot not in covered:
            covered.append(slot)
    return covered


def _missing_profile_slots(
    tiers: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[str]:
    covered = set(_covered_profile_slots(rows))
    return [slot for slot in _profile_slot_targets(tiers) if slot not in covered]


def _forced_tier_overrides_for_slot(
    tiers: dict[str, dict[str, Any]],
    slot: str,
) -> dict[str, dict[str, Any]]:
    target = dict(tiers[slot])
    overrides: dict[str, dict[str, Any]] = {}
    for text_slot in TEXT_PROFILE_SLOTS:
        if text_slot == slot:
            forced = dict(target)
            forced["image_only"] = False
            overrides[text_slot] = forced
        else:
            hidden = dict(tiers.get(text_slot, target))
            hidden["image_only"] = True
            overrides[text_slot] = hidden
    return overrides


def _render_tier_overrides(tiers: dict[str, dict[str, Any]] | None) -> str:
    if not tiers:
        return ""
    lines: list[str] = []
    for slot in TEXT_PROFILE_SLOTS:
        cfg = tiers.get(slot)
        if not isinstance(cfg, dict):
            continue
        lines.append("")
        lines.append(f"[squilla_router.tiers.{slot}]")
        for key in (
            "provider",
            "model",
            "description",
            "supports_image",
            "image_only",
            "thinking_level",
            "thinking",
            "supports_thinking",
        ):
            if key in cfg and cfg[key] is not None:
                lines.append(f"{key} = {_toml_value(cfg[key])}")
    return "\n".join(lines)


def _write_config(
    path: Path,
    provider: str,
    base_url: str,
    model: str,
    *,
    max_tokens: int,
    default_tier: str = "c1",
    tier_overrides: dict[str, dict[str, Any]] | None = None,
    llm_thinking: str | None = None,
) -> None:
    tier_override_toml = _render_tier_overrides(tier_overrides)
    llm_thinking_toml = (
        f"\nthinking = {_toml_value(llm_thinking)}" if llm_thinking is not None else ""
    )
    # Persisted tier_profile ids are deliberately pinned to the legacy nine
    # for downgrade compatibility.  Matrix-only synthesized providers (for
    # example MiniMax) still work through the complete inline tier overrides.
    tier_profile_toml = (
        f'tier_profile = "{provider}"' if provider in LEGACY_PROVIDER_PRESET_IDS else ""
    )
    path.write_text(
        f"""
host = "127.0.0.1"
debug = false
llm_request_timeout_seconds = 90
agent_runtime_timeout_seconds = {LIVE_AGENT_RUNTIME_TIMEOUT_SECONDS}
agent_max_iterations = {LIVE_AGENT_MAX_ITERATIONS}
agent_max_provider_retries = 0

[auth]
mode = "none"

[control_ui]
enabled = false

[rate_limit]
enabled = false

[privacy]
disable_network_observability = true

[tools]
profile = "minimal"
deny = ["*"]

[task_runtime]
turn_hard_deadline_s = {LIVE_TURN_HARD_DEADLINE_SECONDS}

[memory]
source = "state"

[llm]
provider = "{provider}"
model = "{model}"
api_key_env = "{get_provider_spec(provider).env_key}"
base_url = "{base_url}"
max_tokens = {max_tokens}
{llm_thinking_toml}

[squilla_router]
enabled = true
auto_thinking = true
rollout_phase = "full"
strategy = "v4_phase3"
{tier_profile_toml}
default_tier = "{default_tier}"
confidence_threshold = 0.5
kv_cache_anti_downgrade_enabled = true
kv_cache_anti_downgrade_window_seconds = 600
complaint_upgrade_enabled = true
complaint_upgrade_steps = 1
require_router_runtime = true
{tier_override_toml}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _first_record(records: list[dict[str, Any]], *, session_key: str, kind: str) -> dict[str, Any]:
    for record in records:
        if record.get("session_key") == session_key and record.get("kind") == kind:
            return record
    return {}


def _read_decision_records(state_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((state_root / "logs").glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _decision_for_session(
    records: list[dict[str, Any]],
    *,
    session_key: str,
) -> dict[str, Any]:
    for record in records:
        if record.get("session_key") == session_key:
            return record
    return {}


def _router_step_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    for step in decision.get("pipeline_steps") or []:
        if step.get("step_name") == "apply_squilla_router":
            return step
    return {}


def _estimate_cost(
    model: str,
    usage: dict[str, Any],
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cache_read_tokens = int(
        usage.get("cache_read_tokens") or usage.get("cached_tokens") or 0
    )
    cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
    resolved = resolve_model_price(model, provider or "")
    estimate_result = estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        price=resolved.entry,
    )
    estimate = estimate_result.cost_usd
    raw_billed_cost = usage.get("billed_cost")
    provider_billed_cost = None
    cost_source = "opensquilla_static_estimate"
    billing_scope = "static_estimate"
    if (
        isinstance(raw_billed_cost, int | float)
        and not isinstance(raw_billed_cost, bool)
        and raw_billed_cost >= 0
        and str(usage.get("cost_source") or "") == "provider_billed"
    ):
        provider_billed_cost = float(raw_billed_cost)
        cost_source = "provider_billed"
        billing_scope = "provider_response"
    return {
        "provider_billed_cost_usd": provider_billed_cost,
        "opensquilla_estimated_cost_usd": estimate,
        "cost_source": cost_source,
        "billing_scope": billing_scope,
        "raw_gateway_usage_billed_cost_usd": usage.get("billed_cost"),
        "provider_billed": provider_billed_cost,
        "opensquilla_estimate": estimate,
        "input_per_m": resolved.entry.input_per_m,
        "output_per_m": resolved.entry.output_per_m,
        "cache_read_per_m": resolved.entry.cache_read_per_m,
        "cache_write_per_m": resolved.entry.cache_write_per_m,
        "price_source": resolved.source,
        "estimate_basis": estimate_result.basis,
        "source": cost_source,
    }


def _accounting_usage_fields(usage: dict[str, Any]) -> dict[str, Any]:
    """Project only the token/cost fields needed by the public live report."""

    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "cache_write_tokens": usage.get("cache_write_tokens"),
        "billed_cost": usage.get("billed_cost"),
        # Required to distinguish a real zero-cost receipt from the legacy
        # zero placeholder carried by responses without provider billing.
        "cost_source": usage.get("cost_source"),
    }


def _failure_kind(
    row: dict[str, Any],
    actual_model: str,
    actual_routed_tier: str | None,
) -> str | None:
    error = str(row.get("turn_error") or "")
    if error:
        return classify_failure(error)
    if not row.get("assistant_excerpt"):
        return "implementation"
    if not row.get("assistant_marker_present"):
        return "implementation"
    if actual_routed_tier != row.get("expected_slot"):
        return "implementation"
    if actual_model != row.get("expected_model"):
        return "model-unavailable"
    return None


def _actual_model_from_records(
    request: dict[str, Any],
    response: dict[str, Any],
) -> str:
    request_payload = request.get("payload") or {}
    response_payload = response.get("payload") or {}
    request_config = request_payload.get("config") or {}
    usage = response_payload.get("usage") or {}
    return str(
        request_payload.get("model")
        or request_config.get("model")
        or request.get("model")
        or usage.get("model")
        or response.get("model")
        or ""
    )


def _run_gateway_case_batch_in_temp(
    *,
    provider: str,
    api_key: str,
    base_url: str,
    tiers: dict[str, dict[str, Any]],
    cases: list[dict[str, Any]],
    max_tokens: int,
    timeout_seconds: float,
    case_mode: str,
    default_tier: str = "c1",
    tier_overrides: dict[str, dict[str, Any]] | None = None,
    llm_thinking: str | None = None,
    tmp_path: Path,
) -> dict[str, Any]:
    active_tiers = tier_overrides or tiers
    default_model = str(
        active_tiers.get(default_tier, {}).get("model")
        or tiers.get(default_tier, {}).get("model")
        or next(iter(_profile_slot_targets(tiers).values())).get("model")
        or ""
    )
    port = _free_port()
    config_path = tmp_path / "gateway.toml"
    state_dir = tmp_path / "state"
    turn_log_dir = tmp_path / "turn-calls"
    user_state_dir = tmp_path / "user-state"
    state_dir.mkdir(mode=0o700)
    turn_log_dir.mkdir(mode=0o700)
    user_state_dir.mkdir(mode=0o700)
    _write_config(
        config_path,
        provider,
        base_url,
        default_model,
        max_tokens=max_tokens,
        default_tier=default_tier,
        tier_overrides=tier_overrides,
        llm_thinking=llm_thinking,
    )

    provider_spec = get_provider_spec(provider)
    env = child_environment(
        provider,
        {provider_spec.env_key: api_key},
        base_environment=os.environ,
    )
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_USER_STATE_DIR"] = str(user_state_dir)
    env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

    stdout_path = tmp_path / "gateway.stdout.log"
    stderr_path = tmp_path / "gateway.stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_file:
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
            cwd=tmp_path,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )

        health: dict[str, Any] | None = None
        error: str | None = None
        rows: list[dict[str, Any]] = []
        try:
            health, error = _wait_for_gateway_health(proc, port)
            if error is None:
                for case in cases:
                    slot = str(case.get("slot") or case.get("tier") or default_tier)
                    marker = _case_marker(provider, slot, str(case["id"]))
                    session_key = (
                        f"profile-e2e:{provider}:{case['id']}:{int(time.time() * 1000)}"
                    )
                    message = case["message"].format(marker=marker)
                    try:
                        accepted = _post_json(
                            f"http://127.0.0.1:{port}/api/chat",
                            {
                                "sessionKey": session_key,
                                "message": message,
                                "intent": "new_chat",
                            },
                            timeout=10.0,
                        )
                        assistant, history, turn_error = _wait_for_assistant_reply(
                            port=port,
                            session_key=session_key,
                            previous_assistant_count=0,
                            timeout_seconds=timeout_seconds,
                        )
                    except Exception as exc:  # noqa: BLE001 - compact E2E diagnostic
                        accepted = {}
                        assistant = None
                        history = None
                        turn_error = f"{type(exc).__name__}: {exc}"
                    assistant_text = str((assistant or {}).get("text", "")).strip()
                    rows.append(
                        {
                            "case_id": case["id"],
                            "case_mode": case_mode,
                            "expected_slot": slot,
                            "expected_tier": slot,
                            "expected_model": str(tiers.get(slot, {}).get("model") or ""),
                            "marker": marker,
                            "session_key": session_key,
                            "accepted": accepted,
                            "assistant_excerpt": assistant_text[:240],
                            "assistant_marker_present": marker in assistant_text,
                            "history_message_count": len((history or {}).get("messages", [])),
                            "turn_error": turn_error,
                        }
                    )
        finally:
            _stop_gateway(proc)
            stdout_file.flush()
            stderr_file.flush()
            records = _read_turn_call_records(turn_log_dir)
            decisions = _read_decision_records(tmp_path / "state")
    enriched: list[dict[str, Any]] = []
    for row in rows:
        request = _first_record(records, session_key=row["session_key"], kind="llm_request")
        response = _first_record(records, session_key=row["session_key"], kind="llm_response")
        decision = _decision_for_session(decisions, session_key=row["session_key"])
        router_step = _router_step_from_decision(decision)
        request_payload = request.get("payload") or {}
        response_payload = response.get("payload") or {}
        request_config = request_payload.get("config") or {}
        usage = response_payload.get("usage") or {}
        request_tools = request_payload.get("tools") or []
        actual_model = _actual_model_from_records(request, response)
        actual_routed_tier = (
            router_step.get("routed_tier")
            or request_payload.get("routed_tier")
            or request_payload.get("squilla_router_tier")
            or request_config.get("routed_tier")
        )
        if actual_routed_tier is not None:
            actual_routed_tier = str(actual_routed_tier)
        failure_kind = _failure_kind(row, actual_model, actual_routed_tier)
        row_ok = (
            failure_kind is None
            and bool(row.get("assistant_excerpt"))
            and actual_model == row["expected_model"]
            and actual_routed_tier == row["expected_slot"]
            and request.get("provider") == provider
            and response.get("provider") == provider
            and not request_tools
        )
        enriched.append(
            {
                **row,
                "ok": row_ok,
                "failure_kind": failure_kind,
                "error": row.get("turn_error"),
                "actual_routed_tier": actual_routed_tier,
                "routing_source": router_step.get("routing_source"),
                "routing_confidence": router_step.get("confidence"),
                "actual_slot_covered": row["expected_slot"] if row_ok else None,
                "actual_request_model": actual_model or request.get("model"),
                "actual_response_model": usage.get("model"),
                "actual_request_provider": request.get("provider"),
                "actual_response_provider": response.get("provider"),
                "request_tool_count": len(request_tools),
                "latency_ms": int(response_payload.get("duration_ms") or 0),
                "request_thinking": request_config.get("thinking"),
                "request_thinking_level": request_config.get("thinking_level"),
                "usage": _accounting_usage_fields(usage),
                "cost": _estimate_cost(
                    actual_model or row["expected_model"],
                    usage,
                    provider=provider,
                ),
            }
        )

    llm_responses = [record for record in records if record.get("kind") == "llm_response"]
    batch_ok = error is None and bool(enriched) and all(row["ok"] for row in enriched)
    report = {
        "case_mode": case_mode,
        "ok": batch_ok,
        "health": health or {},
        "cases": enriched,
        "usage_from_turn_logs": _usage_from_llm_responses(llm_responses),
        "error": error,
    }
    return sanitize_report(report, (api_key,))


def _run_gateway_case_batch(
    *,
    provider: str,
    api_key: str,
    base_url: str,
    tiers: dict[str, dict[str, Any]],
    cases: list[dict[str, Any]],
    max_tokens: int,
    timeout_seconds: float,
    case_mode: str,
    default_tier: str = "c1",
    tier_overrides: dict[str, dict[str, Any]] | None = None,
    llm_thinking: str | None = None,
) -> dict[str, Any]:
    """Run one isolated batch and always remove raw Gateway artifacts."""

    tmp_path = Path(tempfile.mkdtemp(prefix=f"opensquilla-{provider}-profile-e2e-"))
    os.chmod(tmp_path, 0o700)
    try:
        return _run_gateway_case_batch_in_temp(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            tiers=tiers,
            cases=cases,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            case_mode=case_mode,
            default_tier=default_tier,
            tier_overrides=tier_overrides,
            llm_thinking=llm_thinking,
            tmp_path=tmp_path,
        )
    finally:
        scan_and_remove_temporary_tree(tmp_path, (api_key,))


def _run_provider(provider: str, *, max_tokens: int, timeout_seconds: float) -> dict[str, Any]:
    spec = get_provider_spec(provider)
    api_key = os.environ.get(spec.env_key, "").strip()
    requested_base_url = os.environ.get(BASE_ENV.get(provider, ""), "").strip()
    base_url = registry_endpoint(provider, requested_base_url or None)
    tiers = _profile_tiers(provider)
    max_tokens = max(max_tokens, 1024) if provider == "tokenrhythm" else max_tokens
    slot_targets = _profile_slot_targets(tiers)
    if not api_key:
        return {
            "provider": provider,
            "ok": False,
            "provider_ok": False,
            "skipped": True,
            "failure_kind": "skipped_missing_key",
            "env_key": spec.env_key,
            "base_url": base_url,
            "key_present": False,
            "tier_profile": (
                provider if provider in LEGACY_PROVIDER_PRESET_IDS else None
            ),
            "tier_mode": (
                "legacy_profile"
                if provider in LEGACY_PROVIDER_PRESET_IDS
                else "inline_preset"
            ),
            "tier_models": {slot: cfg.get("model") for slot, cfg in slot_targets.items()},
            "profile_slots_covered": [],
            "profile_slots_missing": list(slot_targets),
            "models_covered": [],
            "error": f"{spec.env_key} is empty",
        }

    natural = _run_gateway_case_batch(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        tiers=tiers,
        cases=TIER_CASES,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        case_mode="natural_router",
        tier_overrides=(
            tiers if provider not in LEGACY_PROVIDER_PRESET_IDS else None
        ),
    )
    all_cases = list(natural.get("cases") or [])
    coverage_batches: list[dict[str, Any]] = []
    for missing_slot in _missing_profile_slots(tiers, all_cases):
        target_case = {
            "slot": missing_slot,
            "id": f"coverage_{missing_slot}",
            "message": (
                "不要调用工具，请只回复一句中文短句并包含 {marker}。"
            ),
        }
        batch = _run_gateway_case_batch(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            tiers=tiers,
            cases=[target_case],
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            case_mode="coverage_compensation",
            default_tier=missing_slot,
            tier_overrides=_forced_tier_overrides_for_slot(tiers, missing_slot),
        )
        coverage_batches.append(batch)
        all_cases.extend(batch.get("cases") or [])

    covered_slots = _covered_profile_slots(all_cases)
    missing_slots = _missing_profile_slots(tiers, all_cases)
    models_covered = sorted(
        {
            str(row.get("actual_request_model") or row.get("expected_model") or "")
            for row in all_cases
            if row.get("ok") is True
        }
        - {""}
    )
    natural_cases = [row for row in all_cases if row.get("case_mode") == "natural_router"]
    coverage_cases = [
        row for row in all_cases if row.get("case_mode") == "coverage_compensation"
    ]
    provider_ok = not missing_slots and any(
        row.get("case_mode") == "natural_router" and row.get("assistant_excerpt")
        for row in all_cases
    )
    failure_kinds = sorted(
        {str(row.get("failure_kind")) for row in all_cases if row.get("failure_kind")}
    )
    return {
        "provider": provider,
        "ok": provider_ok,
        "provider_ok": provider_ok,
        "env_key": spec.env_key,
        "base_url": base_url,
        "key_present": bool(api_key),
        "tier_profile": provider if provider in LEGACY_PROVIDER_PRESET_IDS else None,
        "tier_mode": (
            "legacy_profile"
            if provider in LEGACY_PROVIDER_PRESET_IDS
            else "inline_preset"
        ),
        "tier_models": {slot: cfg.get("model") for slot, cfg in slot_targets.items()},
        "profile_slots_covered": covered_slots,
        "profile_slots_missing": missing_slots,
        "models_covered": models_covered,
        "natural_cases_ok": bool(natural_cases)
        and all(
            row.get("failure_kind") in (None, "router_selected_unexpected_tier")
            for row in natural_cases
        ),
        "coverage_cases_ok": bool(coverage_cases) and all(row.get("ok") for row in coverage_cases)
        if coverage_cases
        else True,
        "health": natural.get("health") or {},
        "cases": all_cases,
        "batches": [natural, *coverage_batches],
        "usage_from_turn_logs": natural.get("usage_from_turn_logs"),
        "failure_kinds": failure_kinds,
        "error": "; ".join(failure_kinds) or natural.get("error"),
    }


def _public_provider_result(result: dict[str, Any]) -> dict[str, Any]:
    """Drop raw prompts, replies, session ids, endpoints, and diagnostics."""

    cases = [
        {
            "provider": str(result.get("provider") or ""),
            "model": str(
                row.get("actual_response_model")
                or row.get("actual_request_model")
                or row.get("expected_model")
                or ""
            ),
            "status": "passed" if row.get("ok") is True else "failed",
            "failure_class": row.get("failure_kind"),
            "usage": row.get("usage") or {},
            "cost": row.get("cost") or {},
            "latency_ms": int(row.get("latency_ms") or 0),
        }
        for row in result.get("cases") or []
        if isinstance(row, dict)
    ]
    return {
        "provider": str(result.get("provider") or ""),
        "status": (
            "skipped"
            if result.get("skipped") is True
            else ("passed" if result.get("ok") is True else "failed")
        ),
        "failure_class": (
            None
            if result.get("ok") is True
            else (
                "missing-credential"
                if result.get("skipped") is True
                else str(next(iter(result.get("failure_kinds") or []), "implementation"))
            )
        ),
        "models": list(result.get("models_covered") or []),
        "usage": result.get("usage_from_turn_logs") or {},
        "cost": {},
        "latency_ms": sum(int(row.get("latency_ms") or 0) for row in cases),
        "cases": cases,
    }


def _project_public_result(row: dict[str, Any]) -> dict[str, Any]:
    """Project one in-memory case onto the persisted report contract."""

    status = str(row.get("status") or "failed")
    failure_class = row.get("failure_class")
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
        "usage": dict(usage) if isinstance(usage, dict) else {},
        "cost": dict(cost) if isinstance(cost, dict) else {},
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


def _public_report_rows(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten cases and keep session, marker, prompt, and batch evidence in memory."""

    rows: list[dict[str, Any]] = []
    for result in raw_results:
        provider = str(result.get("provider") or "")
        cases = [row for row in result.get("cases") or [] if isinstance(row, dict)]
        if cases:
            for case in cases:
                ok = case.get("ok") is True
                rows.append(
                    _project_public_result(
                        {
                            "provider": provider,
                            "model": str(
                                case.get("actual_response_model")
                                or case.get("actual_request_model")
                                or case.get("expected_model")
                                or ""
                            ),
                            "status": "passed" if ok else "failed",
                            "failure_class": (
                                None
                                if ok
                                else str(case.get("failure_kind") or "implementation")
                            ),
                            "usage": case.get("usage") or {},
                            "cost": case.get("cost") or {},
                            "latency_ms": int(case.get("latency_ms") or 0),
                        }
                    )
                )
            continue

        tier_models = result.get("tier_models")
        tier_models = tier_models if isinstance(tier_models, dict) else {}
        models = list(
            dict.fromkeys(
                str(model)
                for model in [*(result.get("models_covered") or []), *tier_models.values()]
                if model
            )
        ) or [""]
        skipped = result.get("skipped") is True
        passed = result.get("ok") is True
        status = "skipped" if skipped else ("passed" if passed else "failed")
        failure_class = (
            None
            if passed
            else (
                "missing-credential"
                if skipped
                else str(next(iter(result.get("failure_kinds") or []), "implementation"))
            )
        )
        for model in models:
            rows.append(
                _project_public_result(
                    {
                        "provider": provider,
                        "model": model,
                        "status": status,
                        "failure_class": failure_class,
                        "usage": result.get("usage_from_turn_logs") or {},
                        "cost": {},
                        "latency_ms": 0,
                    }
                )
            )

    _assert_public_report_schema(rows)
    return rows


def _emit_main_diagnostics(
    summaries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    secrets: dict[str, str],
) -> None:
    diagnostic = {
        "providers": len(summaries),
        "provider_status": {
            "passed": sum(row.get("status") == "passed" for row in summaries),
            "failed": sum(row.get("status") == "failed" for row in summaries),
            "skipped": sum(row.get("status") == "skipped" for row in summaries),
        },
        "case_rows": len(rows),
    }
    message = "live provider gateway coverage: " + json.dumps(diagnostic, sort_keys=True)
    print(redact_text(message, secrets), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", nargs="+", default=DEFAULT_PROVIDERS)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    if not is_temporary_report_path(output):
        parser.error("--output must be inside the system temporary directory")
    if not 32 <= args.max_tokens <= 64:
        parser.error("--max-tokens must be between 32 and 64")

    if not args.no_env_file and os.environ.get("OPENSQUILLA_LIVE_DISABLE_DOTENV") != "1":
        _load_env_quietly()
    secrets = {
        name: os.environ.get(name, "")
        for name in provider_secret_names()
        if os.environ.get(name)
    }
    try:
        raw_results = [
            _run_provider(
                provider,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            )
            for provider in args.providers
        ]
    except (OSError, RuntimeError, ValueError) as exc:
        output.unlink(missing_ok=True)
        print(
            redact_text(f"provider profile gateway matrix failed: {exc}", secrets),
            file=sys.stderr,
        )
        return 2
    summaries = [_public_provider_result(result) for result in raw_results]
    all_ok = all(result.get("status") == "passed" for result in summaries)
    payload = _public_report_rows(raw_results)
    _emit_main_diagnostics(summaries, payload, secrets)
    try:
        payload = sanitize_report(payload, secrets)
        if report_contains_secret(payload, secrets):
            raise RuntimeError("refusing to write a report containing provider credentials")
        payload = write_safe_report(output, payload, secrets)
        _assert_public_report_schema(payload)
    except (OSError, RuntimeError, ValueError) as exc:
        output.unlink(missing_ok=True)
        print(redact_text(f"unable to write live report: {exc}", secrets), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
