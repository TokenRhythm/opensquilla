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
import asyncio
import base64
import binascii
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.context_budget import CHARS_PER_TOKEN, ContextBudgetGovernor  # noqa: E402
from opensquilla.engine.capacity_admission import (  # noqa: E402
    MAX_THINKING_BUDGET_TOKENS,
    model_has_request_capacity,
)
from opensquilla.engine.pricing import estimate_cost, resolve_model_price  # noqa: E402
from opensquilla.gateway.config import GatewayConfig  # noqa: E402
from opensquilla.provider.model_catalog import ModelCatalog  # noqa: E402
from opensquilla.provider.preset_registry import (  # noqa: E402
    LEGACY_PROVIDER_PRESET_IDS,
    get_preset,
)
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from opensquilla.provider.request_proof import estimate_provider_media_tokens  # noqa: E402
from opensquilla.session.compaction import estimate_entry_model_replay_tokens  # noqa: E402
from opensquilla.session.manager import SessionManager  # noqa: E402
from opensquilla.session.storage import SessionStorage  # noqa: E402
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
ATTACHMENT_CAPACITY_OPT_IN_ENV = "OPENSQUILLA_LIVE_TOKENRHYTHM_ATTACHMENT_CAPACITY"
ATTACHMENT_CAPACITY_PROVIDER = "tokenrhythm"
ATTACHMENT_CAPACITY_MODEL = "kimi-k2.6"
ATTACHMENT_CAPACITY_BASE_CONTEXT_WINDOW_TOKENS = 1_000_000
# Kimi may return provider-billed reasoning tokens even when the request turns
# explicit thinking off. A real 64-token gate consumed 63 reasoning tokens and
# truncated before its completion marker, so use the same bounded smoke floor
# as the TokenRhythm profile matrix. The prompt still requests a short answer.
ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS = 4_096
ATTACHMENT_CAPACITY_PROVIDER_TIMEOUT_SECONDS = 60.0
ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS = 75.0
ATTACHMENT_CAPACITY_TOTAL_TIMEOUT_SECONDS = 120.0
ATTACHMENT_CAPACITY_GATEWAY_READY_TIMEOUT_SECONDS = 45.0
ATTACHMENT_CAPACITY_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS = 15.0
# The shared polling helper can spend up to three seconds in its final history
# read plus its half-second interval after its own deadline check.
ATTACHMENT_CAPACITY_ASSISTANT_POLL_OVERRUN_SECONDS = 4.0
ATTACHMENT_CAPACITY_MIN_RAW_MARGIN_TOKENS = 4_096
ATTACHMENT_CAPACITY_EXPECTED_HISTORY_TURNS = 1
ATTACHMENT_CAPACITY_EXPECTED_MEDIA_BLOCKS = 3
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
_PUBLIC_USAGE_KEYS = frozenset(
    {
        "billed_cost",
        "cache_write_tokens",
        "cached_tokens",
        "cachedTokens",
        "compaction_count",
        "configured_max_output_tokens",
        "cost_source",
        "decoded_history_bytes",
        "history_image_count",
        "history_turn_count",
        "input_tokens",
        "last_history_image_count",
        "models",
        "output_tokens",
        "physical_request_count",
        "physical_response_count",
        "projected_media_fits_at_max_thinking",
        "projected_media_tokens",
        "provider_proof_effective_token_budget",
        "provider_proof_estimated_tokens",
        "provider_proof_fits",
        "provider_proof_media_blocks",
        "raw_history_estimated_tokens",
        "raw_history_fits_at_zero_thinking",
        "reasoning_tokens",
        "reasoningTokens",
        "route_max_history_turns",
        "router_admission_token_limit",
        "router_max_thinking_admission_token_limit",
        "source",
        "totalCostUsd",
        "totalInputTokens",
        "totalOutputTokens",
        "totalTokens",
    }
)
_PUBLIC_COST_KEYS = frozenset(
    {
        "billing_scope",
        "cache_read_per_m",
        "cache_write_per_m",
        "cost_source",
        "estimate_basis",
        "input_per_m",
        "opensquilla_estimate",
        "opensquilla_estimated_cost_usd",
        "output_per_m",
        "price_source",
        "provider_billed",
        "provider_billed_cost_usd",
        "raw_gateway_usage_billed_cost_usd",
        "source",
    }
)
_PUBLIC_USAGE_BOOLEAN_KEYS = frozenset(
    {
        "projected_media_fits_at_max_thinking",
        "provider_proof_fits",
        "raw_history_fits_at_zero_thinking",
    }
)
_PUBLIC_USAGE_STRING_KEYS = frozenset({"cost_source", "source"})
_PUBLIC_USAGE_STRING_LIST_KEYS = frozenset({"models"})
_PUBLIC_USAGE_NUMERIC_KEYS = (
    _PUBLIC_USAGE_KEYS
    - _PUBLIC_USAGE_BOOLEAN_KEYS
    - _PUBLIC_USAGE_STRING_KEYS
    - _PUBLIC_USAGE_STRING_LIST_KEYS
)
_PUBLIC_COST_STRING_KEYS = frozenset(
    {"billing_scope", "cost_source", "estimate_basis", "price_source", "source"}
)
_PUBLIC_COST_NUMERIC_KEYS = _PUBLIC_COST_KEYS - _PUBLIC_COST_STRING_KEYS
_PUBLIC_STATUS_VALUES = frozenset({"passed", "failed", "skipped"})
_PUBLIC_TEXT_LIMIT = 256
_PUBLIC_ACCOUNTING_TEXT_LIMIT = 128
_PUBLIC_MODEL_LIST_LIMIT = 16

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
        f"E2E_{_marker_component(provider)}_{_marker_component(slot)}_{_marker_component(case_id)}"
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
    for slot in (*TEXT_PROFILE_SLOTS, "image_model"):
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
            "ensemble_enabled",
            "ensemble_selection_mode",
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
    agent_max_iterations: int = LIVE_AGENT_MAX_ITERATIONS,
    llm_request_timeout_seconds: float = 90.0,
    agent_runtime_timeout_seconds: float = LIVE_AGENT_RUNTIME_TIMEOUT_SECONDS,
    turn_hard_deadline_seconds: float = LIVE_TURN_HARD_DEADLINE_SECONDS,
    model_context_window_tokens: int | None = None,
    model_supports_vision_override: str | None = None,
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
    model_override_fields: dict[str, dict[str, Any]] = {}
    if model_context_window_tokens is not None:
        model_override_fields.setdefault(model, {})["context_window"] = max(
            1,
            int(model_context_window_tokens),
        )
    if model_supports_vision_override is not None:
        model_override_fields.setdefault(model_supports_vision_override, {})[
            "supports_vision"
        ] = True
    model_override_lines: list[str] = []
    for override_model, fields in model_override_fields.items():
        model_override_lines.extend(
            (
                "",
                f"[models.{_toml_value(provider)}.{_toml_value(override_model)}]",
                *(f"{key} = {_toml_value(value)}" for key, value in fields.items()),
            )
        )
    model_override_toml = "\n".join(model_override_lines)
    path.write_text(
        f"""
host = "127.0.0.1"
debug = false
llm_request_timeout_seconds = {llm_request_timeout_seconds}
agent_runtime_timeout_seconds = {agent_runtime_timeout_seconds}
agent_max_iterations = {agent_max_iterations}
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
turn_hard_deadline_s = {turn_hard_deadline_seconds}

[memory]
source = "state"

[naming]
enabled = false

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
{model_override_toml}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _tokenrhythm_attachment_tiers() -> dict[str, dict[str, Any]]:
    preset = get_preset(ATTACHMENT_CAPACITY_PROVIDER)
    if preset is None:
        raise RuntimeError("TokenRhythm preset is unavailable")
    tiers = preset.tier_defaults()
    image_tier = tiers.get("image_model")
    if not isinstance(image_tier, dict):
        raise RuntimeError("TokenRhythm preset has no image_model tier")
    if image_tier.get("model") != ATTACHMENT_CAPACITY_MODEL:
        raise RuntimeError("TokenRhythm image_model does not match the verified live fixture")
    unsafe_fallback_slots = [
        slot
        for slot in TEXT_PROFILE_SLOTS
        if not isinstance(tiers.get(slot), dict)
        or tiers[slot].get("supports_image") is not False
    ]
    if unsafe_fallback_slots:
        raise RuntimeError(
            "TokenRhythm attachment gate requires every text fallback to be explicitly "
            "non-vision before any live request"
        )
    return tiers


def _attachment_capacity_admission_token_limit(
    *,
    thinking_budget_tokens: int = 0,
) -> int:
    """Return the exact input ceiling used by this special gate's config."""

    catalog = ModelCatalog()
    context_window = catalog.resolve_context_window(
        ATTACHMENT_CAPACITY_MODEL,
        ATTACHMENT_CAPACITY_PROVIDER,
    )
    governor = ContextBudgetGovernor.from_values(
        context_window_tokens=context_window,
        max_output_tokens=ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS,
        thinking_budget_tokens=thinking_budget_tokens,
        context_overflow_threshold=0.85,
    )
    return max(
        1,
        governor.snapshot().provider_request_max_chars // CHARS_PER_TOKEN,
    )


def _attachment_capacity_request_fits(
    request_input_tokens: int,
    *,
    thinking_budget_tokens: int,
) -> bool:
    """Run the production admission predicate with the special gate's limits."""

    return model_has_request_capacity(
        provider=ATTACHMENT_CAPACITY_PROVIDER,
        model=ATTACHMENT_CAPACITY_MODEL,
        material_tokens=max(0, int(request_input_tokens)),
        request_input_tokens=max(0, int(request_input_tokens)),
        thinking_budget_tokens=max(0, int(thinking_budget_tokens)),
        max_output_override_tokens=ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS,
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _deterministic_png(width: int, height: int, *, seed: int) -> bytes:
    """Build a valid, deterministic RGB PNG without optional image libraries."""

    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    state = seed & 0xFFFFFFFF
    rows = bytearray()
    for _row in range(height):
        rows.append(0)
        for _column in range(width * 3):
            state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
            rows.append((state >> 24) & 0xFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=0)),
            _png_chunk(b"IEND", b""),
        )
    )


