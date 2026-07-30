#!/usr/bin/env python3
"""Run the fixed, low-cost live provider baseline with isolated credentials.

The parent process parses ``provider.keys`` as inert data.  Each provider key
is injected only into a provider-specific child environment; no credential is
placed in argv, TOML, a repository file, or the final report.  Endpoints are
always resolved from the checked-in provider registry.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.provider.preset_registry import get_preset  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    child_environment,
    classify_failure,
    is_temporary_report_path,
    parse_provider_keys_file,
    provider_response_model_matches,
    provider_secret_names,
    redact_text,
    registry_endpoint,
    report_contains_secret,
    sanitize_report,
    write_safe_report,
)
from scripts.live_harness_security import is_premium_model as _shared_is_premium_model  # noqa: E402

# Keep this bounded provider set stable so opt-in live reports remain
# comparable across runs. Missing or empty credentials are reported as skips.
DEFAULT_PROVIDERS = (
    "dashscope",
    "openai",
    "deepseek",
    "gemini",
    "moonshot",
    "zhipu",
    "volcengine",
    "qianfan",
    "minimax",
)

# These entries are intentionally represented as skips.  The harness must not
# fill them from ambient environment variables or substitute another provider.
EXPLICIT_EMPTY_PROVIDERS = (
    "openrouter",
    "siliconflow",
    "groq",
    "mistral",
    "byteplus",
    "aihubmix",
)

STAGE_ORDER = (
    "probe",
    "adapter_stream",
    "openai_responses_probe",
    "openai_responses_stream",
    "gateway_main",
)
_PROBE_STAGES = frozenset({"probe", "openai_responses_probe"})
_STREAM_STAGES = frozenset(
    {"adapter_stream", "openai_responses_stream", "deep_multi_model"}
)
_SPECIAL_STAGES = frozenset(
    {"thinking_off", "thinking_on", "vision_synthetic_color_block"}
)
_TRANSIENT_FAILURES = frozenset({"rate-limit", "transport"})
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

# Multi-model coverage prefers the five providers called out by the live-test
# contract.  A failed/missing primary slot is filled, in order, from the
# bounded fallback set.  This selection happens only after the provider's
# one-token probe succeeds.
DEEP_PROVIDER_PRIORITY = ("deepseek", "openai", "gemini", "dashscope", "zhipu")
DEEP_PROVIDER_FALLBACK = ("moonshot", "volcengine", "qianfan")
_THINKING_PROVIDERS = frozenset(
    {"deepseek", "openai", "gemini", "dashscope", "zhipu", "moonshot", "volcengine"}
)
_VISION_PROVIDER_ORDER = ("gemini", "moonshot", "qianfan")
_VISION_PROVIDER_LIMIT = 2

_MODEL_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "dashscope": ("DASHSCOPE_MODEL",),
    "openai": ("OPENAI_MODEL",),
    "deepseek": ("DEEPSEEK_MODEL", "DEEPSEEK_REASONER_MODEL"),
    "gemini": ("GEMINI_MODEL",),
    "moonshot": ("MOONSHOT_MODEL", "KIMI_MODEL"),
    "zhipu": ("ZAI_MODEL", "ZHIPU_MODEL"),
    "volcengine": ("VOLCENGINE_MODEL",),
    "qianfan": ("QIANFAN_MODEL",),
    "minimax": ("MINIMAX_MODEL",),
    "openrouter": ("OPENROUTER_MODEL",),
    "siliconflow": ("SILICONFLOW_MODEL",),
    "groq": ("GROQ_MODEL",),
    "mistral": ("MISTRAL_MODEL",),
    "byteplus": ("BYTEPLUS_MODEL",),
    "aihubmix": ("AIHUBMIX_MODEL",),
}

_DEFAULT_MODELS = {
    "dashscope": "qwen3.7-plus",
    "openai": "gpt-5.4-mini",
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-3.5-flash",
    "moonshot": "kimi-k2.6",
    "zhipu": "glm-5",
    "volcengine": "doubao-seed-2-0-lite-260215",
    "qianfan": "ernie-4.5-turbo-128k",
    "minimax": "MiniMax-M2.7",
    "openrouter": "deepseek/deepseek-v4-flash",
    "siliconflow": "Qwen/Qwen3-8B",
    "groq": "llama-3.1-8b-instant",
    "mistral": "mistral-small-latest",
    "byteplus": "seed-2-0-lite-260228",
    "aihubmix": "gpt-4o-mini",
}

_USAGE_KEYS = frozenset(
    {
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_write_tokens",
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)
_COST_KEYS = frozenset(
    {
        "billing_scope",
        "cost_source",
        "opensquilla_estimate",
        "opensquilla_estimated_cost_usd",
        "provider_billed",
        "provider_billed_cost_usd",
        "source",
    }
)


def _is_premium_model(model: str) -> bool:
    return _shared_is_premium_model(model)


def _provider_model(provider: str, metadata: Mapping[str, str]) -> str:
    for name in _MODEL_ENV_NAMES.get(provider, ()):
        model = str(metadata.get(name) or "").strip()
        if model and not _is_premium_model(model):
            return model
    return _DEFAULT_MODELS[provider]


def _preset_model(provider: str, tier: str) -> str:
    preset = get_preset(provider)
    if preset is None:
        return ""
    entry = preset.tiers.get(tier)
    if not isinstance(entry, Mapping):
        return ""
    if str(entry.get("provider") or provider).strip().lower() != provider:
        return ""
    return str(entry.get("model") or "").strip()


def _deep_models(provider: str, metadata: Mapping[str, str]) -> tuple[str, ...]:
    """Return file model + repository c0/c2 models, safely de-duplicated."""

    candidates = (
        _provider_model(provider, metadata),
        _preset_model(provider, "c0"),
        _preset_model(provider, "c2"),
    )
    return tuple(
        dict.fromkeys(
            model
            for model in candidates
            if model and not _is_premium_model(model)
        )
    )


def _selected_deep_providers(probe_passed: set[str]) -> tuple[str, ...]:
    selected = [provider for provider in DEEP_PROVIDER_PRIORITY if provider in probe_passed]
    for provider in DEEP_PROVIDER_FALLBACK:
        if len(selected) >= len(DEEP_PROVIDER_PRIORITY):
            break
        if provider in probe_passed:
            selected.append(provider)
    return tuple(selected)


def _model_capabilities(provider: str, model: str) -> Any:
    # Local import keeps baseline inventory/help usage light and avoids
    # initializing the catalog in the credential-owning parent unless a deep
    # case is actually being planned.
    from opensquilla.provider.model_catalog import ModelCatalog  # noqa: PLC0415

    return ModelCatalog().get_capabilities(
        model,
        provider_name=provider,
        base_url=registry_endpoint(provider),
    )


def _thinking_model(provider: str, metadata: Mapping[str, str]) -> str:
    if provider not in _THINKING_PROVIDERS:
        return ""
    model = _provider_model(provider, metadata)
    if _is_premium_model(model):
        return ""
    return model if _model_capabilities(provider, model).supports_reasoning else ""


def _vision_model(provider: str, metadata: Mapping[str, str]) -> str:
    candidates = [_provider_model(provider, metadata), _preset_model(provider, "image_model")]
    for model in dict.fromkeys(candidate for candidate in candidates if candidate):
        if not _is_premium_model(model) and _model_capabilities(
            provider, model
        ).supports_vision:
            return model
    return ""


def _synthetic_color_block_png_base64() -> str:
    """Generate an 8x8 opaque red PNG without reading any operator media."""

    width = height = 8
    scanline = b"\x00" + (b"\xff\x00\x00" * width)
    pixels = scanline * height

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(pixels))
    image += chunk(b"IEND", b"")
    return base64.b64encode(image).decode("ascii")


def _stage_runtime_provider(stage: str, provider: str) -> str:
    if stage in {"openai_responses_probe", "openai_responses_stream"}:
        if provider != "openai":
            raise ValueError(f"{stage} is only valid for openai")
        return "openai_responses"
    return provider


def _stage_command(
    stage: str,
    provider: str,
    model: str,
    output: Path,
    *,
    smoke_max_tokens: int,
    gateway_max_tokens: int,
    gateway_timeout_seconds: float,
    special_max_tokens: int = 256,
) -> list[str]:
    """Build a shell-free stage command whose argv contains no credential."""

    runtime_provider = _stage_runtime_provider(stage, provider)
    if stage in _PROBE_STAGES | _STREAM_STAGES:
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "live_provider_profile_smoke.py"),
            "--provider",
            runtime_provider,
            "--model",
            model,
            "--base-url",
            registry_endpoint(runtime_provider),
            "--max-tokens",
            "1" if stage in _PROBE_STAGES else str(smoke_max_tokens),
            "--no-env-file",
            "--child-report",
            "--output",
            str(output),
        ]
        if stage in _PROBE_STAGES:
            command.extend(("--skip-stream", "--exact-max-tokens"))
        return command
    if stage in _SPECIAL_STAGES:
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-special",
            "--special-stage",
            stage,
            "--provider",
            provider,
            "--model",
            model,
            "--output",
            str(output),
            "--special-max-tokens",
            str(special_max_tokens),
        ]
    if stage == "gateway_main":
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-gateway",
            "--provider",
            provider,
            "--model",
            model,
            "--output",
            str(output),
            "--gateway-max-tokens",
            str(gateway_max_tokens),
            "--gateway-timeout-seconds",
            str(gateway_timeout_seconds),
        ]
    raise ValueError(f"unknown live matrix stage {stage!r}")


def _read_stage_report(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _run_stage(
    *,
    stage: str,
    provider: str,
    command: list[str],
    env: dict[str, str],
    output: Path,
    secrets: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Execute one child and return an in-memory diagnostic envelope.

    The child report is removed immediately after reading.  Captured output is
    retained only as a redacted in-memory classification input and is never
    included in the final report.
    """

    output.unlink(missing_ok=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        child_report = _read_stage_report(output)
        return {
            "returncode": completed.returncode,
            "latency_ms": elapsed_ms,
            "report": sanitize_report(child_report, secrets) if child_report else None,
            "stdout": redact_text(completed.stdout, secrets),
            "stderr": redact_text(completed.stderr, secrets),
            "spawn_failure": False,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "report": None,
            "stdout": redact_text(exc.stdout or "", secrets),
            "stderr": redact_text(exc.stderr or "", secrets),
            "spawn_failure": False,
            "timeout": True,
        }
    except OSError as exc:
        return {
            "returncode": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "report": None,
            "stdout": "",
            "stderr": redact_text(f"{type(exc).__name__}: {exc}", secrets),
            "spawn_failure": True,
            "timeout": False,
        }
    finally:
        output.unlink(missing_ok=True)


def _first_result(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    results = report.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {}
    return results[0]


def _normalize_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text in _USAGE_KEYS and (item is None or isinstance(item, int | float)):
            normalized[key_text] = item
        elif key_text in {"direct", "stream", "gateway"}:
            nested = _normalize_usage(item)
            if nested:
                normalized[key_text] = nested
    return normalized


def _normalize_cost(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) in _COST_KEYS and (item is None or isinstance(item, str | int | float))
    }


def _usage_nonzero(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if key in _USAGE_KEYS and isinstance(item, int | float) and item > 0:
            return True
        if isinstance(item, dict) and _usage_nonzero(item):
            return True
    return False


def _failure_class_from_text(text: str) -> str:
    return classify_failure(text)


def _execution_failure_class(execution: Mapping[str, Any]) -> str:
    if execution.get("timeout") or execution.get("spawn_failure"):
        return "transport"
    diagnostic = json.dumps(execution.get("report"), ensure_ascii=False, sort_keys=True)
    diagnostic += " " + str(execution.get("stdout") or "")
    diagnostic += " " + str(execution.get("stderr") or "")
    return _failure_class_from_text(diagnostic)


def _stage_success(stage: str, execution: Mapping[str, Any]) -> bool:
    row = _first_result(execution.get("report"))
    if stage in _PROBE_STAGES:
        # A one-token probe commonly cannot emit the full marker.  A parsed
        # content_mismatch still proves the authenticated request was accepted.
        return row.get("direct_status") in {"passed", "content_mismatch"}
    if stage in _STREAM_STAGES:
        return bool(
            execution.get("returncode") == 0
            and row.get("direct_status") == "passed"
            and row.get("stream_status") == "passed"
            and str(row.get("response_model") or "").strip()
            and _usage_nonzero(row.get("usage"))
        )
    if stage == "gateway_main":
        return bool(
            execution.get("returncode") == 0
            and row.get("status") == "passed"
            and str(row.get("model") or "").strip()
            and _usage_nonzero(row.get("usage"))
        )
    if stage in _SPECIAL_STAGES:
        return bool(
            execution.get("returncode") == 0
            and row.get("status") == "passed"
            and row.get("done_event") is True
            and row.get("marker_verified") is True
            and str(row.get("model") or "").strip()
            and _usage_nonzero(row.get("usage"))
        )
    return False


def _stage_summary(
    stage: str,
    provider: str,
    requested_model: str,
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    row = _first_result(execution.get("report"))
    passed = _stage_success(stage, execution)
    response_model = str(row.get("response_model") or row.get("model") or "").strip()
    model_matches = provider_response_model_matches(provider, requested_model, response_model)
    if passed and stage not in _PROBE_STAGES and not model_matches:
        passed = False
    return {
        "stage": stage,
        "provider": provider,
        "model": response_model or requested_model,
        "status": "passed" if passed else "failed",
        "failure_class": (
            None
            if passed
            else (
                "implementation"
                if response_model and not model_matches
                else _execution_failure_class(execution)
            )
        ),
        "usage": _normalize_usage(row.get("usage")),
        "cost": _normalize_cost(row.get("cost")),
        "latency_ms": int(execution.get("latency_ms") or 0),
    }


def _skipped_stage(stage: str, provider: str, model: str, reason: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "provider": provider,
        "model": model,
        "status": "skipped",
        "failure_class": reason,
        "usage": {},
        "cost": {},
        "latency_ms": 0,
    }


def _execute_stage(
    *,
    stage: str,
    provider: str,
    model: str,
    output: Path,
    env: dict[str, str],
    secrets: Mapping[str, str],
    smoke_max_tokens: int,
    gateway_max_tokens: int,
    gateway_timeout_seconds: float,
    stage_timeout_seconds: float,
    special_max_tokens: int = 256,
) -> dict[str, Any]:
    command = _stage_command(
        stage,
        provider,
        model,
        output,
        smoke_max_tokens=smoke_max_tokens,
        gateway_max_tokens=gateway_max_tokens,
        gateway_timeout_seconds=gateway_timeout_seconds,
        special_max_tokens=special_max_tokens,
    )
    summary: dict[str, Any] | None = None
    for _attempt in range(2):
        execution = _run_stage(
            stage=stage,
            provider=provider,
            command=command,
            env=env,
            output=output,
            secrets=secrets,
            timeout_seconds=stage_timeout_seconds,
        )
        summary = _stage_summary(stage, provider, model, execution)
        if summary["status"] == "passed" or summary["failure_class"] not in _TRANSIENT_FAILURES:
            break
    assert summary is not None
    return summary


def _provider_result(
    *,
    provider: str,
    model: str,
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    required = [stage for stage in stages if stage["failure_class"] != "not-applicable"]
    passed = bool(required) and all(stage["status"] == "passed" for stage in required)
    failure_class = next(
        (str(stage["failure_class"]) for stage in required if stage["status"] != "passed"),
        None,
    )
    return {
        "provider": provider,
        "model": model,
        "status": (
            "passed"
            if passed
            else ("skipped" if failure_class == "missing-credential" else "failed")
        ),
        "failure_class": failure_class,
        "usage": {},
        "cost": {},
        "latency_ms": sum(int(stage["latency_ms"]) for stage in stages),
        "stages": stages,
    }


def _project_public_result(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one in-memory diagnostic row onto the persisted report contract."""

    status = str(row.get("status") or "failed")
    failure_class = row.get("failure_class")
    usage = row.get("usage")
    cost = row.get("cost")
    return {
        "provider": str(row.get("provider") or ""),
        "model": str(row.get("model") or ""),
        "status": status,
        "failure_class": (
            None
            if failure_class is None
            else str(failure_class)
        ),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "cost": dict(cost) if isinstance(cost, Mapping) else {},
        "latency_ms": int(row.get("latency_ms") or 0),
    }


def _assert_public_report_schema(report: Any) -> None:
    """Require an array of exact public result rows before and after sanitizing."""

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


def _public_report_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten final matrix cases while retaining full evidence only in memory."""

    diagnostic_rows: list[Mapping[str, Any]] = []
    for provider_row in report.get("providers") or []:
        if not isinstance(provider_row, Mapping):
            continue
        stages = [row for row in provider_row.get("stages") or [] if isinstance(row, Mapping)]
        diagnostic_rows.extend(stages or [provider_row])
    for group_name in (
        "explicit_empty_provider_inventory",
        "deep_multi_model",
        "thinking",
        "vision",
    ):
        diagnostic_rows.extend(
            row for row in report.get(group_name) or [] if isinstance(row, Mapping)
        )

    public_rows = [_project_public_result(row) for row in diagnostic_rows]
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

    diagnostic = {
        "source_file_mode": f"{file_mode:04o}",
        "permission_warning": permission_warning,
        "ignored_line_numbers": list(ignored_line_numbers),
        "coverage": {
            "status": "passed" if report.get("ok") is True else "incomplete",
            "provider_count": len(report.get("providers") or []),
            "deep_provider_selection": list(report.get("deep_provider_selection") or []),
            "deep_coverage_complete": report.get("deep_coverage_complete") is True,
        },
    }
    message = "live matrix diagnostics: " + json.dumps(
        diagnostic,
        ensure_ascii=False,
        sort_keys=True,
    )
    print(redact_text(message, secrets), file=sys.stderr)


def _deep_case_rows(
    *,
    probe_passed: set[str],
    provider_envs: Mapping[str, dict[str, str]],
    metadata: Mapping[str, str],
    temp_root: Path,
    secrets: Mapping[str, str],
    smoke_max_tokens: int,
    gateway_max_tokens: int,
    gateway_timeout_seconds: float,
    stage_timeout_seconds: float,
    special_max_tokens: int,
) -> tuple[tuple[str, ...], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute the bounded probe-gated multi-model/thinking/vision matrix."""

    selected = _selected_deep_providers(probe_passed)
    deep_rows: list[dict[str, Any]] = []
    thinking_rows: list[dict[str, Any]] = []
    vision_rows: list[dict[str, Any]] = []

    common = {
        "secrets": secrets,
        "smoke_max_tokens": smoke_max_tokens,
        "gateway_max_tokens": gateway_max_tokens,
        "gateway_timeout_seconds": gateway_timeout_seconds,
        "stage_timeout_seconds": stage_timeout_seconds,
        "special_max_tokens": special_max_tokens,
    }
    case_index = 0
    for provider in selected:
        env = provider_envs[provider]
        for model in _deep_models(provider, metadata):
            deep_rows.append(
                _execute_stage(
                    stage="deep_multi_model",
                    provider=provider,
                    model=model,
                    output=temp_root / f"deep-{case_index}.json",
                    env=env,
                    **common,
                )
            )
            case_index += 1

        thinking_model = _thinking_model(provider, metadata)
        if thinking_model:
            for stage in ("thinking_off", "thinking_on"):
                thinking_rows.append(
                    _execute_stage(
                        stage=stage,
                        provider=provider,
                        model=thinking_model,
                        output=temp_root / f"thinking-{case_index}.json",
                        env=env,
                        **common,
                    )
                )
                case_index += 1

    vision_selected = 0
    for provider in _VISION_PROVIDER_ORDER:
        if provider not in probe_passed or vision_selected >= _VISION_PROVIDER_LIMIT:
            continue
        model = _vision_model(provider, metadata)
        if not model:
            continue
        vision_rows.append(
            _execute_stage(
                stage="vision_synthetic_color_block",
                provider=provider,
                model=model,
                output=temp_root / f"vision-{case_index}.json",
                env=provider_envs[provider],
                **common,
            )
        )
        case_index += 1
        vision_selected += 1

    return selected, deep_rows, thinking_rows, vision_rows


def run_matrix(
    *,
    providers: list[str],
    secrets: dict[str, str],
    smoke_max_tokens: int,
    gateway_max_tokens: int,
    gateway_timeout_seconds: float,
    stage_timeout_seconds: float,
    special_max_tokens: int = 256,
    models: Mapping[str, str] | None = None,
    base_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the probe-gated baseline and return a strictly scrubbed report."""

    if not 32 <= smoke_max_tokens <= 64:
        raise ValueError("smoke_max_tokens must be between 32 and 64")
    if not 32 <= gateway_max_tokens <= 64:
        raise ValueError("gateway_max_tokens must be between 32 and 64")
    if not 128 <= special_max_tokens <= 256:
        raise ValueError("special_max_tokens must be between 128 and 256")
    metadata = dict(models or {})
    for provider in providers:
        registry_endpoint(provider)
    for provider in EXPLICIT_EMPTY_PROVIDERS:
        registry_endpoint(provider)

    provider_reports: list[dict[str, Any]] = []
    probe_passed: set[str] = set()
    provider_envs: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="opensquilla-live-provider-matrix-") as temp_dir:
        temp_root = Path(temp_dir)
        for provider in providers:
            spec = get_provider_spec(provider)
            model = _provider_model(provider, metadata)
            secret = secrets.get(spec.env_key, "")
            if spec.requires_api_key() and not secret:
                stages = [
                    _skipped_stage(stage, provider, model, "missing-credential")
                    for stage in STAGE_ORDER
                    if provider == "openai" or not stage.startswith("openai_responses")
                ]
                provider_reports.append(
                    _provider_result(provider=provider, model=model, stages=stages)
                )
                continue

            primary_env = child_environment(
                provider,
                secrets,
                base_environment=os.environ if base_environment is None else base_environment,
            )
            provider_envs[provider] = primary_env
            stages: list[dict[str, Any]] = []
            probe = _execute_stage(
                stage="probe",
                provider=provider,
                model=model,
                output=temp_root / f"{provider}-probe.json",
                env=primary_env,
                secrets=secrets,
                smoke_max_tokens=smoke_max_tokens,
                gateway_max_tokens=gateway_max_tokens,
                gateway_timeout_seconds=gateway_timeout_seconds,
                stage_timeout_seconds=stage_timeout_seconds,
            )
            stages.append(probe)
            if probe["status"] != "passed":
                for stage in STAGE_ORDER[1:]:
                    if provider != "openai" and stage.startswith("openai_responses"):
                        continue
                    stages.append(_skipped_stage(stage, provider, model, "blocked-by-probe"))
                provider_reports.append(
                    _provider_result(provider=provider, model=model, stages=stages)
                )
                continue
            probe_passed.add(provider)

            stages.append(
                _execute_stage(
                    stage="adapter_stream",
                    provider=provider,
                    model=model,
                    output=temp_root / f"{provider}-adapter-stream.json",
                    env=primary_env,
                    secrets=secrets,
                    smoke_max_tokens=smoke_max_tokens,
                    gateway_max_tokens=gateway_max_tokens,
                    gateway_timeout_seconds=gateway_timeout_seconds,
                    stage_timeout_seconds=stage_timeout_seconds,
                )
            )

            if provider == "openai":
                responses_env = child_environment(
                    "openai_responses",
                    secrets,
                    base_environment=os.environ if base_environment is None else base_environment,
                )
                responses_probe = _execute_stage(
                    stage="openai_responses_probe",
                    provider=provider,
                    model=model,
                    output=temp_root / "openai-responses-probe.json",
                    env=responses_env,
                    secrets=secrets,
                    smoke_max_tokens=smoke_max_tokens,
                    gateway_max_tokens=gateway_max_tokens,
                    gateway_timeout_seconds=gateway_timeout_seconds,
                    stage_timeout_seconds=stage_timeout_seconds,
                )
                stages.append(responses_probe)
                if responses_probe["status"] == "passed":
                    stages.append(
                        _execute_stage(
                            stage="openai_responses_stream",
                            provider=provider,
                            model=model,
                            output=temp_root / "openai-responses-stream.json",
                            env=responses_env,
                            secrets=secrets,
                            smoke_max_tokens=smoke_max_tokens,
                            gateway_max_tokens=gateway_max_tokens,
                            gateway_timeout_seconds=gateway_timeout_seconds,
                            stage_timeout_seconds=stage_timeout_seconds,
                        )
                    )
                else:
                    stages.append(
                        _skipped_stage(
                            "openai_responses_stream",
                            provider,
                            model,
                            "blocked-by-probe",
                        )
                    )

            stages.append(
                _execute_stage(
                    stage="gateway_main",
                    provider=provider,
                    model=model,
                    output=temp_root / f"{provider}-gateway-main.json",
                    env=primary_env,
                    secrets=secrets,
                    smoke_max_tokens=smoke_max_tokens,
                    gateway_max_tokens=gateway_max_tokens,
                    gateway_timeout_seconds=gateway_timeout_seconds,
                    stage_timeout_seconds=stage_timeout_seconds,
                )
            )
            provider_reports.append(_provider_result(provider=provider, model=model, stages=stages))

        (
            deep_providers,
            deep_rows,
            thinking_rows,
            vision_rows,
        ) = _deep_case_rows(
            probe_passed=probe_passed,
            provider_envs=provider_envs,
            metadata=metadata,
            temp_root=temp_root,
            secrets=secrets,
            smoke_max_tokens=smoke_max_tokens,
            gateway_max_tokens=gateway_max_tokens,
            gateway_timeout_seconds=gateway_timeout_seconds,
            stage_timeout_seconds=stage_timeout_seconds,
            special_max_tokens=special_max_tokens,
        )

    explicit_skips = [
        {
            "provider": provider,
            "model": _provider_model(provider, metadata),
            "status": "skipped",
            "failure_class": "missing-credential",
            "usage": {},
            "cost": {},
            "latency_ms": 0,
        }
        for provider in EXPLICIT_EMPTY_PROVIDERS
    ]
    deep_case_groups = (deep_rows, thinking_rows, vision_rows)
    deep_cases_ok = all(
        row["status"] == "passed"
        for group in deep_case_groups
        for row in group
    )
    full_inventory = tuple(providers) == DEFAULT_PROVIDERS
    thinking_stages = {str(row.get("stage") or "") for row in thinking_rows}
    deep_coverage_complete = (
        len(deep_providers) == len(DEEP_PROVIDER_PRIORITY)
        and bool(deep_rows)
        and {"thinking_off", "thinking_on"} <= thinking_stages
        and bool(vision_rows)
    )
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": bool(provider_reports)
        and all(result["status"] == "passed" for result in provider_reports)
        and deep_cases_ok
        and (deep_coverage_complete or not full_inventory),
        "stage_order": list(STAGE_ORDER),
        "providers": provider_reports,
        "explicit_empty_provider_inventory": explicit_skips,
        "deep_provider_selection": list(deep_providers),
        "deep_coverage_complete": deep_coverage_complete,
        "deep_multi_model": deep_rows,
        "thinking": thinking_rows,
        "vision": vision_rows,
    }
    safe_payload = sanitize_report(payload, secrets)
    if report_contains_secret(safe_payload, secrets):
        raise RuntimeError("refusing to return a report containing provider credentials")
    return safe_payload


def _gateway_child_report(
    provider: str,
    model: str,
    *,
    max_tokens: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run exactly one main-model gateway turn in an isolated child process."""

    from scripts.live_provider_profile_gateway_e2e import (  # noqa: PLC0415
        _forced_tier_overrides_for_slot,
        _run_gateway_case_batch,
    )

    spec = get_provider_spec(provider)
    api_key = os.environ.get(spec.env_key, "").strip()
    secrets = {spec.env_key: api_key} if api_key else {}
    started = time.perf_counter()
    if spec.requires_api_key() and not api_key:
        row = _skipped_stage("gateway_main", provider, model, "missing-credential")
        return {"ok": False, "results": [row]}, secrets

    tiers = {
        slot: {
            "provider": provider,
            "model": model,
            "supports_image": False,
            "image_only": False,
            # Connectivity/main-stream validation must not spend its bounded
            # 32-64 token budget on hidden reasoning.  Thinking is exercised
            # independently by the 128-256 token special stages below.
            "thinking_level": "off",
        }
        for slot in ("c0", "c1", "c2", "c3")
    }
    case = {
        "slot": "c1",
        "id": "main_model_baseline",
        "message": "Do not call tools. Reply with exactly this marker: {marker}",
    }
    try:
        batch = _run_gateway_case_batch(
            provider=provider,
            api_key=api_key,
            base_url=registry_endpoint(provider),
            tiers=tiers,
            cases=[case],
            max_tokens=min(max(max_tokens, 32), 64),
            timeout_seconds=timeout_seconds,
            case_mode="single_main_model_baseline",
            default_tier="c1",
            tier_overrides=_forced_tier_overrides_for_slot(tiers, "c1"),
            # The v4 controller can override a tier's thinking_level.  The
            # explicit [llm] setting has higher runtime precedence and makes
            # this low-cost baseline deterministically non-thinking.
            llm_thinking="off",
        )
        case_row = next(iter(batch.get("cases") or []), {})
        usage = _normalize_usage(case_row.get("usage"))
        actual_model = str(
            case_row.get("actual_response_model")
            or case_row.get("actual_request_model")
            or model
        )
        passed = bool(
            batch.get("ok") is True
            and case_row.get("ok") is True
            and _usage_nonzero(usage)
        )
        diagnostic = json.dumps(batch, ensure_ascii=False, sort_keys=True)
        result = {
            "stage": "gateway_main",
            "provider": provider,
            "model": actual_model,
            "status": "passed" if passed else "failed",
            "failure_class": None if passed else _failure_class_from_text(diagnostic),
            "usage": usage,
            "cost": _normalize_cost(case_row.get("cost")),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - classified, never serialized verbatim
        diagnostic = redact_text(f"{type(exc).__name__}: {exc}", secrets)
        result = {
            "stage": "gateway_main",
            "provider": provider,
            "model": model,
            "status": "failed",
            "failure_class": _failure_class_from_text(diagnostic),
            "usage": {},
            "cost": {},
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    return {"ok": result["status"] == "passed", "results": [result]}, secrets


async def _special_child_report(
    stage: str,
    provider: str,
    model: str,
    *,
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run one synthetic thinking or vision adapter case in an isolated child."""

    from opensquilla.engine.pricing import (  # noqa: PLC0415
        estimate_cost,
        resolve_model_price,
    )
    from opensquilla.provider.selector import (  # noqa: PLC0415
        ProviderConfig,
        _build_provider,
    )
    from opensquilla.provider.types import (  # noqa: PLC0415
        ChatConfig,
        ContentBlockImage,
        ContentBlockText,
        DoneEvent,
        ErrorEvent,
        Message,
        ReasoningDeltaEvent,
        TextDeltaEvent,
    )

    if stage not in _SPECIAL_STAGES:
        raise ValueError(f"unsupported special stage {stage!r}")
    if not 128 <= max_tokens <= 256:
        raise ValueError("special child max_tokens must be between 128 and 256")

    spec = get_provider_spec(provider)
    api_key = os.environ.get(spec.env_key, "").strip()
    secrets = {spec.env_key: api_key} if api_key else {}
    if spec.requires_api_key() and not api_key:
        row = _skipped_stage(stage, provider, model, "missing-credential")
        return {"ok": False, "results": [row]}, secrets

    marker = f"OPENSQUILLA_{stage.upper()}_OK"
    caps = _model_capabilities(provider, model)
    if stage == "vision_synthetic_color_block":
        content: Any = [
            ContentBlockText(
                text=(
                    "This is a generated test image. If it is a solid red square, "
                    f"reply exactly with: {marker}"
                )
            ),
            ContentBlockImage(
                source_type="base64",
                media_type="image/png",
                data=_synthetic_color_block_png_base64(),
            ),
        ]
        thinking = False
    else:
        content = f"Do not call tools. Reply exactly with: {marker}"
        thinking = stage == "thinking_on"

    provider_obj = _build_provider(
        ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=registry_endpoint(provider),
            replay_provider_state=False,
        )
    )
    chunks: list[str] = []
    done: DoneEvent | None = None
    error = ""
    reasoning_observed = False
    started = time.perf_counter()
    try:
        async for event in provider_obj.chat(
            [Message(role="user", content=content)],
            config=ChatConfig(
                max_tokens=max_tokens,
                temperature=None,
                thinking=thinking,
                thinking_budget_tokens=max_tokens if thinking else 0,
                # Provider-specific lower bounds (DashScope is the important
                # case) must not turn a 128-token portability test into a 400.
                # Omit an explicit budget and validate only the on/off toggle.
                thinking_budget_explicit=False,
                timeout=60.0,
                model_capabilities=caps,
            ),
        ):
            if isinstance(event, TextDeltaEvent):
                chunks.append(event.text)
            elif isinstance(event, ReasoningDeltaEvent):
                reasoning_observed = reasoning_observed or bool(event.text)
            elif isinstance(event, DoneEvent):
                done = event
            elif isinstance(event, ErrorEvent):
                error = event.message or event.code
                break
    except Exception as exc:  # noqa: BLE001 - classified and scrubbed by the child
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - started) * 1000)
    text_value = "".join(chunks)
    usage = (
        {
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "reasoning_tokens": done.reasoning_tokens,
            "cached_tokens": done.cached_tokens,
            "cache_write_tokens": done.cache_write_tokens,
        }
        if done is not None
        else {}
    )
    model_value = str(done.model or model) if done is not None else model
    marker_verified = marker in text_value
    reasoning_evidence = reasoning_observed or bool(
        done and (done.reasoning_content or done.reasoning_tokens)
    )
    toggle_verified = (
        True
        if stage == "vision_synthetic_color_block"
        else reasoning_evidence == (stage == "thinking_on")
    )
    passed = bool(
        done is not None
        and not error
        and marker_verified
        and toggle_verified
        and _usage_nonzero(usage)
    )
    resolved = resolve_model_price(model_value, provider)
    estimate_result = estimate_cost(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cached_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
        price=resolved.entry,
    )
    estimated_cost = estimate_result.cost_usd
    row = {
        "stage": stage,
        "provider": provider,
        "model": model_value,
        "status": "passed" if passed else "failed",
        "failure_class": None if passed else _failure_class_from_text(error or "content mismatch"),
        "usage": usage,
        "cost": {
            "opensquilla_estimated_cost_usd": estimated_cost,
            "cost_source": "opensquilla_static_estimate",
            "billing_scope": "static_estimate",
            "opensquilla_estimate": estimated_cost,
            "source": "opensquilla_static_estimate",
            "price_source": resolved.source,
            "estimate_basis": estimate_result.basis,
        },
        "latency_ms": latency_ms,
        # Evidence consumed by the parent, then stripped by _stage_summary.
        "done_event": done is not None,
        "marker_verified": marker_verified,
        "reasoning_observed": reasoning_evidence,
        "toggle_verified": toggle_verified,
        "error": redact_text(error, secrets),
    }
    return {"ok": passed, "results": [row]}, secrets


def _is_temp_report_path(path: Path) -> bool:
    return is_temporary_report_path(path)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child-gateway", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-special", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--special-stage", choices=sorted(_SPECIAL_STAGES), help=argparse.SUPPRESS)
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=DEFAULT_PROVIDERS,
        default=list(DEFAULT_PROVIDERS),
        help=(
            "restricted retry subset of the fixed provider inventory; "
            "defaults to all nine providers"
        ),
    )
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke-max-tokens", type=int, default=64)
    parser.add_argument("--gateway-max-tokens", type=int, default=64)
    parser.add_argument("--gateway-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--stage-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--special-max-tokens", type=int, default=256)
    args = parser.parse_args(argv)

    if not _is_temp_report_path(args.output):
        parser.error("--output must be inside the system temporary directory")
    if args.child_gateway:
        if not args.provider or not args.model:
            parser.error("--child-gateway requires --provider and --model")
    elif args.child_special:
        if not args.provider or not args.model or not args.special_stage:
            parser.error("--child-special requires --special-stage, --provider, and --model")
    else:
        if args.secrets_file is None:
            parser.error("--secrets-file is required")
        if len(args.providers) != len(set(args.providers)):
            parser.error("--providers must not contain duplicates")
        if args.output.resolve() == args.secrets_file.resolve():
            parser.error("--output must not overwrite --secrets-file")
        if not 32 <= args.smoke_max_tokens <= 64:
            parser.error("--smoke-max-tokens must be between 32 and 64")
        if not 32 <= args.gateway_max_tokens <= 64:
            parser.error("--gateway-max-tokens must be between 32 and 64")
        if not 128 <= args.special_max_tokens <= 256:
            parser.error("--special-max-tokens must be between 128 and 256")
    return args


def main() -> int:
    args = _parse_args()
    if args.child_gateway:
        assert args.provider and args.model
        try:
            payload, child_secrets = _gateway_child_report(
                args.provider,
                args.model,
                max_tokens=args.gateway_max_tokens,
                timeout_seconds=args.gateway_timeout_seconds,
            )
            safe_payload = write_safe_report(args.output, payload, child_secrets)
        except (OSError, RuntimeError, ValueError) as exc:
            args.output.unlink(missing_ok=True)
            known_secrets = {
                name: os.environ.get(name, "")
                for name in provider_secret_names()
                if os.environ.get(name)
            }
            print(
                redact_text(f"unable to run gateway live case: {exc}", known_secrets),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2))
        return 0 if safe_payload["ok"] else 1
    if args.child_special:
        assert args.provider and args.model and args.special_stage
        try:
            payload, child_secrets = asyncio.run(
                _special_child_report(
                    args.special_stage,
                    args.provider,
                    args.model,
                    max_tokens=args.special_max_tokens,
                )
            )
            safe_payload = write_safe_report(args.output, payload, child_secrets)
        except (OSError, RuntimeError, ValueError) as exc:
            args.output.unlink(missing_ok=True)
            known_secrets = {
                name: os.environ.get(name, "")
                for name in provider_secret_names()
                if os.environ.get(name)
            }
            print(
                redact_text(
                    f"unable to run special live case: {exc}",
                    known_secrets,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2))
        return 0 if safe_payload["ok"] else 1

    assert args.secrets_file is not None

    try:
        inventory = parse_provider_keys_file(args.secrets_file)
        payload = run_matrix(
            providers=list(args.providers),
            secrets=inventory.secrets,
            models=inventory.models,
            smoke_max_tokens=args.smoke_max_tokens,
            gateway_max_tokens=args.gateway_max_tokens,
            gateway_timeout_seconds=args.gateway_timeout_seconds,
            stage_timeout_seconds=args.stage_timeout_seconds,
            special_max_tokens=args.special_max_tokens,
        )
        _emit_main_diagnostics(
            payload,
            file_mode=inventory.file_mode,
            permission_warning=inventory.permission_warning,
            ignored_line_numbers=inventory.ignored_line_numbers,
            secrets=inventory.secrets,
        )
        public_payload = _public_report_rows(payload)
        safe_payload = write_safe_report(args.output, public_payload, inventory.secrets)
        _assert_public_report_schema(safe_payload)
    except (OSError, RuntimeError, ValueError) as exc:
        args.output.unlink(missing_ok=True)
        known_secrets = inventory.secrets if "inventory" in locals() else {}
        print(
            redact_text(f"unable to run live matrix: {exc}", known_secrets),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(safe_payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