def _inline_image(name: str, payload: bytes) -> dict[str, str]:
    return {
        "type": "image/png",
        "name": name,
        "data": base64.b64encode(payload).decode("ascii"),
    }


def _inline_history_envelope(text: str, images: list[dict[str, str]]) -> str:
    return json.dumps(
        {"text": text, "attachments": images},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _upload_inline_attachment(
    *,
    port: int,
    attachment: dict[str, str],
    timeout: float,
) -> str:
    payload = base64.b64decode(attachment["data"], validate=True)
    boundary = f"----OpenSquillaAttachmentCapacity{time.time_ns():x}"
    filename = attachment.get("name") or "current.png"
    media_type = attachment.get("type") or "application/octet-stream"
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode()
    body = prefix + payload + f"\r\n--{boundary}--\r\n".encode("ascii")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/files/upload",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result = json.loads(response.read().decode("utf-8"))
    file_uuid = result.get("file_uuid") if isinstance(result, dict) else None
    if not isinstance(file_uuid, str) or not file_uuid.startswith("u-"):
        raise RuntimeError("attachment upload did not return a valid file_uuid")
    return file_uuid


def _attachment_capacity_fixture() -> dict[str, Any]:
    """Return isolated synthetic history whose raw envelope falsely exceeds admission."""

    admission_limit = _attachment_capacity_admission_token_limit(thinking_budget_tokens=0)
    max_thinking_admission_limit = _attachment_capacity_admission_token_limit(
        thinking_budget_tokens=MAX_THINKING_BUDGET_TOKENS
    )
    oldest_width = 128
    fixture: dict[str, Any] | None = None
    while oldest_width <= 512:
        payloads = [
            _deterministic_png(oldest_width, oldest_width, seed=11),
            _deterministic_png(48, 48, seed=22),
            _deterministic_png(48, 48, seed=33),
            _deterministic_png(116, 116, seed=44),
        ]
        images = [
            _inline_image("history-1.png", payloads[0]),
            _inline_image("history-2.png", payloads[1]),
            _inline_image("history-3a.png", payloads[2]),
            _inline_image("history-3b.png", payloads[3]),
        ]
        turns = [
            {
                "user": _inline_history_envelope("Historical image turn one.", [images[0]]),
                "assistant": "Historical answer one.",
            },
            {
                "user": _inline_history_envelope("Historical image turn two.", [images[1]]),
                "assistant": "Historical answer two.",
            },
            {
                "user": _inline_history_envelope(
                    "Historical image turn three.",
                    [images[2], images[3]],
                ),
                "assistant": "Historical answer three.",
            },
        ]
        estimator_output = StringIO()
        with redirect_stdout(estimator_output), redirect_stderr(estimator_output):
            raw_tokens = sum(
                estimate_entry_model_replay_tokens({"content": turn[role]})
                for turn in turns
                for role in ("user", "assistant")
            )
        retained_media_tokens = sum(
            estimate_provider_media_tokens("image", len(payload)) for payload in payloads[2:]
        )
        current_payload = _deterministic_png(71, 71, seed=55)
        projected_media_tokens = retained_media_tokens + estimate_provider_media_tokens(
            "image", len(current_payload)
        )
        raw_fits = _attachment_capacity_request_fits(
            raw_tokens,
            thinking_budget_tokens=0,
        )
        projected_fits_at_max_thinking = _attachment_capacity_request_fits(
            projected_media_tokens,
            thinking_budget_tokens=MAX_THINKING_BUDGET_TOKENS,
        )
        fixture = {
            "turns": turns,
            "current_attachment": _inline_image("current.png", current_payload),
            "excluded_base64": [images[0]["data"], images[1]["data"]],
            "retained_base64": [images[2]["data"], images[3]["data"]],
            "metrics": {
                "history_turn_count": len(turns),
                "history_image_count": len(images),
                "last_history_image_count": 2,
                "raw_history_estimated_tokens": raw_tokens,
                "router_admission_token_limit": admission_limit,
                "router_max_thinking_admission_token_limit": (max_thinking_admission_limit),
                "projected_media_tokens": projected_media_tokens,
                "configured_max_output_tokens": (ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS),
                "raw_history_fits_at_zero_thinking": raw_fits,
                "projected_media_fits_at_max_thinking": (projected_fits_at_max_thinking),
                "decoded_history_bytes": sum(len(payload) for payload in payloads),
            },
        }
        if (
            not raw_fits
            and projected_fits_at_max_thinking
            and raw_tokens >= admission_limit + ATTACHMENT_CAPACITY_MIN_RAW_MARGIN_TOKENS
        ):
            break
        oldest_width += 32
    if fixture is None or (
        fixture["metrics"]["raw_history_estimated_tokens"]
        < admission_limit + ATTACHMENT_CAPACITY_MIN_RAW_MARGIN_TOKENS
        or fixture["metrics"]["raw_history_fits_at_zero_thinking"] is not False
    ):
        raise RuntimeError("unable to construct an over-admission attachment fixture")
    if (
        fixture["metrics"]["projected_media_tokens"] >= max_thinking_admission_limit
        or fixture["metrics"]["projected_media_fits_at_max_thinking"] is not True
    ):
        raise RuntimeError("attachment fixture media projection is not safely below admission")
    return fixture


async def _seed_attachment_capacity_history_async(
    state_dir: Path,
    session_key: str,
    fixture: dict[str, Any],
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage = SessionStorage(str(state_dir / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        await manager.create(
            session_key=session_key,
            agent_id="main",
            display_name="TokenRhythm attachment capacity live fixture",
            model_routing_mode="router",
        )
        for turn in fixture["turns"]:
            await manager.append_message(session_key, "user", turn["user"])
            await manager.append_message(session_key, "assistant", turn["assistant"])
    finally:
        await storage.close()


def _seed_attachment_capacity_history(
    state_dir: Path,
    session_key: str,
    fixture: dict[str, Any],
) -> None:
    asyncio.run(_seed_attachment_capacity_history_async(state_dir, session_key, fixture))


def _attachment_capacity_session_metrics(state_dir: Path, session_key: str) -> dict[str, Any]:
    connection = sqlite3.connect(state_dir / "sessions.db")
    try:
        row = connection.execute(
            "SELECT session_id, compaction_count, input_tokens, output_tokens FROM sessions "
            "WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        usage_row = (
            connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status = 'finalized' THEN 1 "
                "ELSE 0 END), 0) FROM usage_events WHERE session_id = ?",
                (row[0],),
            ).fetchone()
            if row is not None
            else None
        )
        unknown_reasons = (
            connection.execute(
                "SELECT unknown_reason FROM usage_events WHERE session_id = ? "
                "AND status = 'unknown' ORDER BY started_at_ms, event_id",
                (row[0],),
            ).fetchall()
            if row is not None
            else []
        )
    finally:
        connection.close()
    if row is None:
        return {
            "compaction_count": -1,
            "session_input_tokens": 0,
            "session_output_tokens": 0,
            "physical_request_count": -1,
            "physical_response_count": -1,
        }
    physical_failure_kind = None
    for (unknown_reason,) in reversed(unknown_reasons):
        prefix, separator, safe_code = str(unknown_reason or "").partition(":")
        if prefix != "provider_error" or not separator:
            continue
        physical_failure_kind = _attachment_capacity_safe_failure_kind(safe_code)
        if physical_failure_kind is not None:
            break
    return {
        "compaction_count": int(row[1] or 0),
        "session_input_tokens": int(row[2] or 0),
        "session_output_tokens": int(row[3] or 0),
        "physical_request_count": int((usage_row or (0, 0))[0] or 0),
        "physical_response_count": int((usage_row or (0, 0))[1] or 0),
        "physical_failure_kind": physical_failure_kind,
    }


def _provider_proof_from_logs(paths: list[Path]) -> dict[str, Any]:
    """Extract only scalar proof evidence; never retain the surrounding raw log line."""

    proof: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "provider.request_proof" not in line:
                continue
            current: dict[str, Any] = {}
            for field in (
                "estimated_tokens",
                "effective_proof_token_budget",
            ):
                match = re.search(rf"\b{field}=(\d+)", line)
                if match:
                    current[field] = int(match.group(1))
            match = re.search(r"\bmedia_blocks_reserved=(\d+)", line)
            if match:
                current["media_blocks"] = int(match.group(1))
            match = re.search(r"\bfits=(True|False|true|false)", line)
            if match:
                current["fits"] = match.group(1).lower() == "true"
            if current:
                proof = current
    return proof


def _request_projection_evidence(
    request: dict[str, Any],
    fixture: dict[str, Any],
) -> dict[str, Any]:
    payload = request.get("payload") or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    history_texts: set[str] = set()
    for turn in fixture.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        try:
            envelope = json.loads(str(turn.get("user") or ""))
        except (json.JSONDecodeError, ValueError):
            continue
        text = envelope.get("text") if isinstance(envelope, dict) else None
        if isinstance(text, str) and text:
            history_texts.add(text)

    def _message_text_parts(message: dict[str, Any]) -> list[str]:
        content = message.get("content")
        if isinstance(content, str):
            return [content]
        if not isinstance(content, list):
            return []
        return [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]

    history_user_turns = sum(
        1
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and any(
            history_text in text_part
            for text_part in _message_text_parts(message)
            for history_text in history_texts
        )
    )

    typed_image_payloads: list[str] = []
    text_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text_parts.extend(_message_text_parts(message))
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            data = block.get("data")
            if not isinstance(data, str):
                source = block.get("source")
                data = source.get("data") if isinstance(source, dict) else None
            if isinstance(data, str):
                typed_image_payloads.append(data)

    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    excluded_absent = all(value not in serialized for value in fixture["excluded_base64"])
    expected_payloads = [
        *fixture["retained_base64"],
        fixture["current_attachment"]["data"],
    ]
    expected_present = sorted(typed_image_payloads) == sorted(expected_payloads)
    expected_media_text_absent = all(
        expected not in text_part
        for expected in expected_payloads
        for text_part in text_parts
    )
    return {
        "history_user_turn_count": history_user_turns,
        "media_blocks": len(typed_image_payloads),
        "excluded_old_media_absent": excluded_absent,
        "expected_media_present": expected_present,
        "expected_media_text_absent": expected_media_text_absent,
    }


def _attachment_capacity_safe_failure_kind(raw_code: object) -> str | None:
    if not isinstance(raw_code, str | int) or isinstance(raw_code, bool):
        return None
    code = str(raw_code).strip().lower().replace("-", "_")
    if not code or not code.isascii() or len(code) > 64:
        return None
    if code in {
        "401",
        "authentication",
        "provider_auth_invalid",
        "auth_invalid",
    }:
        return "auth"
    if code in {
        "402",
        "provider_insufficient_credits",
        "insufficient_credits",
        "insufficient_quota",
        "usage_limit_reached",
    }:
        return "balance"
    if code in {"403", "permission"}:
        return "not-entitled"
    if code in {
        "404",
        "not_found",
        "provider_model_not_found",
        "model_not_found",
    }:
        return "model-unavailable"
    if code in {
        "429",
        "rate_limit",
        "provider_rate_limited",
        "rate_limited",
    }:
        return "rate-limit"
    numeric_status = int(code) if code.isdigit() and len(code) == 3 else None
    if (
        numeric_status == 408
        or (numeric_status is not None and 500 <= numeric_status <= 599)
        or code
        in {
            "transport",
            "unavailable",
            "provider_provider_overloaded",
            "provider_overloaded",
            "provider_transport_transient",
            "transport_transient",
            "provider_retry_after_deadline",
            "request_error",
            "response_incomplete",
            "timeout",
        }
    ):
        return "transport"
    return None


def _attachment_capacity_llm_error_failure_kind(
    errors: list[dict[str, Any]],
) -> str | None:
    """Classify only the bounded error codes retained by turn-call logging.

    Provider prose is deliberately absent from ``llm_error`` records.  The
    nested ``error.code`` is already projected through
    ``safe_provider_failure_code`` and is therefore the authoritative safe
    signal when the HTTP/UI surface has collapsed an upstream failure to a
    generic task error.
    """

    for row in reversed(errors):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        error = payload.get("error")
        if not isinstance(error, dict):
            continue
        failure_kind = _attachment_capacity_safe_failure_kind(error.get("code"))
        if failure_kind is not None:
            return failure_kind
    return None


_ATTACHMENT_CAPACITY_HTTP_ERROR_RE = re.compile(
    r"provider\.chat_http_error\b.*?\bstatus_code=(\d{3})\b"
)


def _attachment_capacity_provider_http_statuses(paths: list[Path]) -> list[int]:
    """Extract only allowlisted HTTP status scalars from captured provider logs."""

    statuses: list[int] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _ATTACHMENT_CAPACITY_HTTP_ERROR_RE.search(line)
            if match is None:
                continue
            status = int(match.group(1))
            if 400 <= status <= 599 and status not in statuses:
                statuses.append(status)
    return statuses


def _evaluate_attachment_capacity_evidence(
    *,
    records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    session_key: str,
    fixture: dict[str, Any],
    session_metrics: dict[str, Any],
    proof: dict[str, Any],
    turn_error: str | None,
    provider_http_statuses: list[int] | None = None,
) -> dict[str, Any]:
    session_records = [row for row in records if row.get("session_key") == session_key]
    requests = [row for row in session_records if row.get("kind") == "llm_request"]
    responses = [row for row in session_records if row.get("kind") == "llm_response"]
    errors = [row for row in session_records if row.get("kind") == "llm_error"]
    request = requests[0] if len(requests) == 1 else {}
    response = responses[0] if len(responses) == 1 else {}
    projection = _request_projection_evidence(request, fixture) if request else {}
    decision = _decision_for_session(decisions, session_key=session_key)
    router_step = _router_step_from_decision(decision)
    response_usage = (response.get("payload") or {}).get("usage") or {}
    request_model = str(request.get("model") or "")
    response_model = str(response_usage.get("model") or "")
    input_tokens = int(response_usage.get("input_tokens") or 0)
    output_tokens = int(response_usage.get("output_tokens") or 0)
    physical_request_count = int(
        session_metrics.get("physical_request_count", len(requests))
    )
    physical_response_count = int(
        session_metrics.get("physical_response_count", len(responses))
    )
    ok = all(
        (
            not turn_error,
            len(requests) == 1,
            len(responses) == 1,
            not errors,
            request.get("provider") == ATTACHMENT_CAPACITY_PROVIDER,
            response.get("provider") == ATTACHMENT_CAPACITY_PROVIDER,
            request_model == ATTACHMENT_CAPACITY_MODEL,
            response_model == ATTACHMENT_CAPACITY_MODEL,
            router_step.get("routing_source") == "image_route",
            decision.get("image_route_reason") == "current_turn",
            projection.get("history_user_turn_count") == ATTACHMENT_CAPACITY_EXPECTED_HISTORY_TURNS,
            projection.get("media_blocks") == ATTACHMENT_CAPACITY_EXPECTED_MEDIA_BLOCKS,
            projection.get("excluded_old_media_absent") is True,
            projection.get("expected_media_present") is True,
            projection.get("expected_media_text_absent") is True,
            session_metrics.get("compaction_count") == 0,
            physical_request_count == 1,
            physical_response_count == 1,
            proof.get("fits") is True,
            proof.get("media_blocks") == ATTACHMENT_CAPACITY_EXPECTED_MEDIA_BLOCKS,
            input_tokens > 0,
            output_tokens > 0,
        )
    )
    failure_text = turn_error or ""
    if not failure_text and not ok:
        failure_text = "attachment capacity live evidence invariant failed"
    failure_kind = None
    if not ok:
        ledger_failure_kind = session_metrics.get("physical_failure_kind")
        if isinstance(ledger_failure_kind, str):
            failure_kind = ledger_failure_kind
        for status in reversed(provider_http_statuses or []):
            if failure_kind is not None:
                break
            failure_kind = _attachment_capacity_safe_failure_kind(status)
            if failure_kind is not None:
                break
        if failure_kind is None:
            failure_kind = _attachment_capacity_llm_error_failure_kind(errors)
        if failure_kind is None:
            # OpenSquilla error refs are opaque correlation IDs. Strip only
            # the fixed terminal suffix so digits such as ``404`` inside a
            # random ref cannot masquerade as an HTTP status.
            classification_text = re.sub(
                r"\s+\(ref:\s*[0-9a-fA-F]{8}\)\s*$",
                "",
                failure_text,
            )
            failure_kind = classify_failure(classification_text)
    return {
        "ok": ok,
        "failure_kind": failure_kind,
        "actual_model": request_model,
        "actual_request_model": request_model,
        "actual_response_model": response_model,
        "request_count": len(requests),
        "response_count": len(responses),
        "request_projection": projection,
        "router_step": router_step,
        "decision": {
            "image_route_reason": decision.get("image_route_reason"),
        },
        "usage": response_usage,
        "proof": proof,
    }


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
    cache_read_tokens = int(usage.get("cache_read_tokens") or usage.get("cached_tokens") or 0)
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
                    session_key = f"profile-e2e:{provider}:{case['id']}:{int(time.time() * 1000)}"
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
    try:
        os.chmod(tmp_path, 0o700)
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


def _attachment_capacity_remaining_timeout(
    deadline: float,
    cap_seconds: float,
    *,
    overrun_reserve_seconds: float = 0.0,
) -> float:
    """Return a bounded positive wait without extending an absolute deadline."""

    remaining = deadline - time.monotonic() - max(0.0, overrun_reserve_seconds)
    if remaining <= 0:
        return 0.0
    return min(max(0.0, cap_seconds), remaining)


def _wait_for_attachment_gateway_ready(
    proc: subprocess.Popen[Any],
    port: int,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Wait for completed Gateway boot, not merely the live process probe."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None, f"gateway exited early with code {proc.returncode} before readiness"
        remaining = deadline - time.monotonic()
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback readiness probe
                f"http://127.0.0.1:{port}/ready",
                timeout=min(1.0, max(0.05, remaining)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("ready") is True:
                return payload, None
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            # Startup probes may fail transiently; retry within the fixed deadline.
            pass
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return None, "gateway did not become ready before timeout"


def _run_tokenrhythm_attachment_capacity_in_temp(
    *,
    api_key: str,
    base_url: str,
    tmp_path: Path,
    synthetic_vision_capability_override: bool = False,
) -> dict[str, Any]:
    total_deadline = time.monotonic() + ATTACHMENT_CAPACITY_TOTAL_TIMEOUT_SECONDS
    # The shared shutdown helper has a hard 10-second terminate wait followed
    # by a 5-second kill wait. All active readiness/submit/reply waits must end
    # before this work deadline so shutdown cannot push the gate past 120s.
    work_deadline = total_deadline - ATTACHMENT_CAPACITY_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS
    fixture = _attachment_capacity_fixture()
    tiers = _tokenrhythm_attachment_tiers()
    state_dir = tmp_path / "state"
    session_state_dir = state_dir / "state"
    turn_log_dir = tmp_path / "turn-calls"
    user_state_dir = tmp_path / "user-state"
    log_dir = tmp_path / "logs"
    for directory in (turn_log_dir, user_state_dir, log_dir):
        directory.mkdir(mode=0o700)
    session_key = f"agent:main:webchat:attachment-capacity-{int(time.time() * 1000)}"
    _seed_attachment_capacity_history(session_state_dir, session_key, fixture)

    config_path = tmp_path / "gateway.toml"
    _write_config(
        config_path,
        ATTACHMENT_CAPACITY_PROVIDER,
        base_url,
        str(tiers["c1"]["model"]),
        max_tokens=ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS,
        tier_overrides=tiers,
        agent_max_iterations=1,
        llm_request_timeout_seconds=ATTACHMENT_CAPACITY_PROVIDER_TIMEOUT_SECONDS,
        agent_runtime_timeout_seconds=ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS,
        turn_hard_deadline_seconds=ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS,
        model_context_window_tokens=ATTACHMENT_CAPACITY_BASE_CONTEXT_WINDOW_TOKENS,
        model_supports_vision_override=(
            ATTACHMENT_CAPACITY_MODEL if synthetic_vision_capability_override else None
        ),
    )
    port = _free_port()
    provider_spec = get_provider_spec(ATTACHMENT_CAPACITY_PROVIDER)
    env = child_environment(
        ATTACHMENT_CAPACITY_PROVIDER,
        {provider_spec.env_key: api_key},
        base_environment=os.environ,
    )
    isolated_home = str(user_state_dir)
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_USER_STATE_DIR"] = str(user_state_dir)
    env["OPENSQUILLA_LOG_DIR"] = str(log_dir)
    env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

    stdout_path = tmp_path / "gateway.stdout.log"
    stderr_path = tmp_path / "gateway.stderr.log"
    records: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    proof: dict[str, Any] = {}
    provider_http_statuses: list[int] = []
    health: dict[str, Any] | None = None
    turn_error: str | None = None
    assistant: dict[str, Any] | None = None
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
            cwd=tmp_path,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        try:
            readiness_timeout = _attachment_capacity_remaining_timeout(
                work_deadline,
                ATTACHMENT_CAPACITY_GATEWAY_READY_TIMEOUT_SECONDS,
            )
            if readiness_timeout < ATTACHMENT_CAPACITY_GATEWAY_READY_TIMEOUT_SECONDS:
                turn_error = "total deadline exhausted before gateway readiness wait"
            else:
                health, turn_error = _wait_for_attachment_gateway_ready(
                    proc,
                    port,
                    timeout_seconds=readiness_timeout,
                )
            if turn_error is None:
                marker = "ATTACHMENT_CAPACITY_LIVE_OK"
                upload_timeout = _attachment_capacity_remaining_timeout(
                    work_deadline,
                    10.0,
                )
                if upload_timeout <= 0:
                    turn_error = "total deadline exhausted before attachment submit"
                else:
                    current_attachment = fixture["current_attachment"]
                    file_uuid = _upload_inline_attachment(
                        port=port,
                        attachment=current_attachment,
                        timeout=upload_timeout,
                    )
                    submit_timeout = _attachment_capacity_remaining_timeout(
                        work_deadline,
                        10.0,
                    )
                    if submit_timeout <= 0:
                        turn_error = "total deadline exhausted before attachment submit"
                    else:
                        _post_json(
                            f"http://127.0.0.1:{port}/api/chat",
                            {
                                "sessionKey": session_key,
                                "message": (
                                    "请简短描述当前图片，不要调用工具，最后单独输出 "
                                    + marker
                                    + "。"
                                ),
                                "attachments": [
                                    {
                                        "file_uuid": file_uuid,
                                        "mime": current_attachment["type"],
                                        "name": current_attachment["name"],
                                    }
                                ],
                            },
                            timeout=submit_timeout,
                        )
            if turn_error is None:
                reply_timeout = _attachment_capacity_remaining_timeout(
                    work_deadline,
                    ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS,
                    overrun_reserve_seconds=(ATTACHMENT_CAPACITY_ASSISTANT_POLL_OVERRUN_SECONDS),
                )
                if reply_timeout <= 0:
                    turn_error = "total deadline exhausted before assistant reply wait"
                else:
                    assistant, _history, turn_error = _wait_for_assistant_reply(
                        port=port,
                        session_key=session_key,
                        previous_assistant_count=len(fixture["turns"]),
                        timeout_seconds=reply_timeout,
                    )
                if turn_error is None and marker not in str((assistant or {}).get("text") or ""):
                    turn_error = "assistant reply did not contain the bounded completion marker"
        except Exception as exc:  # noqa: BLE001 - compact, sanitized live diagnostic
            turn_error = f"{type(exc).__name__}: {exc}"
        finally:
            _stop_gateway(proc)
            stdout_file.flush()
            stderr_file.flush()
            records = _read_turn_call_records(turn_log_dir)
            decisions = _read_decision_records(tmp_path)
            provider_log_paths = [stdout_path, stderr_path, log_dir / "debug.log"]
            proof = _provider_proof_from_logs(provider_log_paths)
            provider_http_statuses = _attachment_capacity_provider_http_statuses(
                provider_log_paths
            )

    session_metrics = _attachment_capacity_session_metrics(session_state_dir, session_key)
    evidence = _evaluate_attachment_capacity_evidence(
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics=session_metrics,
        proof=proof,
        turn_error=turn_error,
        provider_http_statuses=provider_http_statuses,
    )
    response = _first_record(records, session_key=session_key, kind="llm_response")
    response_payload = response.get("payload") or {}
    usage = _accounting_usage_fields(response_payload.get("usage") or {})
    usage.update(
        {
            **fixture["metrics"],
            "provider_proof_estimated_tokens": proof.get("estimated_tokens"),
            "provider_proof_effective_token_budget": proof.get("effective_proof_token_budget"),
            "provider_proof_media_blocks": proof.get("media_blocks"),
            # Usage-ledger starts are committed immediately before each
            # physical provider dispatch, including selector fallback legs.
            # This is stronger than the outer llm_request turn-call record,
            # which counts one logical Agent call around the whole selector.
            "physical_request_count": session_metrics["physical_request_count"],
            "physical_response_count": session_metrics["physical_response_count"],
            "compaction_count": session_metrics["compaction_count"],
            "route_max_history_turns": evidence["request_projection"].get(
                "history_user_turn_count"
            ),
            "provider_proof_fits": proof.get("fits"),
        }
    )
    actual_request_model = str(evidence.get("actual_request_model") or "")
    actual_response_model = str(evidence.get("actual_response_model") or "")
    case = {
        "ok": evidence["ok"],
        "failure_kind": evidence["failure_kind"],
        "expected_model": ATTACHMENT_CAPACITY_MODEL,
        "actual_request_model": actual_request_model,
        "actual_response_model": actual_response_model,
        "usage": usage,
        "cost": _estimate_cost(
            actual_request_model,
            response_payload.get("usage") or {},
            provider=ATTACHMENT_CAPACITY_PROVIDER,
        ),
        "latency_ms": int(response_payload.get("duration_ms") or 0),
    }
    result = {
        "provider": ATTACHMENT_CAPACITY_PROVIDER,
        "ok": evidence["ok"],
        "provider_ok": evidence["ok"],
        "models_covered": [actual_request_model] if evidence["ok"] else [],
        "failure_kinds": [evidence["failure_kind"]] if evidence["failure_kind"] else [],
        "cases": [case],
        "usage_from_turn_logs": usage,
        "health": health or {},
        "error": turn_error,
    }
    return sanitize_report(result, (api_key,))


def _run_tokenrhythm_attachment_capacity(api_key: str) -> dict[str, Any]:
    requested_base_url = os.environ.get("TOKENRHYTHM_BASE_URL", "").strip()
    base_url = registry_endpoint(
        ATTACHMENT_CAPACITY_PROVIDER,
        requested_base_url or None,
    )
    tmp_path = Path(tempfile.mkdtemp(prefix="opensquilla-tokenrhythm-attachment-capacity-"))
    try:
        os.chmod(tmp_path, 0o700)
        return _run_tokenrhythm_attachment_capacity_in_temp(
            api_key=api_key,
            base_url=base_url,
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
    # This is a live-profile test floor, not a product default or runtime clamp.
    max_tokens = max(max_tokens, 4096) if provider == "tokenrhythm" else max_tokens
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
            "tier_profile": (provider if provider in LEGACY_PROVIDER_PRESET_IDS else None),
            "tier_mode": (
                "legacy_profile" if provider in LEGACY_PROVIDER_PRESET_IDS else "inline_preset"
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
        tier_overrides=(tiers if provider not in LEGACY_PROVIDER_PRESET_IDS else None),
    )
    all_cases = list(natural.get("cases") or [])
    coverage_batches: list[dict[str, Any]] = []
    for missing_slot in _missing_profile_slots(tiers, all_cases):
        target_case = {
            "slot": missing_slot,
            "id": f"coverage_{missing_slot}",
            "message": ("不要调用工具，请只回复一句中文短句并包含 {marker}。"),
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
    coverage_cases = [row for row in all_cases if row.get("case_mode") == "coverage_compensation"]
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
            "legacy_profile" if provider in LEGACY_PROVIDER_PRESET_IDS else "inline_preset"
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
        _project_public_result(
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
        )
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
    if status not in _PUBLIC_STATUS_VALUES:
        status = "failed"
    failure_class = row.get("failure_class")
    if status == "passed":
        failure_class = None
    elif failure_class is None:
        failure_class = "implementation"
    raw_usage = row.get("usage")
    raw_cost = row.get("cost")
    usage = _project_public_accounting(raw_usage, usage=True)
    cost = _project_public_accounting(raw_cost, usage=False)
    return {
        "provider": _bounded_public_text(row.get("provider"), limit=_PUBLIC_TEXT_LIMIT),
        "model": _bounded_public_text(row.get("model"), limit=_PUBLIC_TEXT_LIMIT),
        "status": status,
        "failure_class": (
            _bounded_public_text(failure_class, limit=_PUBLIC_ACCOUNTING_TEXT_LIMIT)
            or "implementation"
            if failure_class is not None
            else None
        ),
        "usage": usage,
        "cost": cost,
        "latency_ms": int(row.get("latency_ms") or 0),
    }


def _bounded_public_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    if not value or len(value) > limit:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return ""
    return value


def _is_public_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _project_public_accounting(raw: object, *, usage: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    numeric_keys = _PUBLIC_USAGE_NUMERIC_KEYS if usage else _PUBLIC_COST_NUMERIC_KEYS
    string_keys = _PUBLIC_USAGE_STRING_KEYS if usage else _PUBLIC_COST_STRING_KEYS
    boolean_keys = _PUBLIC_USAGE_BOOLEAN_KEYS if usage else frozenset()
    string_list_keys = _PUBLIC_USAGE_STRING_LIST_KEYS if usage else frozenset()
    projected: dict[str, Any] = {}
    for raw_key, value in raw.items():
        if not isinstance(raw_key, str):
            continue
        if value is None and raw_key in numeric_keys | string_keys:
            projected[raw_key] = None
        elif raw_key in numeric_keys and _is_public_number(value):
            projected[raw_key] = value
        elif raw_key in boolean_keys and isinstance(value, bool):
            projected[raw_key] = value
        elif raw_key in string_keys:
            safe_text = _bounded_public_text(value, limit=_PUBLIC_ACCOUNTING_TEXT_LIMIT)
            if safe_text:
                projected[raw_key] = safe_text
        elif raw_key in string_list_keys and isinstance(value, list):
            if len(value) <= _PUBLIC_MODEL_LIST_LIMIT:
                safe_values = [
                    _bounded_public_text(item, limit=_PUBLIC_TEXT_LIMIT) for item in value
                ]
                if all(safe_values) and len(safe_values) == len(value):
                    projected[raw_key] = safe_values
    return projected


def _public_accounting_value_is_valid(key: str, value: object, *, usage: bool) -> bool:
    numeric_keys = _PUBLIC_USAGE_NUMERIC_KEYS if usage else _PUBLIC_COST_NUMERIC_KEYS
    string_keys = _PUBLIC_USAGE_STRING_KEYS if usage else _PUBLIC_COST_STRING_KEYS
    boolean_keys = _PUBLIC_USAGE_BOOLEAN_KEYS if usage else frozenset()
    string_list_keys = _PUBLIC_USAGE_STRING_LIST_KEYS if usage else frozenset()
    if value is None:
        return key in numeric_keys | string_keys
    if key in numeric_keys:
        return _is_public_number(value)
    if key in boolean_keys:
        return isinstance(value, bool)
    if key in string_keys:
        return bool(_bounded_public_text(value, limit=_PUBLIC_ACCOUNTING_TEXT_LIMIT))
    if key in string_list_keys:
        return (
            isinstance(value, list)
            and len(value) <= _PUBLIC_MODEL_LIST_LIMIT
            and all(
                bool(_bounded_public_text(item, limit=_PUBLIC_TEXT_LIMIT)) for item in value
            )
        )
    return False


def _assert_public_report_schema(report: Any) -> None:
    """Require an array of exact public rows before and after sanitizing."""

    if not isinstance(report, list):
        raise RuntimeError("public live report must be a JSON array")
    for index, row in enumerate(report):
        if not isinstance(row, dict) or set(row) != _PUBLIC_RESULT_KEYS:
            raise RuntimeError(f"public live report row {index} has an invalid field set")
        provider = _bounded_public_text(row["provider"], limit=_PUBLIC_TEXT_LIMIT)
        model = row["model"]
        if (
            not provider
            or not isinstance(model, str)
            or (model and not _bounded_public_text(model, limit=_PUBLIC_TEXT_LIMIT))
            or row["status"] not in _PUBLIC_STATUS_VALUES
        ):
            raise RuntimeError(f"public live report row {index} has an invalid identity")
        if row["failure_class"] is not None and not _bounded_public_text(
            row["failure_class"], limit=_PUBLIC_ACCOUNTING_TEXT_LIMIT
        ):
            raise RuntimeError(f"public live report row {index} has an invalid failure class")
        if not isinstance(row["usage"], dict) or not isinstance(row["cost"], dict):
            raise RuntimeError(f"public live report row {index} has invalid accounting fields")
        if not set(row["usage"]).issubset(_PUBLIC_USAGE_KEYS):
            raise RuntimeError(f"public live report row {index} has invalid usage fields")
        if not set(row["cost"]).issubset(_PUBLIC_COST_KEYS):
            raise RuntimeError(f"public live report row {index} has invalid cost fields")
        if not all(
            _public_accounting_value_is_valid(key, value, usage=True)
            for key, value in row["usage"].items()
        ):
            raise RuntimeError(f"public live report row {index} has invalid usage values")
        if not all(
            _public_accounting_value_is_valid(key, value, usage=False)
            for key, value in row["cost"].items()
        ):
            raise RuntimeError(f"public live report row {index} has invalid cost values")
        if not _is_public_number(row["latency_ms"]):
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
                                None if ok else str(case.get("failure_kind") or "implementation")
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


def _discard_live_report_output(output: Path) -> bool:
    """Best-effort removal for a failed run without masking its first error."""

    try:
        output.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", nargs="+", default=DEFAULT_PROVIDERS)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--attachment-capacity",
        action="store_true",
        help="run only the isolated TokenRhythm attachment-capacity live gate",
    )
    parser.add_argument(
        "--confirm-live-cost",
        action="store_true",
        help="confirm that the selected live gate may make its bounded provider call",
    )
    args = parser.parse_args()
    output = Path(args.output)
    if not is_temporary_report_path(output):
        parser.error("--output must be inside the system temporary directory")
    if output.exists():
        if output.is_symlink() or not output.is_file():
            parser.error("--output must name a regular file")
        try:
            output.unlink()
        except OSError:
            parser.error("--output could not be cleared before the live run")
    if not 32 <= args.max_tokens <= 64:
        parser.error("--max-tokens must be between 32 and 64")

    if args.attachment_capacity:
        if not args.confirm_live_cost:
            parser.error("--attachment-capacity requires --confirm-live-cost")
        if os.environ.get(ATTACHMENT_CAPACITY_OPT_IN_ENV) != "1":
            parser.error(f"--attachment-capacity requires {ATTACHMENT_CAPACITY_OPT_IN_ENV}=1")
        if not os.environ.get("TOKENRHYTHM_API_KEY", "").strip():
            parser.error("--attachment-capacity requires TOKENRHYTHM_API_KEY in the environment")

    if (
        not args.attachment_capacity
        and not args.no_env_file
        and os.environ.get("OPENSQUILLA_LIVE_DISABLE_DOTENV") != "1"
    ):
        _load_env_quietly()
    secrets: dict[str, str] = {}
    for name in provider_secret_names():
        raw_value = os.environ.get(name, "")
        if not raw_value:
            continue
        secrets[name] = raw_value
        stripped_value = raw_value.strip()
        if stripped_value and stripped_value != raw_value:
            secrets[f"{name}:stripped"] = stripped_value
    attachment_api_key = (
        os.environ.get("TOKENRHYTHM_API_KEY", "").strip()
        if args.attachment_capacity
        else ""
    )
    try:
        if args.attachment_capacity:
            raw_results = [_run_tokenrhythm_attachment_capacity(attachment_api_key)]
        else:
            raw_results = [
                _run_provider(
                    provider,
                    max_tokens=args.max_tokens,
                    timeout_seconds=args.timeout_seconds,
                )
                for provider in args.providers
            ]
    except Exception as exc:  # noqa: BLE001 - live harness must fail closed
        removed = _discard_live_report_output(output)
        cleanup_suffix = "" if removed else "; incomplete output removal also failed"
        print(
            redact_text(
                f"provider profile gateway matrix failed: {exc}{cleanup_suffix}",
                secrets,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if args.attachment_capacity:
            # This can only clear the harness process. The caller must also
            # remove its exported value and rotate a credential exposed during
            # an investigation.
            os.environ.pop("TOKENRHYTHM_API_KEY", None)
    try:
        summaries = [_public_provider_result(result) for result in raw_results]
        all_ok = all(result.get("status") == "passed" for result in summaries)
        payload = _public_report_rows(raw_results)
        _emit_main_diagnostics(summaries, payload, secrets)
        payload = sanitize_report(payload, secrets)
        if report_contains_secret(payload, secrets):
            raise RuntimeError("refusing to write a report containing provider credentials")
        payload = write_safe_report(output, payload, secrets)
        _assert_public_report_schema(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - no traceback or stale report on failure
        removed = _discard_live_report_output(output)
        cleanup_suffix = "" if removed else "; incomplete output removal also failed"
        print(
            redact_text(f"unable to write live report: {exc}{cleanup_suffix}", secrets),
            file=sys.stderr,
        )
        return 2
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
