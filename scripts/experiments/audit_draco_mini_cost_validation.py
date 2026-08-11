#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_integrity import (
    RESULT_EVIDENCE_SCHEMA,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.eval.draco_resume_source_index import (
    DracoResumeSourceError,
    ResumeSourceIndex,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    selection_plan_reference_signal,
    selection_plan_row_capability_signal,
)

EXPECTED_MODELS = (
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "qwen/qwen3.7-max",
)

EXPECTED_PROPOSERS = list(EXPECTED_MODELS)
EXPECTED_AGGREGATOR = "z-ai/glm-5.2"
EXPECTED_JUDGE = "google/gemini-3.1-pro-preview"
EXPECTED_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EXPECTED_MODEL_REVISIONS = {
    "deepseek/deepseek-v4-pro": "deepseek/deepseek-v4-pro-20260423",
    "z-ai/glm-5.2": "z-ai/glm-5.2-20260616",
    "moonshotai/kimi-k2.7-code": "moonshotai/kimi-k2.7-code-20260612",
    "qwen/qwen3.7-max": "qwen/qwen3.7-max-20260520",
    EXPECTED_JUDGE: "google/gemini-3.1-pro-preview-20260219",
}
EXPECTED_ROLE_MODEL_PAIRS = Counter(
    {
        ("proposer", EXPECTED_MODELS[0]): 1,
        ("proposer", EXPECTED_MODELS[1]): 1,
        ("proposer", EXPECTED_MODELS[2]): 1,
        ("proposer", EXPECTED_MODELS[3]): 1,
        ("aggregator", EXPECTED_AGGREGATOR): 1,
    }
)
EXPECTED_PROVIDER_ROUTING = {
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    EXPECTED_JUDGE: "google-ai-studio",
}
EXPECTED_PROVIDER_DISPLAY_NAMES = {
    "deepseek": "DeepSeek",
    "z-ai": "Z.AI",
    "moonshotai": "Moonshot AI",
    "alibaba": "Alibaba",
    "google-ai-studio": "Google AI Studio",
}
EXPECTED_BLOCKED_DOMAINS = [
    "hf.co",
    "huggingface.co",
    "datasets-server.huggingface.co",
    "github.com",
    "raw.githubusercontent.com",
    "openrouter.ai",
    "perplexity.ai",
    "research.perplexity.ai",
]
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
STANDARD_RESULT_NAME_PATTERN = re.compile(
    r"^draco_ensemble_(?P<stamp>[0-9]{8}-[0-9]{6})\.jsonl$"
)
MAX_BOUND_MANIFEST_BYTES = 64 * 1024 * 1024
BOUND_FILE_SIGNATURE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def canonical_frozen_model(
    actual_model: Any,
    expected_models: Iterable[str],
    *,
    allow_requested_base: bool = False,
) -> str | None:
    """Map only the frozen OpenRouter revision back to its requested alias.

    Formal artifacts must not silently accept a future date-suffixed revision.
    ``allow_requested_base`` exists only for request-side/static validation;
    response-side validation uses the exact revision map above.
    """

    actual = str(actual_model or "").strip()
    for expected in expected_models:
        if actual == EXPECTED_MODEL_REVISIONS.get(expected):
            return expected
        if allow_requested_base and actual == expected:
            return expected
    return None


def validate_router_attestation(
    call: dict[str, Any],
    *,
    expected_model: str,
    label: str,
    issues: list[str],
    expected_cache_namespace_sha256: str = "",
) -> None:
    """Require per-request OpenRouter evidence for the frozen provider route."""

    expected_slug = EXPECTED_PROVIDER_ROUTING[expected_model]
    expected_provider_name = EXPECTED_PROVIDER_DISPLAY_NAMES[expected_slug]
    provider_usage = call.get("provider_usage")
    if not isinstance(provider_usage, dict):
        issues.append(f"{label}: missing provider_usage evidence")
        return
    if provider_usage.get("is_byok") is not False:
        issues.append(f"{label}: raw usage does not prove is_byok=false")
    actual_cache_namespace = str(provider_usage.get("cache_namespace_sha256") or "")
    if actual_cache_namespace != expected_cache_namespace_sha256:
        issues.append(f"{label}: cache namespace evidence differs from runtime contract")
    response_ids = provider_usage.get("response_ids")
    if (
        not isinstance(response_ids, list)
        or not response_ids
        or any(not isinstance(item, str) or not item for item in response_ids)
    ):
        issues.append(f"{label}: missing OpenRouter response id evidence")
    metadata = provider_usage.get("router_metadata")
    if not isinstance(metadata, dict) or not metadata:
        issues.append(f"{label}: missing OpenRouter router metadata")
        return
    if metadata.get("requested") != expected_model:
        issues.append(f"{label}: router requested model differs from {expected_model}")
    if metadata.get("strategy") != "direct":
        issues.append(f"{label}: router strategy is not direct")
    if metadata.get("attempt") != 1 or isinstance(metadata.get("attempt"), bool):
        issues.append(f"{label}: router did not succeed on exactly attempt 1")
    if metadata.get("is_byok") is not False:
        issues.append(f"{label}: router metadata does not prove is_byok=false")
    endpoints = metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    selected = (
        [item for item in available if isinstance(item, dict) and item.get("selected") is True]
        if isinstance(available, list)
        else []
    )
    if len(selected) != 1:
        issues.append(f"{label}: router metadata must name exactly one selected endpoint")
    else:
        selected_endpoint = selected[0]
        if selected_endpoint.get("provider") != expected_provider_name:
            issues.append(f"{label}: selected provider is not {expected_provider_name}")
        if (
            canonical_frozen_model(selected_endpoint.get("model"), (expected_model,))
            != expected_model
        ):
            issues.append(f"{label}: selected endpoint model differs from request")
    attempts = metadata.get("attempts")
    if isinstance(attempts, list) and attempts:
        if len(attempts) != 1:
            issues.append(f"{label}: router metadata records fallback attempts")
        else:
            attempt = attempts[0]
            if (
                not isinstance(attempt, dict)
                or attempt.get("provider") != expected_provider_name
                or attempt.get("status") != 200
                or canonical_frozen_model(attempt.get("model"), (expected_model,)) != expected_model
            ):
                issues.append(f"{label}: router attempt evidence differs from frozen route")
    pipeline = metadata.get("pipeline")
    if pipeline not in (None, []):
        issues.append(f"{label}: OpenRouter pipeline modified the benchmark request")


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def decimal_number(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def required_decimal(value: Any, *, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} is missing or non-numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


def canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def normalize_expected_draco_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the runner's ``load_tasks`` normalization for input binding."""

    normalized = dict(payload)
    task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
    prompt = str(payload.get("prompt") or payload.get("problem") or "").strip()
    normalized["id"] = task_id
    normalized["prompt"] = prompt
    rubric_source = None
    if "rubric" in normalized:
        rubric_source = normalized["rubric"]
    elif "answer" in normalized:
        rubric_source = normalized["answer"]
    if rubric_source is not None:
        rubric = rubric_source
        if isinstance(rubric_source, str) and rubric_source.strip():
            try:
                rubric = json.loads(rubric_source.strip())
            except json.JSONDecodeError:
                pass
        normalized["rubric"] = rubric
    return normalized


def capture_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def manifest_preflight_counts(manifest: Any) -> dict[str, int]:
    tool_policy = manifest.get("tool_policy") if isinstance(manifest, dict) else None
    local_tools = tool_policy.get("local_web_tools") if isinstance(tool_policy, dict) else None
    preflight = local_tools.get("preflight") if isinstance(local_tools, dict) else None
    calls = preflight.get("preflight_calls") if isinstance(preflight, dict) else None
    if not isinstance(calls, dict):
        return {}
    result: dict[str, int] = {}
    for tool_name in ("web_search", "web_fetch"):
        value = calls.get(tool_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return {}
        result[tool_name] = value
    return result


def normalized_cross_run_contract(value: Any) -> Any:
    """Remove only the intentional canary/full isolation differences."""

    if not isinstance(value, dict):
        return value
    normalized = json.loads(json.dumps(value, ensure_ascii=False))
    experiment_config = normalized.get("experiment_config")
    if isinstance(experiment_config, dict):
        experiment_config.pop("benchmark_input", None)
    runtime = normalized.get("resolved_llm_runtime")
    if isinstance(runtime, dict):
        for key in (
            "cache_namespace_enabled",
            "cache_namespace_required",
            "cache_namespace_sha256",
        ):
            runtime.pop(key, None)
    tools = normalized.get("tools")
    if isinstance(tools, dict):
        local = tools.get("local_web_tools")
        if isinstance(local, dict):
            local.pop("preflight", None)
    return normalized


def manifest_tool_runtime_issues(manifest: Any) -> list[str]:
    issues: list[str] = []
    policy = manifest.get("tool_policy") if isinstance(manifest, dict) else None
    if not isinstance(policy, dict):
        return ["missing manifest tool_policy"]
    if policy.get("tool_mode") != "local_web_tools":
        issues.append("tool_mode is not local_web_tools")
    if policy.get("tools_enabled") is not True:
        issues.append("local web tools are not enabled")
    if policy.get("tool_names") != ["web_search", "web_fetch"]:
        issues.append("tool_names differ from web_search/web_fetch")
    if policy.get("contamination_blocked_domains") != EXPECTED_BLOCKED_DOMAINS:
        issues.append("contamination blocked domains differ from frozen policy")
    if "execution_security" in policy:
        issues.append("unexpected benchmark execution_security override")
    local = policy.get("local_web_tools")
    if not isinstance(local, dict):
        return [*issues, "missing local_web_tools policy"]
    search = local.get("web_search")
    if not isinstance(search, dict) or any(
        (
            search.get("provider") != "brave",
            search.get("api_key_env") != "BRAVE_SEARCH_API_KEY",
            search.get("max_results") != 5,
            search.get("excluded_domains") != EXPECTED_BLOCKED_DOMAINS,
        )
    ):
        issues.append("web_search configuration differs from frozen Brave policy")
    fetch = local.get("web_fetch")
    if not isinstance(fetch, dict) or any(
        (
            fetch.get("blocked_domains") != EXPECTED_BLOCKED_DOMAINS,
            fetch.get("max_content_tokens") != 50_000,
            fetch.get("max_content_chars") != 200_000,
            fetch.get("allow_firecrawl") is not False,
        )
    ):
        issues.append("web_fetch configuration differs from frozen local policy")
    search_runtime = local.get("search_runtime")
    expected_search_runtime = {
        "configured_provider": "brave",
        "provider": "brave",
        "max_results": 5,
        "api_key_configured": True,
        "api_key_source": "env:BRAVE_SEARCH_API_KEY",
        "api_key_env": "BRAVE_SEARCH_API_KEY",
        "credential_status": "configured",
        "runtime_configured": True,
        "proxy_configured": False,
        "use_env_proxy": False,
        "fallback_policy": "off",
        "diagnostics": False,
    }
    if search_runtime != expected_search_runtime:
        issues.append("Brave runtime is not direct, configured, and fail-closed")
    sandbox_runtime = local.get("sandbox_runtime")
    if not isinstance(sandbox_runtime, dict):
        issues.append("missing sandbox runtime attestation")
    else:
        effective = sandbox_runtime.get("effective")
        resolved_backend = str(sandbox_runtime.get("backend") or "")
        if (
            sandbox_runtime.get("configured") is not True
            or resolved_backend in {"", "host"}
            or sandbox_runtime.get("approval_queue") != "auto_deny_unattended"
            or not isinstance(effective, dict)
            or not {
                "sandbox_enabled",
                "grading_enabled",
                "insecure_mode",
            }.issubset(effective)
        ):
            issues.append("web tools did not use the configured runtime")
    fetch_runtime = local.get("fetch_runtime")
    if not isinstance(fetch_runtime, dict) or any(
        (
            fetch_runtime.get("extractor_mode") != "auto_local_first",
            fetch_runtime.get("firecrawl_allowed") is not False,
            fetch_runtime.get("firecrawl_api_key_active") is not False,
            fetch_runtime.get("external_fetch_cost_tracking") != "not_applicable",
        )
    ):
        issues.append("web_fetch runtime can use an untracked external extractor")
    preflight = local.get("preflight")
    if not isinstance(preflight, dict) or preflight.get("status") != "passed":
        issues.append("local web tool preflight did not pass")
    group_policy = policy.get("group_tool_policies")
    if not isinstance(group_policy, dict) or group_policy.get("B2") != {
        key: value for key, value in policy.items() if key != "group_tool_policies"
    }:
        issues.append("B2 group tool policy differs from the manifest runtime policy")
    return issues


def iter_usage_calls(usage: Any, *, phase: str) -> Iterable[dict[str, Any]]:
    if not isinstance(usage, dict):
        return
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list) and breakdown:
        for item in breakdown:
            if isinstance(item, dict):
                yield {**item, "phase": phase}
        return
    if usage.get("model"):
        yield {
            "phase": phase,
            "role": phase,
            "provider": "openrouter",
            **usage,
        }


def generation_usages(row: dict[str, Any]) -> Iterable[dict[str, Any]]:
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
    emitted = False
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            usage = run.get("usage") if isinstance(run, dict) else None
            if isinstance(usage, dict):
                emitted = True
                yield usage
    if not emitted and isinstance(row.get("usage"), dict):
        yield row["usage"]


def iter_judge_runs(judge: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(judge, dict):
        return
    judgments = judge.get("criterion_judgments")
    if not isinstance(judgments, list):
        return
    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        attempts = judgment.get("judge_attempts")
        if isinstance(attempts, list) and attempts:
            for attempt in attempts:
                run = attempt.get("run") if isinstance(attempt, dict) else None
                if isinstance(run, dict):
                    yield run
        else:
            run = judgment.get("judge_run")
            if isinstance(run, dict):
                yield run


def judge_usages(judge: Any) -> Iterable[dict[str, Any]]:
    for run in iter_judge_runs(judge):
        usage = run.get("usage")
        if isinstance(usage, dict):
            yield usage


def aggregate(calls: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "billable_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "billed_cost_usd": 0.0,
            "positive_cost_calls": 0,
            "zero_cost_calls": 0,
            "nonbillable_zero_usage_calls": 0,
            "unbilled_usage_calls": 0,
            "anomalous_zero_cost_calls": 0,
            "positive_provider_billed_calls": 0,
            "cost_sources": Counter(),
            "roles": Counter(),
        }
    )
    for call in calls:
        model = str(call.get("model") or "<unknown>")
        row = stats[model]
        row["calls"] += 1
        token_values = {
            key: int(number(call.get(key)))
            for key in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "cache_write_tokens",
            )
        }
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens"):
            row[key] += token_values[key]
        cost = number(call.get("billed_cost"))
        cost_source = str(call.get("cost_source") or "none")
        has_usage = any(value > 0 for value in token_values.values())
        nonbillable_placeholder = cost <= 0 and not has_usage and cost_source == "none"
        row["billed_cost_usd"] += cost
        row["positive_cost_calls"] += int(cost > 0)
        row["zero_cost_calls"] += int(cost <= 0)
        row["billable_calls"] += int(not nonbillable_placeholder)
        row["nonbillable_zero_usage_calls"] += int(nonbillable_placeholder)
        row["unbilled_usage_calls"] += int(cost <= 0 and has_usage)
        row["anomalous_zero_cost_calls"] += int(cost <= 0 and not nonbillable_placeholder)
        row["positive_provider_billed_calls"] += int(cost > 0 and cost_source == "provider_billed")
        row["cost_sources"][cost_source] += 1
        row["roles"][str(call.get("role") or call.get("phase") or "unknown")] += 1
    clean: dict[str, dict[str, Any]] = {}
    for model, row in sorted(stats.items()):
        clean[model] = {
            **row,
            "billed_cost_usd": round(row["billed_cost_usd"], 9),
            "cost_sources": dict(sorted(row["cost_sources"].items())),
            "roles": dict(sorted(row["roles"].items())),
        }
    return clean


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"JSONL row on line {line_number} must be an object")
            rows.append(value)
    return rows


def _standard_compact_artifact_paths(result_jsonl: Path) -> dict[str, Path] | None:
    match = STANDARD_RESULT_NAME_PATTERN.fullmatch(result_jsonl.name)
    if match is None:
        return None
    stamp = match.group("stamp")
    parent = Path(os.path.abspath(result_jsonl.parent))
    return {
        "results_jsonl": parent / f"draco_ensemble_{stamp}.jsonl",
        "trace_jsonl": parent / f"draco_run_{stamp}.trace.jsonl",
        "manifest_json": parent / f"draco_run_{stamp}.manifest.json",
        SELECTION_PLAN_PACK_ARTIFACT_FIELD: (
            parent / f"draco_run_{stamp}.selection-plan.pack.jsonl"
        ),
    }


def _manifest_declares_compact_evidence(manifest: dict[str, Any]) -> bool:
    artifacts = manifest.get("artifacts")
    return bool(
        SELECTION_PLAN_EVIDENCE_ROW_FIELD in manifest
        or SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD in manifest
        or (
            isinstance(artifacts, dict)
            and SELECTION_PLAN_PACK_ARTIFACT_FIELD in artifacts
        )
    )


def _parse_indexed_result_row(payload: bytes, *, line_number: int) -> dict[str, Any]:
    text = payload.decode("utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON on line {line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSONL row on line {line_number} must be an object")
    return value


def _bound_file_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(file_stat, field)) for field in BOUND_FILE_SIGNATURE_FIELDS)


def _open_bound_regular(
    path: Path,
    *,
    label: str,
    directory_fd: int | None = None,
    nofollow: bool = True,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = (
            os.open(path.name, flags, dir_fd=directory_fd)
            if directory_fd is not None
            else os.open(path, flags)
        )
    except OSError as exc:
        raise SystemExit(f"Cannot open bound {label}: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SystemExit(f"Bound {label} is not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


class _BoundDirectorySnapshot:
    """Pin the directory used to discover one stamped artifact family."""

    def __init__(self, path: Path, *, nofollow: bool = True) -> None:
        self.path = Path(os.path.abspath(path))
        self.nofollow = nofollow
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if nofollow:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd: int | None = os.open(self.path, flags)
        except OSError as exc:
            raise SystemExit(f"Cannot open bound artifact directory: {self.path}") from exc
        self._signature = _bound_file_signature(os.fstat(self._fd))

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise SystemExit("Bound artifact directory is closed")
        return self._fd

    def contains(self, path: Path) -> bool:
        return Path(os.path.abspath(path.parent)) == self.path

    def entry_exists(self, path: Path) -> bool:
        if not self.contains(path):
            return False
        try:
            os.stat(path.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def verify_path(self) -> None:
        if self._fd is None:
            raise SystemExit("Bound artifact directory is closed")
        if _bound_file_signature(os.fstat(self._fd)) != self._signature:
            raise SystemExit("Bound artifact directory changed during audit")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if self.nofollow:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            path_fd = os.open(self.path, flags)
        except OSError as exc:
            raise SystemExit("Bound artifact directory path changed during audit") from exc
        try:
            if _bound_file_signature(os.fstat(path_fd)) != self._signature:
                raise SystemExit("Bound artifact directory path changed during audit")
        finally:
            os.close(path_fd)

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> _BoundDirectorySnapshot:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            if exc_type is None:
                self.verify_path()
        finally:
            self.close()


class _BoundFileSnapshot:
    """Read one regular file from a pinned directory and retain its identity."""

    def __init__(
        self,
        path: Path,
        *,
        label: str,
        directory: _BoundDirectorySnapshot | None = None,
        nofollow: bool = True,
    ) -> None:
        self.path = Path(os.path.abspath(path))
        self.label = label
        if directory is not None and not directory.contains(self.path):
            raise SystemExit(f"Bound {label} is outside its pinned directory")
        self._fd: int | None = _open_bound_regular(
            self.path,
            label=label,
            directory_fd=directory.fd if directory is not None else None,
            nofollow=nofollow,
        )
        self.nofollow = nofollow
        self._signature = _bound_file_signature(os.fstat(self._fd))

    @property
    def signature(self) -> tuple[int, ...]:
        return self._signature

    def load_rows(self) -> list[dict[str, Any]]:
        if self._fd is None:
            raise SystemExit(f"Bound {self.label} snapshot is closed")
        rows: list[dict[str, Any]] = []
        with os.fdopen(os.dup(self._fd), encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSON on line {line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise SystemExit(f"JSONL row on line {line_number} must be an object")
                rows.append(value)
        self.verify_inode()
        return rows

    def load_json_object(self) -> tuple[dict[str, Any], str]:
        if self._fd is None:
            raise SystemExit(f"Bound {self.label} snapshot is closed")
        size = int(os.fstat(self._fd).st_size)
        if size <= 0 or size > MAX_BOUND_MANIFEST_BYTES:
            raise SystemExit(f"Bound {self.label} is outside its byte limit")
        payload = bytearray(size)
        completed = 0
        while completed < size:
            chunk = os.pread(self._fd, size - completed, completed)
            if not chunk:
                raise SystemExit(f"Bound {self.label} ended before its declared size")
            payload[completed : completed + len(chunk)] = chunk
            completed += len(chunk)
        self.verify_inode()
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Bound {self.label} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise SystemExit(f"Bound {self.label} must contain a JSON object")
        return value, hashlib.sha256(payload).hexdigest()

    def verify_inode(self) -> None:
        if self._fd is None or _bound_file_signature(os.fstat(self._fd)) != self._signature:
            raise SystemExit(f"Bound {self.label} changed during audit")

    def verify_path(self) -> None:
        self.verify_inode()
        path_fd = _open_bound_regular(
            self.path,
            label=self.label,
            nofollow=self.nofollow,
        )
        try:
            if _bound_file_signature(os.fstat(path_fd)) != self._signature:
                raise SystemExit(f"Bound {self.label} path changed during audit")
        finally:
            os.close(path_fd)

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> _BoundFileSnapshot:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            if exc_type is None:
                self.verify_path()
        finally:
            self.close()


def load_audit_artifact_rows(
    result_jsonl: Path,
    *,
    trace_jsonl: Path,
    manifest_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path | None, dict[str, Any] | None]:
    """Load one audit input, strictly binding compact artifacts when signalled.

    Legacy JSONL keeps the historical loader and optional-manifest behavior.
    Compact rows are re-read through ``ResumeSourceIndex`` so its existing
    terminal bundle verifier authenticates the durable trio, selection-plan
    pack, and manifest binding without expanding any selection plan.  The
    trace and manifest used by this audit are read before the index's final
    snapshot verification, so a pathname replacement cannot swap in an
    unauthenticated audit input.
    """

    expected = _standard_compact_artifact_paths(result_jsonl)
    absolute_result = Path(os.path.abspath(result_jsonl))
    absolute_trace = Path(os.path.abspath(trace_jsonl))
    with ExitStack() as snapshots:
        result_directory = snapshots.enter_context(
            _BoundDirectorySnapshot(absolute_result.parent, nofollow=False)
        )
        result_snapshot = snapshots.enter_context(
            _BoundFileSnapshot(
                absolute_result,
                label="result JSONL",
                directory=result_directory,
                nofollow=False,
            )
        )
        rows = result_snapshot.load_rows()
        compact_row_signal = any(
            selection_plan_row_capability_signal(row)
            or selection_plan_reference_signal(row)
            for row in rows
        )
        pack_present = bool(
            expected is not None
            and result_directory.entry_exists(
                expected[SELECTION_PLAN_PACK_ARTIFACT_FIELD]
            )
        )
        manifest_snapshot: _BoundFileSnapshot | None = None
        manifest: dict[str, Any] | None = None
        manifest_sha256 = ""
        manifest_error: SystemExit | None = None
        if expected is not None and result_directory.entry_exists(expected["manifest_json"]):
            try:
                manifest_snapshot = snapshots.enter_context(
                    _BoundFileSnapshot(
                        expected["manifest_json"],
                        label="compact DRACO manifest",
                        directory=result_directory,
                        nofollow=False,
                    )
                )
                manifest, manifest_sha256 = manifest_snapshot.load_json_object()
            except SystemExit as exc:
                manifest_error = exc
        compact_candidate = bool(
            compact_row_signal
            or pack_present
            or (
                manifest is not None
                and _manifest_declares_compact_evidence(manifest)
            )
        )
        trace_snapshot = snapshots.enter_context(
            _BoundFileSnapshot(
                absolute_trace,
                label="trace JSONL",
                directory=(
                    result_directory
                    if result_directory.contains(absolute_trace)
                    else None
                ),
                nofollow=False,
            )
        )
        trace_rows = trace_snapshot.load_rows()
        if not compact_candidate:
            return rows, trace_rows, manifest_path, None

        if expected is None or absolute_result != expected["results_jsonl"]:
            raise SystemExit(
                "Compact selection-plan evidence requires a standard stamped result path"
            )
        if absolute_trace != expected["trace_jsonl"]:
            raise SystemExit(
                "Compact selection-plan evidence requires its bound standard trace sibling"
            )
        if manifest_path is not None and (
            Path(os.path.abspath(manifest_path)) != expected["manifest_json"]
        ):
            raise SystemExit(
                "Compact selection-plan evidence requires its bound standard manifest sibling"
            )
        if manifest_snapshot is None or manifest is None:
            detail = str(manifest_error) if manifest_error is not None else "manifest is missing"
            raise SystemExit(f"Invalid compact DRACO artifact bundle: {detail}")

        bound_manifest_path = expected["manifest_json"]
        try:
            with ResumeSourceIndex([result_jsonl], force_spool=False) as source_index:
                with source_index.open_source(result_jsonl, source_index=0) as indexed_rows:
                    for _indexed in indexed_rows:
                        pass
                source_snapshots = getattr(source_index, "_sources", None)
                source_snapshot = (
                    source_snapshots.get(0)
                    if isinstance(source_snapshots, dict)
                    else None
                )
                compact_bundle = getattr(source_snapshot, "compact_bundle", None)
                if compact_bundle is None:
                    raise SystemExit(
                        "Compact DRACO audit lost its authenticated bundle snapshot"
                    )
                if (
                    manifest_snapshot.signature != compact_bundle.manifest_signature
                    or manifest_sha256 != compact_bundle.manifest_sha256
                    or result_snapshot.signature
                    != compact_bundle.artifact_path_snapshots.get("results_jsonl")
                    or trace_snapshot.signature
                    != compact_bundle.artifact_path_snapshots.get("trace_jsonl")
                ):
                    raise SystemExit(
                        "Compact DRACO audit inputs differ from the authenticated bundle snapshot"
                    )
                source_index.seal()
        except DracoResumeSourceError as exc:
            raise SystemExit(f"Invalid compact DRACO artifact bundle: {exc}") from exc

        return rows, trace_rows, bound_manifest_path, manifest


def markdown(report: dict[str, Any]) -> str:
    config = report.get("configuration_summary") or {}
    runner = config.get("runner") or {}
    judge = config.get("judge") or {}
    proposer_names = ", ".join(config.get("proposers") or EXPECTED_PROPOSERS)
    lines = [
        "# DRACO Ensemble Summary — B2 新 Key 完整配置重跑",
        "",
        f"Raw JSONL: `{report['source_jsonl']}`",
        f"Trace JSONL: `{report.get('trace_jsonl')}`",
        f"Effective config: `{report['effective_config']}`",
        f"Strict audit: `{report.get('output_json', 'cost-audit.json')}`",
        "",
        "## 1. 实验结论",
        "",
        f"- 严格审计：**{'通过' if report['pass'] else '未通过'}**",
        "- 实验组：`B2`",
        f"- 任务覆盖：{report['unique_tasks']}/{report['expected_tasks']}",
        f"- 成功任务：{report['completed_tasks']}/{report['expected_tasks']}",
        f"- 重复任务：{report['duplicate_tasks']}",
        f"- 未知用量记录：{report['usage_unknown_count']}",
        (
            f"- {report['expected_tasks']} 题 benchmark OpenRouter LLM + Judge 精确成本："
            f"${report['total_billed_cost_usd']:.9f}"
        ),
        "- Brave/web_fetch 等外部工具美元成本：**未知，未混入 OpenRouter 成本**",
        "- 全供应商总成本是否精确：**否**",
        "",
        "## 2. 实验配置",
        "",
        f"- 任务集：DRACO mini（本次 {report['expected_tasks']} 题）",
        "- 运行组：B2（4 proposer + 1 aggregator）",
        f"- Proposer：{proposer_names}",
        f"- Aggregator：{config.get('aggregator') or EXPECTED_AGGREGATOR}",
        f"- Judge：{judge.get('model') or EXPECTED_JUDGE}（repeats={judge.get('repeats')}）",
        f"- 工具模式：`{config.get('tool_mode')}`",
        f"- Runner：`{runner.get('mode')}`",
        f"- Agent 最大迭代：{runner.get('agent_max_iterations')}",
        f"- 并发：{runner.get('concurrency')}",
        "",
        "## 3. 实验成绩",
        "",
    ]
    metrics = report.get("metrics")
    if metrics:
        lines.extend(
            [
                (
                    "| Group | Rows | Done | Avg Quality | AvgQ Scored | Avg Pass | "
                    "Judge Err | Avg Gen $ | Avg Visible | Avg Reason | Avg Tokens | "
                    "Avg Tools | Tool % | Avg Steps | Avg LLM Req | p50 ms | p95 ms |"
                ),
                (
                    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
                    "---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
                ),
                (
                    f"| B2 | {metrics['rows']} | {metrics['completed']} | "
                    f"{metrics['avg_quality']:.2f} | {metrics['avg_quality_scored']:.2f} | "
                    f"{metrics['avg_pass_rate']:.2f}% | {metrics['judge_errors']} | "
                    f"{metrics['avg_cost_usd']:.9f} | {metrics['avg_visible_tokens']:.1f} | "
                    f"{metrics['avg_reasoning_tokens']:.1f} | {metrics['avg_total_tokens']:.1f} | "
                    f"{metrics['avg_tool_calls']:.1f} | {metrics['tool_call_rate_pct']:.1f}% | "
                    f"{metrics['avg_trajectory_steps']:.1f} | {metrics['avg_llm_requests']:.1f} | "
                    f"{metrics['latency_p50_ms']:.0f} | {metrics['latency_p95_ms']:.0f} |"
                ),
                "",
                f"- 评分覆盖率：{metrics['score_coverage_pct']:.2f}%",
                f"- 生成延迟 P50/P95：{metrics['latency_p50_ms'] / 1000:.2f} / "
                f"{metrics['latency_p95_ms'] / 1000:.2f} 秒",
            ]
        )
    else:
        lines.append("未提供汇总指标文件。")
    lines.extend(
        [
            "",
            "## 4. 成本汇总",
            "",
            f"- 生成阶段实付成本：${report['generation_billed_cost_usd']:.9f}",
            (
                "- 平均每题生成成本（不含 Judge）："
                f"${report['average_generation_cost_per_task_usd']:.9f}"
            ),
            f"- Judge 实付成本：${report['judge_billed_cost_usd']:.9f}",
            f"- 平均每题 Judge 成本：${report['average_judge_cost_per_task_usd']:.9f}",
            f"- 平均每题 OpenRouter 成本：${report['average_total_cost_per_task_usd']:.9f}",
            f"- Judge 成本占比：{report['judge_cost_share_pct']:.2f}%",
            f"- 任务内观察到 web_search：{report['observed_web_search_invocations']} 次",
            f"- 任务内观察到 web_fetch：{report['observed_web_fetch_invocations']} 次",
            (
                "- 未定价外部工具调用保守上界："
                f"{report['external_unpriced_tool_call_count_upper_bound']} 次"
            ),
            f"- 本次命令外部工具 preflight：{report['external_preflight_call_count']} 次",
            "- 外部工具 USD：未知；全供应商总成本仅给出 OpenRouter 下界，不声明精确值。",
        ]
    )
    if report.get("account_usage_delta_usd") is not None:
        lines.extend(
            [
                (
                    f"- {report['expected_tasks']} 题 benchmark OpenRouter 账户用量增量："
                    f"${report['account_usage_delta_usd']:.9f}"
                ),
                f"- 账单对账差额：${report['account_cost_difference_usd']:.9f}",
                f"- OpenRouter BYOK 用量增量：${report['account_byok_usage_delta_usd']:.9f}",
            ]
        )
    if report.get("validation_account_usage_delta_usd") is not None:
        lines.extend(
            [
                f"- non-BYOK canary 验证成本：${report['validation_account_usage_delta_usd']:.9f}",
                f"- launcher 总 OpenRouter 用量（canary + benchmark）："
                f"${report['launcher_account_usage_delta_usd']:.9f}",
                (
                    "- canary 是启动前验证开销，不计入 "
                    f"{report['expected_tasks']} 题 benchmark 的平均每题成本。"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## 5. Agent Loop 执行证据",
            "",
            (
                f"- 每轮结构：{report['ensemble_calls_per_iteration']} 次模型调用"
                "（4 proposer + 1 aggregator）"
            ),
            (
                f"- 总生成模型请求：{report['generation_call_count']} 次；平均每题："
                f"{report['average_llm_requests_per_task']:.1f} 次"
            ),
            f"- 平均 ensemble 轮数：{report['average_ensemble_iterations_per_task']:.1f} 轮/题",
            (
                "- 进入第 2 轮及以上的任务："
                f"{report['multi_iteration_tasks']}/{report['expected_tasks']}"
            ),
            (
                f"- 单题轮数范围：{report['min_ensemble_iterations']}–"
                f"{report['max_ensemble_iterations']} 轮"
            ),
            (
                f"- 工具调用：总计 {report['total_tool_calls']} 次，平均 "
                f"{report['average_tool_calls_per_task']:.1f} 次/题"
            ),
            "",
            (
                "| Task ID | LLM 请求 | Ensemble 轮数 | 工具调用 | 质量分 | "
                "生成成本（不含 Judge） | 总耗时（秒） |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for task in report["task_summaries"]:
        lines.append(
            f"| `{task['task_id']}` | {task['llm_request_count']} | "
            f"{task['ensemble_iterations']:.0f} | {task['tool_call_count']} | "
            f"{task['quality_total']:.2f} | ${task['generation_cost_usd']:.9f} | "
            f"{task['total_elapsed_ms'] / 1000:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 6. 生成阶段按模型统计",
            "",
            (
                "| 模型 | 记录 | 计费调用 | 正费用 | 零用量占位 | 零费用 Tokens 调用 | "
                "输入 Tokens | 输出 Tokens | 推理 Tokens | 实付成本 | cost_source | 结论 |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    model_checks = report["model_checks"]
    for model, stats in report["generation_models"].items():
        source = ", ".join(f"{k}:{v}" for k, v in stats["cost_sources"].items())
        verdict = "通过" if model_checks.get(model, {}).get("pass") else "未通过"
        lines.append(
            f"| `{model}` | {stats['calls']} | {stats['billable_calls']} | "
            f"{stats['positive_cost_calls']} | {stats['nonbillable_zero_usage_calls']} | "
            f"{stats['unbilled_usage_calls']} | {stats['input_tokens']} | "
            f"{stats['output_tokens']} | {stats['reasoning_tokens']} | "
            f"${stats['billed_cost_usd']:.9f} | {source} | {verdict} |"
        )
    lines.extend(["", "## 7. Judge 阶段按模型统计", ""])
    if report["judge_models"]:
        lines.extend(
            [
                "| 模型 | 调用 | 正费用 | 零费用 | 实付成本 | cost_source |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        for model, stats in report["judge_models"].items():
            source = ", ".join(f"{k}:{v}" for k, v in stats["cost_sources"].items())
            lines.append(
                f"| `{model}` | {stats['calls']} | {stats['positive_cost_calls']} | "
                f"{stats['zero_cost_calls']} | ${stats['billed_cost_usd']:.9f} | {source} |"
            )
    else:
        lines.append("未发现 Judge 用量记录。")
    lines.extend(
        [
            "",
            f"- 唯一评分 criterion：{report['judge_unique_criterion_count']} 个",
            (
                f"- repeats={report['judge_repeats']} 展开后的基础调用："
                f"{report['judge_baseline_call_count']} 次"
            ),
            f"- 解析失败/空结果触发的额外重试：{report['judge_retry_call_count']} 次",
            "- Judge 调用按 criterion × repeat 计数，不是同一请求重复累计。",
        ]
    )
    lines.extend(["", "## 8. 严格校验", ""])
    for check in report["checks"]:
        lines.append(f"- {'✅' if check['pass'] else '❌'} {check['name']}: {check['detail']}")
    lines.extend(
        [
            "",
            "## 9. 口径说明",
            "",
            (
                "- `非计费零用量占位` 指 Tokens 全为 0、费用为 0、"
                "`cost_source=none` 的内部占位记录；它没有形成 OpenRouter 计费调用。"
            ),
            (
                "- 含 Tokens 但费用为 0 的调用只有在 OpenRouter 明确返回 "
                "`provider_billed` 且整批账户差额仍能精确对账时才可通过。"
            ),
            "- 总成本同时与 OpenRouter Key 账户用量增量对账；差额必须在配置容差内。",
            "- 成本精确结论只覆盖 OpenRouter LLM/Judge；Brave 等外部 API 不返回逐次美元费用。",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_jsonl", type=Path)
    parser.add_argument("--expected-tasks", type=int, default=10)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--account-usage-delta-usd", type=float)
    parser.add_argument("--account-byok-usage-delta-usd", type=float)
    parser.add_argument("--account-cost-tolerance-usd", type=float, default=0.000001)
    parser.add_argument("--expected-input-jsonl", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--trace-jsonl", type=Path, required=True)
    parser.add_argument("--effective-config", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--expected-agent-max-iterations", type=int, default=1)
    parser.add_argument("--expected-generation-max-attempts", type=int, default=3)
    parser.add_argument("--expected-concurrency", type=int, default=5)
    parser.add_argument("--expected-judge-repeats", type=int, default=1)
    parser.add_argument("--expected-judge-max-attempts", type=int, default=3)
    parser.add_argument("--expected-judge-concurrency", type=int, default=3)
    parser.add_argument("--expected-tool-mode", default="provider_only")
    parser.add_argument("--reference-effective-config", type=Path)
    parser.add_argument("--account-before", type=Path)
    parser.add_argument("--account-after", type=Path)
    parser.add_argument("--validation-account-before", type=Path)
    parser.add_argument("--validation-account-after", type=Path)
    parser.add_argument("--validation-manifest", type=Path)
    parser.add_argument("--validation-input-jsonl", type=Path)
    parser.add_argument("--expected-cache-namespace-sha256", default="")
    parser.add_argument("--required-observed-tools", default="")
    parser.add_argument(
        "--max-selected-tool-failure-rate",
        type=float,
        default=1.0,
        help="Maximum failed/(successful+failed) tool calls in selected attempts.",
    )
    parser.add_argument("--require-clean-source-now", action="store_true")
    parser.add_argument("--external-preflight-call-count", type=int, default=0)
    parser.add_argument("--validation-external-preflight-call-count", type=int, default=0)
    parser.add_argument(
        "--require-account-reconciliation",
        action="store_true",
        help="Fail unless before/after account snapshots are supplied and match the same key.",
    )
    args = parser.parse_args()

    if args.expected_cache_namespace_sha256 and not SHA256_PATTERN.fullmatch(
        args.expected_cache_namespace_sha256
    ):
        parser.error("--expected-cache-namespace-sha256 must be sha256:<64 lowercase hex>")
    required_observed_tools = {
        item.strip() for item in args.required_observed_tools.split(",") if item.strip()
    }
    if not required_observed_tools <= {"web_search", "web_fetch"}:
        parser.error("--required-observed-tools only accepts web_search,web_fetch")
    if not 0.0 <= args.max_selected_tool_failure_rate <= 1.0:
        parser.error("--max-selected-tool-failure-rate must be between 0 and 1")

    account_before = None
    account_after = None
    validation_account_before = None
    validation_account_after = None
    validation_manifest = None
    account_before_usage = None
    account_after_usage = None
    account_before_byok_usage = None
    account_after_byok_usage = None
    validation_before_usage = None
    validation_after_usage = None
    validation_before_byok_usage = None
    validation_after_byok_usage = None
    if args.require_account_reconciliation and (not args.account_before or not args.account_after):
        parser.error(
            "--require-account-reconciliation requires --account-before and --account-after"
        )
    if args.account_before or args.account_after:
        if not args.account_before or not args.account_after:
            parser.error("--account-before and --account-after must be provided together")
        account_before = json.loads(args.account_before.read_text(encoding="utf-8"))
        account_after = json.loads(args.account_after.read_text(encoding="utf-8"))
        if not isinstance(account_before, dict) or not isinstance(account_after, dict):
            parser.error("OpenRouter account snapshots must contain JSON objects")
        try:
            account_before_usage = required_decimal(
                account_before.get("usage"), label="account before usage"
            )
            account_after_usage = required_decimal(
                account_after.get("usage"), label="account after usage"
            )
            account_before_byok_usage = required_decimal(
                account_before.get("byok_usage"), label="account before byok_usage"
            )
            account_after_byok_usage = required_decimal(
                account_after.get("byok_usage"), label="account after byok_usage"
            )
        except ValueError as exc:
            parser.error(str(exc))
        if account_after_usage < account_before_usage:
            parser.error("OpenRouter account usage decreased across the benchmark window")
        if account_after_byok_usage < account_before_byok_usage:
            parser.error("OpenRouter BYOK usage decreased across the benchmark window")
        args.account_usage_delta_usd = account_after_usage - account_before_usage
        args.account_byok_usage_delta_usd = account_after_byok_usage - account_before_byok_usage
    if args.validation_account_before or args.validation_account_after:
        if not args.validation_account_before or not args.validation_account_after:
            parser.error(
                "--validation-account-before and --validation-account-after "
                "must be provided together"
            )
        validation_account_before = json.loads(
            args.validation_account_before.read_text(encoding="utf-8")
        )
        validation_account_after = json.loads(
            args.validation_account_after.read_text(encoding="utf-8")
        )
        if not isinstance(validation_account_before, dict) or not isinstance(
            validation_account_after, dict
        ):
            parser.error("OpenRouter validation snapshots must contain JSON objects")
        try:
            validation_before_usage = required_decimal(
                validation_account_before.get("usage"),
                label="validation before usage",
            )
            validation_after_usage = required_decimal(
                validation_account_after.get("usage"),
                label="validation after usage",
            )
            validation_before_byok_usage = required_decimal(
                validation_account_before.get("byok_usage"),
                label="validation before byok_usage",
            )
            validation_after_byok_usage = required_decimal(
                validation_account_after.get("byok_usage"),
                label="validation after byok_usage",
            )
        except ValueError as exc:
            parser.error(str(exc))
        if validation_after_usage < validation_before_usage:
            parser.error("OpenRouter validation usage decreased across the canary window")
        if validation_after_byok_usage < validation_before_byok_usage:
            parser.error("OpenRouter validation BYOK usage decreased across the canary window")
    if args.validation_manifest:
        validation_manifest = json.loads(args.validation_manifest.read_text(encoding="utf-8"))
        if not isinstance(validation_manifest, dict):
            parser.error("OpenRouter validation manifest must contain a JSON object")

    rows, trace_rows, args.manifest, bound_compact_manifest = load_audit_artifact_rows(
        args.result_jsonl,
        trace_jsonl=args.trace_jsonl,
        manifest_path=args.manifest,
    )
    b2_rows = [row for row in rows if row.get("group") == "B2"]
    task_ids = [str(row.get("task_id") or "") for row in b2_rows]
    task_counts = Counter(task_ids)
    duplicates = sum(count - 1 for count in task_counts.values() if count > 1)
    completed = sum(bool(row.get("final_text")) and not row.get("error") for row in b2_rows)
    usage_unknown = sum(int(number(row.get("usage_unknown_count"))) for row in b2_rows)
    result_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    trace_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    trace_integrity_issues: list[str] = []
    for line_number, row in enumerate(rows, 1):
        key = (str(row.get("group") or ""), str(row.get("task_id") or ""))
        if key in result_by_key:
            trace_integrity_issues.append(f"duplicate result key {key}")
        result_by_key[key] = row
        if int(number(row.get("row_index"))) != line_number:
            trace_integrity_issues.append(f"result row_index mismatch at line {line_number}")
    for line_number, row in enumerate(trace_rows, 1):
        key = (str(row.get("group") or ""), str(row.get("task_id") or ""))
        if key in trace_by_key:
            trace_integrity_issues.append(f"duplicate trace key {key}")
        trace_by_key[key] = row
        if int(number(row.get("row_index"))) != line_number:
            trace_integrity_issues.append(f"trace row_index mismatch at line {line_number}")
    if set(result_by_key) != set(trace_by_key):
        trace_integrity_issues.append("result and trace key sets differ")
    for key in sorted(set(result_by_key) & set(trace_by_key)):
        result_row = result_by_key[key]
        trace_row_value = trace_by_key[key]
        if not verify_result_row_evidence(result_row):
            trace_integrity_issues.append(f"invalid full-result evidence hash for {key}")
        expected_trace = trace_row_from_result(result_row)
        if trace_row_value != expected_trace:
            trace_integrity_issues.append(
                f"trace is not the exact deterministic result projection for {key}"
            )
        for field in ("task_input_sha256", "run_compatibility_fingerprint"):
            result_value = str(result_row.get(field) or "")
            trace_value = str(trace_row_value.get(field) or "")
            if not SHA256_PATTERN.fullmatch(result_value):
                trace_integrity_issues.append(f"invalid result {field} for {key}")
            if result_value != trace_value:
                trace_integrity_issues.append(f"result/trace {field} mismatch for {key}")
        if result_row.get("row_index") != trace_row_value.get("row_index"):
            trace_integrity_issues.append(f"result/trace row_index mismatch for {key}")
    expected_task_ids: set[str] | None = None
    expected_prompts: dict[str, str] = {}
    expected_task_input_hashes: dict[str, str] = {}
    expected_rubrics: dict[str, dict[str, Any]] = {}
    if args.expected_input_jsonl:
        expected_rows = load_rows(args.expected_input_jsonl)
        for row in expected_rows:
            normalized_task = normalize_expected_draco_task(row)
            task_id = str(normalized_task.get("id") or "")
            if task_id:
                expected_prompts[task_id] = str(normalized_task.get("prompt") or "")
                expected_task_input_hashes[task_id] = canonical_json_sha256(normalized_task)
                rubric = normalized_task.get("rubric")
                if isinstance(rubric, dict):
                    criteria: list[dict[str, Any]] = []
                    sections = rubric.get("sections")
                    for section in sections if isinstance(sections, list) else []:
                        if not isinstance(section, dict):
                            continue
                        section_id = str(section.get("id") or "")
                        section_title = str(section.get("title") or "")
                        for criterion in (
                            section.get("criteria")
                            if isinstance(section.get("criteria"), list)
                            else []
                        ):
                            if not isinstance(criterion, dict):
                                continue
                            criteria.append(
                                {
                                    "id": str(criterion.get("id") or ""),
                                    "section_id": section_id,
                                    "section_title": section_title,
                                    "weight": criterion.get("weight"),
                                    "requirement": str(criterion.get("requirement") or ""),
                                }
                            )
                    expected_rubrics[task_id] = {
                        "id": str(rubric.get("id") or ""),
                        "criteria": criteria,
                    }
        expected_task_ids = set(expected_prompts)
        expected_task_ids.discard("")
    actual_task_ids = set(task_counts)
    missing_task_ids = sorted((expected_task_ids or set()) - actual_task_ids)
    unexpected_task_ids = sorted(actual_task_ids - (expected_task_ids or actual_task_ids))
    prompt_mismatch_task_ids = []
    task_input_mismatch_task_ids = []
    if expected_prompts:
        for row in b2_rows:
            task_id = str(row.get("task_id") or "")
            expected_prompt = expected_prompts.get(task_id)
            if expected_prompt is None:
                continue
            expected_sha256 = hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest()
            if (
                str(row.get("prompt") or "") != expected_prompt
                or str(row.get("prompt_sha256") or "") != expected_sha256
            ):
                prompt_mismatch_task_ids.append(task_id)
            if str(row.get("task_input_sha256") or "") != expected_task_input_hashes.get(
                task_id, ""
            ):
                task_input_mismatch_task_ids.append(task_id)
        prompt_mismatch_task_ids.sort()
        task_input_mismatch_task_ids.sort()
    validation_input_task_ids: set[str] = set()
    validation_input_prompt_hashes: set[str] = set()
    if args.validation_input_jsonl:
        for row in load_rows(args.validation_input_jsonl):
            validation_task_id = str(row.get("task_id") or row.get("id") or "")
            validation_prompt = str(
                row.get("prompt") if row.get("prompt") is not None else row.get("problem") or ""
            )
            if validation_task_id:
                validation_input_task_ids.add(validation_task_id)
            validation_input_prompt_hashes.add(
                hashlib.sha256(validation_prompt.encode("utf-8")).hexdigest()
            )

    generation_calls: list[dict[str, Any]] = []
    judge_calls: list[dict[str, Any]] = []
    generation_usage_issues: list[str] = []
    judge_usage_issues: list[str] = []
    generation_protocol_issues: list[str] = []
    judge_protocol_issues: list[str] = []
    tool_trace_issues: list[str] = []
    tool_error_reasons: Counter[str] = Counter()
    successful_tool_names: Counter[str] = Counter()
    selected_successful_tool_names: Counter[str] = Counter()
    failed_tool_names: Counter[str] = Counter()
    selected_failed_tool_names: Counter[str] = Counter()
    selected_tool_infrastructure_issues: list[str] = []
    raw_llm_cost = Decimal(0)

    def validate_call(call: dict[str, Any], *, label: str, issues: list[str]) -> None:
        nonlocal raw_llm_cost
        try:
            cost = required_decimal(call.get("billed_cost"), label=f"{label} cost")
            for token_field in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "cache_write_tokens",
            ):
                tokens = required_decimal(call.get(token_field, 0), label=f"{label} {token_field}")
                if tokens != tokens.to_integral_value():
                    raise ValueError(f"{label} {token_field} must be an integer")
        except ValueError as exc:
            issues.append(str(exc))
            return
        raw_llm_cost += cost

    def trace_sequence_is_valid(events: list[Any]) -> bool:
        event_sequences = [
            event.get("seq") if isinstance(event, dict) else None for event in events
        ]
        sequence_start = (
            0
            if events and isinstance(events[0], dict) and events[0].get("kind") == "routing_setup"
            else 1
        )
        return event_sequences == list(range(sequence_start, sequence_start + len(events)))

    def validate_attempt_tool_trace(
        *, task_id: str, scope: str, events_value: Any, selected: bool = False
    ) -> int:
        if not isinstance(events_value, list):
            tool_trace_issues.append(f"{task_id}/{scope}: trace_events is not a list")
            return 0
        events = events_value
        if not trace_sequence_is_valid(events):
            tool_trace_issues.append(f"{task_id}/{scope}: trace event sequence is invalid")
        starts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        results: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if not isinstance(event, dict):
                continue
            tool_use_id = str(event.get("tool_use_id") or "")
            if event.get("kind") == "tool_use_start":
                starts[tool_use_id].append(event)
            elif event.get("kind") == "tool_result":
                results[tool_use_id].append(event)
        if set(starts) != set(results):
            tool_trace_issues.append(f"{task_id}/{scope}: tool start/result ID sets differ")
        for tool_use_id in sorted(set(starts) | set(results)):
            start_rows = starts.get(tool_use_id, [])
            result_rows = results.get(tool_use_id, [])
            if not tool_use_id or len(start_rows) != 1 or len(result_rows) != 1:
                tool_trace_issues.append(
                    f"{task_id}/{scope}: tool {tool_use_id or '<missing>'} "
                    "is not one start/one result"
                )
                continue
            start_event = start_rows[0]
            result_event = result_rows[0]
            tool_name = str(start_event.get("tool_name") or "")
            if tool_name != str(result_event.get("tool_name") or ""):
                tool_trace_issues.append(
                    f"{task_id}/{scope}: tool name differs between start/result for {tool_use_id}"
                )
            status = result_event.get("execution_status")
            status_value = status.get("status") if isinstance(status, dict) else None
            reason = str(status.get("reason") or "") if isinstance(status, dict) else ""
            diagnostic = result_event.get("diagnostic")
            diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
            diagnostic_status_value = diagnostic.get("status")
            diagnostic_status = str(diagnostic_status_value or "").casefold()
            diagnostic_reason = str(diagnostic.get("reason") or diagnostic.get("reason_code") or "")
            diagnostic_http_failure = False
            for http_status_value in (
                diagnostic.get("http_status"),
                diagnostic_status_value,
            ):
                if isinstance(http_status_value, bool):
                    continue
                try:
                    diagnostic_http_failure = int(http_status_value) >= 400
                except (TypeError, ValueError):
                    continue
                if diagnostic_http_failure:
                    break
            envelope_error = bool(
                diagnostic.get("error_present")
                or diagnostic.get("error_class")
                or diagnostic.get("ok") is False
                or diagnostic_http_failure
                or diagnostic_status
                in {
                    "error",
                    "failed",
                    "denied",
                    "approval_denied",
                    "rejected",
                }
            )
            if not isinstance(status, dict) or status_value not in {
                "success",
                "error",
                "timeout",
                "cancelled",
            }:
                tool_trace_issues.append(
                    f"{task_id}/{scope}: tool result lacks a definitive execution status"
                )
            is_error = result_event.get("is_error") is True
            effective_error = is_error or envelope_error
            if effective_error:
                failed_tool_names[tool_name] += 1
                if selected:
                    selected_failed_tool_names[tool_name] += 1
                    # web_search is the benchmark's configured Brave service,
                    # so an error here is infrastructure/auth/quota health, not
                    # an arbitrary target page returning a content-level 404.
                    if tool_name == "web_search":
                        selected_tool_infrastructure_issues.append(
                            f"{task_id}/{scope}: selected web_search failed "
                            f"({reason or diagnostic_reason or diagnostic_status or 'unspecified'})"
                        )
                tool_error_reasons[
                    reason or diagnostic_reason or diagnostic_status or "unspecified"
                ] += 1
            else:
                successful_tool_names[tool_name] += 1
                if selected:
                    selected_successful_tool_names[tool_name] += 1
            if (is_error and status_value == "success") or (
                not is_error and status_value in {"error", "timeout", "cancelled"}
            ):
                tool_trace_issues.append(
                    f"{task_id}/{scope}: is_error and execution status disagree for {tool_use_id}"
                )
            control_reason = f"{reason} {diagnostic_reason} {diagnostic_status}".casefold()
            if any(
                token in control_reason
                for token in (
                    "approval",
                    "sandbox",
                    "policy",
                    "permission",
                    "budget",
                    "run_mode",
                )
            ):
                tool_trace_issues.append(
                    f"{task_id}/{scope}: benchmark control blocked tool execution ({reason})"
                )
        return sum(len(items) for items in starts.values())

    judge_judgment_count = 0
    for row in b2_rows:
        task_id = str(row.get("task_id") or "")
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
        if not isinstance(attempts, list) or not attempts:
            generation_usage_issues.append(f"{task_id}: missing generation attempts")
            attempts = []
        attempt_numbers = [
            int(number(item.get("attempt"))) if isinstance(item, dict) else 0 for item in attempts
        ]
        if len(attempts) > args.expected_generation_max_attempts or attempt_numbers != list(
            range(1, len(attempts) + 1)
        ):
            generation_protocol_issues.append(
                f"{task_id}: generation attempts are not consecutive 1.."
                f"{args.expected_generation_max_attempts}"
            )
        for attempt_index, attempt in enumerate(attempts, 1):
            run = attempt.get("run") if isinstance(attempt, dict) else None
            usage = run.get("usage") if isinstance(run, dict) else None
            calls = (
                list(iter_usage_calls(usage, phase="generation")) if isinstance(usage, dict) else []
            )
            expected_requests = int(number(run.get("llm_request_count"))) if run else 0
            if expected_requests != len(calls):
                generation_usage_issues.append(
                    f"{task_id}/attempt-{attempt_index}: "
                    f"llm_request_count={expected_requests}, usage_rows={len(calls)}"
                )
            by_agent_call: dict[int, Counter[tuple[str, str]]] = defaultdict(Counter)
            for call_number, call in enumerate(calls, 1):
                label = f"{task_id}/attempt-{attempt_index}/call-{call_number}"
                validate_call(call, label=label, issues=generation_usage_issues)
                if str(call.get("provider") or "") != "openrouter":
                    generation_usage_issues.append(f"{label}: provider is not openrouter")
                if str(call.get("cost_source") or "") != "provider_billed":
                    generation_usage_issues.append(f"{label}: cost is not provider_billed")
                agent_call_index = int(number(call.get("agent_call_index")))
                if agent_call_index <= 0:
                    generation_usage_issues.append(f"{label}: missing agent_call_index")
                actual_model = str(call.get("model") or "")
                canonical_model = canonical_frozen_model(actual_model, EXPECTED_MODELS)
                if canonical_model is None:
                    generation_usage_issues.append(
                        f"{label}: actual model is not a frozen B2 model: {actual_model}"
                    )
                else:
                    validate_router_attestation(
                        call,
                        expected_model=canonical_model,
                        label=label,
                        issues=generation_usage_issues,
                        expected_cache_namespace_sha256=(args.expected_cache_namespace_sha256),
                    )
                by_agent_call[agent_call_index][
                    (str(call.get("role") or ""), canonical_model or actual_model)
                ] += 1
            for agent_call_index, actual_pairs in sorted(by_agent_call.items()):
                if actual_pairs != EXPECTED_ROLE_MODEL_PAIRS:
                    generation_usage_issues.append(
                        f"{task_id}/attempt-{attempt_index}/agent-{agent_call_index}: "
                        "role/model set differs from the frozen B2 ensemble"
                    )
            if by_agent_call:
                actual_agent_indices = sorted(by_agent_call)
                if (
                    actual_agent_indices != list(range(1, len(actual_agent_indices) + 1))
                    or actual_agent_indices[-1] > args.expected_agent_max_iterations
                ):
                    generation_protocol_issues.append(
                        f"{task_id}/attempt-{attempt_index}: agent_call_index is not "
                        f"consecutive within 1..{args.expected_agent_max_iterations}"
                    )
            generation_calls.extend(calls)
        judge = row.get("judge")
        rubric = expected_rubrics.get(task_id)
        if not isinstance(judge, dict):
            judge_protocol_issues.append(f"{task_id}: missing Judge payload")
        elif not isinstance(rubric, dict):
            judge_protocol_issues.append(f"{task_id}: missing input rubric")
        else:
            rubric_criteria = rubric.get("criteria")
            rubric_criteria = rubric_criteria if isinstance(rubric_criteria, list) else []
            judgments = judge.get("criterion_judgments")
            judgments = judgments if isinstance(judgments, list) else []
            expected_judgment_count = len(rubric_criteria) * args.expected_judge_repeats
            if any(
                (
                    judge.get("mode") != "draco_criterion_judgments",
                    judge.get("rubric_id") != rubric.get("id"),
                    judge.get("judge_model") != EXPECTED_JUDGE,
                    int(number(judge.get("judge_repeats"))) != args.expected_judge_repeats,
                    int(number(judge.get("rubric_criteria_count"))) != len(rubric_criteria),
                    int(number(judge.get("criteria_count"))) != expected_judgment_count,
                    len(judgments) != expected_judgment_count,
                    int(number(judge.get("valid_criteria_count"))) != expected_judgment_count,
                    int(number(judge.get("invalid_criteria_count"))) != 0,
                    int(number(judge.get("judge_error_count"))) != 0,
                    judge.get("score_status") != "complete",
                )
            ):
                judge_protocol_issues.append(
                    f"{task_id}: Judge summary differs from the input rubric/repeat contract"
                )
            expected_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
            for criterion in rubric_criteria:
                if not isinstance(criterion, dict):
                    continue
                for repeat_index in range(args.expected_judge_repeats):
                    expected_by_key[
                        (
                            str(criterion.get("id") or ""),
                            str(criterion.get("section_id") or ""),
                            repeat_index,
                        )
                    ] = criterion
            actual_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
            for judgment_index, judgment in enumerate(judgments, 1):
                if not isinstance(judgment, dict):
                    judge_protocol_issues.append(
                        f"{task_id}/judgment-{judgment_index}: not an object"
                    )
                    continue
                repeat_value = judgment.get("repeat_index")
                repeat_index = (
                    repeat_value
                    if isinstance(repeat_value, int) and not isinstance(repeat_value, bool)
                    else -1
                )
                key = (
                    str(judgment.get("id") or ""),
                    str(judgment.get("section_id") or ""),
                    repeat_index,
                )
                if key in actual_by_key:
                    judge_protocol_issues.append(
                        f"{task_id}: duplicate Judge criterion/repeat {key}"
                    )
                actual_by_key[key] = judgment
                expected_criterion = expected_by_key.get(key)
                if not isinstance(expected_criterion, dict) or any(
                    (
                        judgment.get("section_title") != expected_criterion.get("section_title"),
                        judgment.get("weight") != expected_criterion.get("weight"),
                        judgment.get("requirement") != expected_criterion.get("requirement"),
                        not isinstance(judgment.get("met"), bool),
                    )
                ):
                    judge_protocol_issues.append(
                        f"{task_id}: Judge criterion/repeat does not match rubric {key}"
                    )
                judge_attempts = judgment.get("judge_attempts")
                judge_attempts = judge_attempts if isinstance(judge_attempts, list) else []
                logical_attempts = [
                    int(number(item.get("attempt"))) if isinstance(item, dict) else 0
                    for item in judge_attempts
                ]
                if (
                    not judge_attempts
                    or len(judge_attempts) > args.expected_judge_max_attempts
                    or logical_attempts != list(range(1, len(judge_attempts) + 1))
                    or int(number(judgment.get("judge_attempt_count"))) != len(judge_attempts)
                ):
                    judge_protocol_issues.append(
                        f"{task_id}: Judge retry attempts violate the configured limit for {key}"
                    )
                if judge_attempts:
                    last_attempt = judge_attempts[-1]
                    last_attempt = last_attempt if isinstance(last_attempt, dict) else {}
                    if any(
                        (
                            last_attempt.get("verdict") != judgment.get("verdict"),
                            last_attempt.get("met") != judgment.get("met"),
                            last_attempt.get("run") != judgment.get("judge_run"),
                        )
                    ):
                        judge_protocol_issues.append(
                            f"{task_id}: Judge top-level result does not match the "
                            f"last retry attempt for {key}"
                        )
            if set(actual_by_key) != set(expected_by_key):
                judge_protocol_issues.append(
                    f"{task_id}: Judge criterion/repeat key set differs from input rubric"
                )
        if isinstance(judge, dict) and isinstance(judge.get("criterion_judgments"), list):
            judge_judgment_count += len(judge["criterion_judgments"])
        judge_scopes = [("judge", judge)] + [
            (f"candidate_judge_{index}", candidate)
            for index, candidate in enumerate(row.get("candidate_judges") or [], 1)
        ]
        for phase, judge_value in judge_scopes:
            for run_index, run in enumerate(iter_judge_runs(judge_value), 1):
                usage = run.get("usage")
                calls = (
                    list(iter_usage_calls(usage, phase=phase)) if isinstance(usage, dict) else []
                )
                expected_requests = int(number(run.get("llm_request_count")))
                if expected_requests != len(calls):
                    judge_usage_issues.append(
                        f"{task_id}/{phase}-{run_index}: "
                        f"llm_request_count={expected_requests}, usage_rows={len(calls)}"
                    )
                for call_number, call in enumerate(calls, 1):
                    label = f"{task_id}/{phase}-{run_index}/call-{call_number}"
                    validate_call(call, label=label, issues=judge_usage_issues)
                    judge_model = canonical_frozen_model(call.get("model"), (EXPECTED_JUDGE,))
                    if judge_model != EXPECTED_JUDGE:
                        judge_usage_issues.append(f"{label}: unexpected Judge model")
                    else:
                        validate_router_attestation(
                            call,
                            expected_model=EXPECTED_JUDGE,
                            label=label,
                            issues=judge_usage_issues,
                            expected_cache_namespace_sha256=(args.expected_cache_namespace_sha256),
                        )
                    if str(call.get("cost_source") or "") != "provider_billed":
                        judge_usage_issues.append(f"{label}: cost is not provider_billed")
                judge_calls.extend(calls)

        run_trace = row.get("run_trace")
        events_value = run_trace.get("events") if isinstance(run_trace, dict) else None
        events = events_value if isinstance(events_value, list) else []
        event_count = run_trace.get("event_count") if isinstance(run_trace, dict) else None
        if (
            not isinstance(run_trace, dict)
            or not isinstance(events_value, list)
            or isinstance(event_count, bool)
            or not isinstance(event_count, int)
            or event_count != len(events)
        ):
            tool_trace_issues.append(f"{task_id}: run_trace event_count mismatch")
        if not trace_sequence_is_valid(events):
            tool_trace_issues.append(f"{task_id}: run_trace event sequence is invalid")

        selected_attempt = (
            int(number(execution.get("selected_generation_attempt")))
            if isinstance(execution, dict)
            else 0
        )
        attempt_events_by_number: dict[int, list[Any]] = {}
        attempt_runs_by_number: dict[int, dict[str, Any]] = {}
        attempt_tool_call_count = 0
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_number = int(number(attempt.get("attempt")))
            attempt_run = attempt.get("run")
            attempt_events = (
                attempt_run.get("trace_events") if isinstance(attempt_run, dict) else None
            )
            if attempt_number > 0 and isinstance(attempt_events, list):
                attempt_events_by_number[attempt_number] = attempt_events
            if attempt_number > 0 and isinstance(attempt_run, dict):
                attempt_runs_by_number[attempt_number] = attempt_run
            attempt_tool_call_count += validate_attempt_tool_trace(
                task_id=task_id,
                scope=f"generation-attempt-{attempt_number or '<missing>'}",
                events_value=attempt_events,
                selected=attempt_number == selected_attempt,
            )
        selected_events = attempt_events_by_number.get(selected_attempt)
        if selected_events is None or events != selected_events:
            tool_trace_issues.append(
                f"{task_id}: top-level run_trace does not match selected generation attempt"
            )
        selected_run = attempt_runs_by_number.get(selected_attempt)
        if not isinstance(selected_run, dict):
            generation_protocol_issues.append(
                f"{task_id}: selected generation attempt does not resolve to a run"
            )
        else:
            if selected_run.get("final_text_sha256") != row.get("final_text_sha256"):
                generation_protocol_issues.append(
                    f"{task_id}: selected generation final_text_sha256 differs from row"
                )
            if selected_run.get("error") != row.get("error"):
                generation_protocol_issues.append(
                    f"{task_id}: selected generation error differs from row"
                )
        if int(number(row.get("total_tool_call_count"))) != attempt_tool_call_count:
            tool_trace_issues.append(f"{task_id}: total_tool_call_count differs from trace")

    generation_response_models = {str(call.get("model") or "") for call in generation_calls}
    judge_response_models = {str(call.get("model") or "") for call in judge_calls}
    generation_models = aggregate(
        [
            {
                **call,
                "model": canonical_frozen_model(call.get("model"), EXPECTED_MODELS)
                or str(call.get("model") or ""),
            }
            for call in generation_calls
        ]
    )
    judge_models = aggregate(
        [
            {
                **call,
                "model": canonical_frozen_model(call.get("model"), (EXPECTED_JUDGE,))
                or str(call.get("model") or ""),
            }
            for call in judge_calls
        ]
    )
    generation_actual_models = {
        canonical_frozen_model(call.get("model"), EXPECTED_MODELS) or str(call.get("model") or "")
        for call in generation_calls
    }
    judge_actual_models = {
        canonical_frozen_model(call.get("model"), (EXPECTED_JUDGE,)) or str(call.get("model") or "")
        for call in judge_calls
    }
    row_accounting_cost = Decimal(0)
    row_accounting_issues: list[str] = []
    external_unpriced_call_upper_bound = 0
    observed_web_search_invocations = 0
    observed_web_fetch_invocations = 0
    candidate_judge_count = 0
    for row in b2_rows:
        task_id = str(row.get("task_id") or "")
        candidate_judge_count += len(row.get("candidate_judges") or [])
        accounting = row.get("cost_accounting")
        llm_total = accounting.get("llm_total") if isinstance(accounting, dict) else None
        if not isinstance(llm_total, dict):
            row_accounting_issues.append(f"{task_id}: missing llm_total accounting")
        else:
            try:
                row_accounting_cost += required_decimal(
                    llm_total.get("recorded_cost_usd"),
                    label=f"{task_id} recorded LLM cost",
                )
                request_count = required_decimal(
                    llm_total.get("request_count"), label=f"{task_id} request_count"
                )
                exact_request_count = required_decimal(
                    llm_total.get("exact_request_count"),
                    label=f"{task_id} exact_request_count",
                )
                if llm_total.get("cost_exact") is not True or request_count != exact_request_count:
                    row_accounting_issues.append(f"{task_id}: row LLM accounting is not exact")
            except ValueError as exc:
                row_accounting_issues.append(str(exc))
        non_byok = row.get("openrouter_non_byok_audit")
        if not isinstance(non_byok, dict) or non_byok.get("pass") is not True:
            row_accounting_issues.append(f"{task_id}: non-BYOK audit did not pass")
        external = accounting.get("external_tools") if isinstance(accounting, dict) else None
        if not isinstance(external, dict):
            row_accounting_issues.append(f"{task_id}: missing external tool accounting")
        else:
            external_unpriced_call_upper_bound += int(
                number(external.get("potentially_unpriced_tool_call_count_upper_bound"))
            )
        execution = row.get("execution")
        generation_attempts = (
            execution.get("generation_attempts") if isinstance(execution, dict) else None
        )
        for attempt in generation_attempts if isinstance(generation_attempts, list) else []:
            attempt_run = attempt.get("run") if isinstance(attempt, dict) else None
            events = attempt_run.get("trace_events") if isinstance(attempt_run, dict) else None
            for event in events if isinstance(events, list) else []:
                if not isinstance(event, dict) or event.get("kind") != "tool_use_start":
                    continue
                tool_name = str(event.get("tool_name") or "")
                observed_web_search_invocations += int(tool_name == "web_search")
                observed_web_fetch_invocations += int(tool_name == "web_fetch")
    model_checks = {}
    selected_tool_success_count = sum(selected_successful_tool_names.values())
    selected_tool_failure_count = sum(selected_failed_tool_names.values())
    selected_tool_attempt_count = selected_tool_success_count + selected_tool_failure_count
    selected_tool_failure_rate = (
        selected_tool_failure_count / selected_tool_attempt_count
        if selected_tool_attempt_count
        else 0.0
    )

    checks = [
        {
            "name": "仅运行 B2",
            "pass": len(b2_rows) == len(rows),
            "detail": f"B2 rows={len(b2_rows)}, all rows={len(rows)}",
        },
        {
            "name": "DRACO mini 任务覆盖",
            "pass": len(task_counts) == args.expected_tasks,
            "detail": f"unique={len(task_counts)}, expected={args.expected_tasks}",
        },
        {
            "name": "所有任务成功",
            "pass": completed == args.expected_tasks,
            "detail": f"completed={completed}, expected={args.expected_tasks}",
        },
        {
            "name": "无重复任务",
            "pass": duplicates == 0,
            "detail": f"duplicates={duplicates}",
        },
        {
            "name": "无未知用量",
            "pass": usage_unknown == 0,
            "detail": f"usage_unknown_count={usage_unknown}",
        },
        {
            "name": "Result 与 trace 完整且逐行一致",
            "pass": len(trace_rows) == args.expected_tasks and not trace_integrity_issues,
            "detail": (
                f"result_rows={len(rows)}, trace_rows={len(trace_rows)}, "
                f"issues={len(trace_integrity_issues)}"
            ),
        },
        {
            "name": "生成阶段实际模型、角色与每轮五成员完全一致",
            "pass": generation_actual_models == set(EXPECTED_MODELS)
            and not generation_usage_issues
            and not generation_protocol_issues,
            "detail": (
                f"models={sorted(generation_actual_models)}, "
                f"usage_issues={len(generation_usage_issues)}, "
                f"protocol_issues={len(generation_protocol_issues)}"
            ),
        },
        {
            "name": "Judge 实际模型与物理请求费用证据完全一致",
            "pass": judge_actual_models == {EXPECTED_JUDGE}
            and not judge_usage_issues
            and not judge_protocol_issues
            and candidate_judge_count == 0,
            "detail": (
                f"models={sorted(judge_actual_models)}, "
                f"candidate_judges={candidate_judge_count}, "
                f"usage_issues={len(judge_usage_issues)}, "
                f"protocol_issues={len(judge_protocol_issues)}"
            ),
        },
        {
            "name": "任务内 Web 工具执行已配对且未被安全/预算控制拦截",
            "pass": not tool_trace_issues
            and not selected_tool_infrastructure_issues
            and required_observed_tools <= set(selected_successful_tool_names)
            and selected_tool_failure_rate <= args.max_selected_tool_failure_rate,
            "detail": (
                f"trace_issues={len(tool_trace_issues)}, "
                f"infrastructure_issues={len(selected_tool_infrastructure_issues)}, "
                f"all_attempt_success={dict(sorted(successful_tool_names.items()))}, "
                f"selected_attempt_success="
                f"{dict(sorted(selected_successful_tool_names.items()))}, "
                f"selected_attempt_failed="
                f"{dict(sorted(selected_failed_tool_names.items()))}, "
                f"selected_failure_rate={selected_tool_failure_rate:.6f}, "
                f"max_selected_failure_rate={args.max_selected_tool_failure_rate:.6f}, "
                f"failed={dict(sorted(failed_tool_names.items()))}, "
                f"required={sorted(required_observed_tools)}"
            ),
        },
        {
            "name": "每行 OpenRouter 非 BYOK 与成本核算均为精确",
            "pass": not row_accounting_issues,
            "detail": f"issues={len(row_accounting_issues)}",
        },
    ]
    if account_before is not None and account_after is not None:
        before_key_fingerprint = str(account_before.get("api_key_sha256") or "")
        after_key_fingerprint = str(account_after.get("api_key_sha256") or "")
        before_captured_at = capture_timestamp(account_before.get("captured_at"))
        after_captured_at = capture_timestamp(account_after.get("captured_at"))
        checks.append(
            {
                "name": "OpenRouter 对账使用同一 API key",
                "pass": bool(before_key_fingerprint)
                and before_key_fingerprint == after_key_fingerprint,
                "detail": "key fingerprint present and unchanged",
            }
        )
        settlement = account_after.get("settlement")
        settlement_valid = False
        settlement_detail = "missing"
        if isinstance(settlement, dict):
            try:
                settlement_expected = required_decimal(
                    settlement.get("expected_recorded_cost_usd"),
                    label="settlement expected cost",
                )
                settlement_observed = required_decimal(
                    settlement.get("observed_usage_delta_usd"),
                    label="settlement observed delta",
                )
                settlement_tolerance = required_decimal(
                    settlement.get("tolerance_usd"),
                    label="settlement tolerance",
                )
                settlement_attempts = required_decimal(
                    settlement.get("attempts"), label="settlement attempts"
                )
                settlement_valid = bool(
                    settlement_attempts >= 1
                    and settlement_observed + settlement_tolerance >= settlement_expected
                )
                settlement_detail = (
                    f"attempts={settlement_attempts}, expected={settlement_expected}, "
                    f"observed={settlement_observed}"
                )
            except ValueError as exc:
                settlement_detail = str(exc)
        checks.append(
            {
                "name": "OpenRouter 账单已完成结算轮询",
                "pass": settlement_valid,
                "detail": settlement_detail,
            }
        )
        checks.append(
            {
                "name": "OpenRouter 对账 key 与 benchmark 进程环境一致",
                "pass": account_before.get("benchmark_environment_key_verified") is True
                and account_after.get("benchmark_environment_key_verified") is True,
                "detail": "environment key fingerprint verified in both snapshots",
            }
        )
        checks.append(
            {
                "name": "OpenRouter 账户快照时间顺序有效",
                "pass": before_captured_at is not None
                and after_captured_at is not None
                and before_captured_at <= after_captured_at,
                "detail": (
                    f"before={account_before.get('captured_at')}, "
                    f"after={account_after.get('captured_at')}"
                ),
            }
        )
    if validation_account_before is not None and validation_account_after is not None:
        validation_before_fingerprint = str(validation_account_before.get("api_key_sha256") or "")
        validation_after_fingerprint = str(validation_account_after.get("api_key_sha256") or "")
        benchmark_fingerprint = str((account_before or {}).get("api_key_sha256") or "")
        validation_before_time = capture_timestamp(validation_account_before.get("captured_at"))
        validation_after_time = capture_timestamp(validation_account_after.get("captured_at"))
        benchmark_before_time = capture_timestamp((account_before or {}).get("captured_at"))
        checks.extend(
            [
                {
                    "name": "canary 与 benchmark 使用同一 OpenRouter API key",
                    "pass": bool(benchmark_fingerprint)
                    and validation_before_fingerprint == benchmark_fingerprint
                    and validation_after_fingerprint == benchmark_fingerprint,
                    "detail": "all key fingerprints present and equal",
                },
                {
                    "name": "canary 对账 key 与 benchmark 进程环境一致",
                    "pass": validation_account_before.get("benchmark_environment_key_verified")
                    is True
                    and validation_account_after.get("benchmark_environment_key_verified") is True,
                    "detail": "environment key fingerprint verified in canary snapshots",
                },
                {
                    "name": "canary 在正式 benchmark 前完成",
                    "pass": validation_before_time is not None
                    and validation_after_time is not None
                    and benchmark_before_time is not None
                    and validation_before_time <= validation_after_time
                    and validation_after_time <= benchmark_before_time,
                    "detail": (
                        f"canary_before={validation_before_time}, "
                        f"canary_after={validation_after_time}, "
                        f"benchmark_before={benchmark_before_time}"
                    ),
                },
                {
                    "name": "canary OpenRouter BYOK 用量为零",
                    "pass": validation_after_byok_usage == validation_before_byok_usage,
                    "detail": "validation byok_usage delta must be exactly zero",
                },
            ]
        )
        validation_settlement = validation_account_after.get("settlement")
        validation_settlement_valid = False
        validation_settlement_detail = "missing settlement evidence"
        if isinstance(validation_settlement, dict):
            try:
                validation_expected = required_decimal(
                    validation_settlement.get("expected_recorded_cost_usd"),
                    label="canary settlement expected cost",
                )
                validation_observed = required_decimal(
                    validation_settlement.get("observed_usage_delta_usd"),
                    label="canary settlement observed delta",
                )
                validation_tolerance = required_decimal(
                    validation_settlement.get("tolerance_usd"),
                    label="canary settlement tolerance",
                )
                validation_attempts = required_decimal(
                    validation_settlement.get("attempts"),
                    label="canary settlement attempts",
                )
                validation_settlement_valid = bool(
                    validation_attempts >= 1
                    and validation_observed + validation_tolerance >= validation_expected
                )
                validation_settlement_detail = (
                    f"attempts={validation_attempts}, expected={validation_expected}, "
                    f"observed={validation_observed}"
                )
            except ValueError as exc:
                validation_settlement_detail = str(exc)
        checks.append(
            {
                "name": "canary OpenRouter 账单已完成结算轮询",
                "pass": validation_settlement_valid,
                "detail": validation_settlement_detail,
            }
        )
    if expected_task_ids is not None:
        checks.append(
            {
                "name": "任务 ID 与 DRACO mini 输入完全一致",
                "pass": not missing_task_ids and not unexpected_task_ids,
                "detail": (
                    f"expected={len(expected_task_ids)}, actual={len(actual_task_ids)}, "
                    f"missing={len(missing_task_ids)}, unexpected={len(unexpected_task_ids)}"
                ),
            }
        )
        checks.append(
            {
                "name": "任务 Prompt 与 DRACO mini 输入及 SHA 完全一致",
                "pass": not prompt_mismatch_task_ids,
                "detail": f"mismatches={len(prompt_mismatch_task_ids)}",
            }
        )
        checks.append(
            {
                "name": "任务完整输入与 DRACO mini 逐任务 SHA 完全一致",
                "pass": not task_input_mismatch_task_ids,
                "detail": f"mismatches={len(task_input_mismatch_task_ids)}",
            }
        )

    manifest = None
    manifest_fingerprint = ""
    benchmark_preflight_counts: dict[str, int] = {}
    validation_preflight_counts = manifest_preflight_counts(validation_manifest)
    if args.manifest:
        manifest = (
            bound_compact_manifest
            if bound_compact_manifest is not None
            else json.loads(args.manifest.read_text(encoding="utf-8"))
        )
        benchmark_preflight_counts = manifest_preflight_counts(manifest)
        manifest_pass = bool(
            manifest.get("status") == "complete"
            and manifest.get("groups") == ["B2"]
            and int(number(manifest.get("rows_written"))) == args.expected_tasks
            and int(number(manifest.get("task_count"))) == args.expected_tasks
        )
        checks.append(
            {
                "name": "Manifest 完整",
                "pass": manifest_pass,
                "detail": (
                    f"status={manifest.get('status')}, groups={manifest.get('groups')}, "
                    f"rows_written={manifest.get('rows_written')}, "
                    f"task_count={manifest.get('task_count')}"
                ),
            }
        )
        tool_runtime_issues = manifest_tool_runtime_issues(manifest)
        checks.append(
            {
                "name": "Brave、web_fetch 与配置驱动的工具运行时完全冻结",
                "pass": not tool_runtime_issues,
                "detail": f"issues={len(tool_runtime_issues)}",
            }
        )
        if args.require_clean_source_now:
            repo = Path(__file__).resolve().parents[2]
            source_paths = ("scripts", "src", "configs", "pyproject.toml")
            expected_head = str((manifest.get("source_provenance") or {}).get("git_head") or "")
            try:
                current_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()
                tracked_clean = (
                    subprocess.run(
                        ["git", "diff", "--quiet", "HEAD", "--", *source_paths],
                        cwd=repo,
                        timeout=10,
                    ).returncode
                    == 0
                )
                untracked = subprocess.run(
                    [
                        "git",
                        "ls-files",
                        "--others",
                        "--exclude-standard",
                        "--",
                        *source_paths,
                    ],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()
                source_clean_now = bool(
                    expected_head
                    and current_head == expected_head
                    and tracked_clean
                    and not untracked
                )
                source_detail = (
                    f"head_matches={current_head == expected_head}, "
                    f"tracked_clean={tracked_clean}, untracked={bool(untracked)}"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                source_clean_now = False
                source_detail = f"source audit failed: {type(exc).__name__}"
            checks.append(
                {
                    "name": "审计时源码仍与实验 manifest 完全一致",
                    "pass": source_clean_now,
                    "detail": source_detail,
                }
            )
        checks.append(
            {
                "name": "Web 工具 preflight 调用已在 manifest 留痕",
                "pass": benchmark_preflight_counts.get("web_search", 0)
                >= args.external_preflight_call_count
                and benchmark_preflight_counts.get("web_fetch", 0) >= 1,
                "detail": f"calls={benchmark_preflight_counts}",
            }
        )
        compatibility = manifest.get("run_compatibility")
        fingerprints = (
            compatibility.get("fingerprints") if isinstance(compatibility, dict) else None
        )
        contracts = compatibility.get("contracts") if isinstance(compatibility, dict) else None
        manifest_fingerprint = (
            str(fingerprints.get("B2") or "") if isinstance(fingerprints, dict) else ""
        )
        manifest_contract = contracts.get("B2") if isinstance(contracts, dict) else None
        contract_fingerprint = (
            canonical_json_sha256(manifest_contract) if isinstance(manifest_contract, dict) else ""
        )
        artifact_fingerprints = {
            str(row.get("run_compatibility_fingerprint") or "") for row in [*rows, *trace_rows]
        }
        checks.append(
            {
                "name": "Manifest compatibility 合同与 result/trace 自洽",
                "pass": bool(SHA256_PATTERN.fullmatch(manifest_fingerprint))
                and contract_fingerprint == manifest_fingerprint
                and artifact_fingerprints == {manifest_fingerprint},
                "detail": (
                    f"manifest={manifest_fingerprint}, "
                    f"contract={contract_fingerprint}, "
                    f"artifact_fingerprints={sorted(artifact_fingerprints)}"
                ),
            }
        )
        runtime = (
            manifest_contract.get("resolved_llm_runtime")
            if isinstance(manifest_contract, dict)
            else None
        )
        routing = runtime.get("provider_routing") if isinstance(runtime, dict) else None
        expected_key_fingerprint = (
            f"sha256:{account_before.get('api_key_sha256')}"
            if isinstance(account_before, dict)
            else ""
        )
        routing_matches = isinstance(routing, dict) and all(
            routing.get(model) == provider for model, provider in EXPECTED_PROVIDER_ROUTING.items()
        )
        runtime_provider = runtime.get("provider") if isinstance(runtime, dict) else None
        runtime_strict = (
            runtime.get("provider_routing_strict") if isinstance(runtime, dict) else None
        )
        runtime_stream_errors = (
            runtime.get("stream_error_frames") if isinstance(runtime, dict) else None
        )
        runtime_router_metadata = (
            runtime.get("router_metadata_required") if isinstance(runtime, dict) else None
        )
        runtime_require_parameters = (
            runtime.get("require_parameters") if isinstance(runtime, dict) else None
        )
        runtime_response_cache_disabled = (
            runtime.get("response_cache_disabled") if isinstance(runtime, dict) else None
        )
        runtime_cache_namespace_enabled = (
            runtime.get("cache_namespace_enabled") if isinstance(runtime, dict) else None
        )
        runtime_cache_namespace_required = (
            runtime.get("cache_namespace_required") if isinstance(runtime, dict) else None
        )
        runtime_cache_namespace_sha256 = (
            str(runtime.get("cache_namespace_sha256") or "") if isinstance(runtime, dict) else ""
        )
        expected_cache_namespace_enabled = bool(args.expected_cache_namespace_sha256)
        runtime_base_url = runtime.get("base_url") if isinstance(runtime, dict) else None
        runtime_base_url_from_env = (
            runtime.get("base_url_from_env") if isinstance(runtime, dict) else None
        )
        runtime_proxy = runtime.get("proxy") if isinstance(runtime, dict) else None
        runtime_trust_env = runtime.get("trust_env") if isinstance(runtime, dict) else None
        runtime_ambient_proxies = (
            runtime.get("ambient_proxies") if isinstance(runtime, dict) else None
        )
        runtime_key_matches = bool(
            isinstance(runtime, dict) and runtime.get("api_key_sha256") == expected_key_fingerprint
        )
        runtime_pass = bool(
            isinstance(runtime, dict)
            and runtime.get("provider") == "openrouter"
            and runtime.get("provider_routing_strict") is True
            and runtime.get("stream_error_frames") is True
            and runtime.get("router_metadata_required") is True
            and runtime.get("require_parameters") is True
            and runtime.get("response_cache_disabled") is True
            and runtime.get("cache_namespace_enabled") is expected_cache_namespace_enabled
            and runtime.get("cache_namespace_required") is expected_cache_namespace_enabled
            and runtime_cache_namespace_sha256 == args.expected_cache_namespace_sha256
            and runtime.get("base_url") == EXPECTED_OPENROUTER_BASE_URL
            and runtime.get("base_url_from_env") is False
            and runtime.get("proxy") == ""
            and runtime.get("trust_env") is False
            and runtime.get("ambient_proxies") == {}
            and routing_matches
            and runtime_key_matches
        )
        checks.append(
            {
                "name": "OpenRouter runtime、Key 指纹与上游路由已冻结",
                "pass": runtime_pass,
                "detail": (
                    f"provider={runtime_provider}, strict={runtime_strict}, "
                    f"stream_errors={runtime_stream_errors}, "
                    f"router_metadata={runtime_router_metadata}, "
                    f"require_parameters={runtime_require_parameters}, "
                    f"response_cache_disabled={runtime_response_cache_disabled}, "
                    f"cache_namespace_enabled={runtime_cache_namespace_enabled}, "
                    f"cache_namespace_required={runtime_cache_namespace_required}, "
                    f"cache_namespace_matches="
                    f"{runtime_cache_namespace_sha256 == args.expected_cache_namespace_sha256}, "
                    f"base_url={runtime_base_url}, "
                    f"base_url_from_env={runtime_base_url_from_env}, "
                    f"proxy={runtime_proxy!r}, trust_env={runtime_trust_env}, "
                    f"ambient_proxies={runtime_ambient_proxies}, "
                    f"routing_matches={routing_matches}, "
                    f"key_matches={runtime_key_matches}"
                ),
            }
        )
        if account_before is not None and account_after is not None:
            before_captured_at = capture_timestamp(account_before.get("captured_at"))
            after_captured_at = capture_timestamp(account_after.get("captured_at"))
            manifest_started_at = number(manifest.get("started_at"))
            manifest_finished_at = number(manifest.get("finished_at"))
            checks.append(
                {
                    "name": "OpenRouter 对账窗口完整覆盖 benchmark",
                    "pass": before_captured_at is not None
                    and after_captured_at is not None
                    and manifest_started_at > 0
                    and manifest_finished_at >= manifest_started_at
                    and before_captured_at <= manifest_started_at
                    and after_captured_at >= manifest_finished_at,
                    "detail": (
                        f"before={before_captured_at}, run_start={manifest_started_at}, "
                        f"run_finish={manifest_finished_at}, after={after_captured_at}"
                    ),
                }
            )

    if validation_manifest is not None:
        validation_compatibility = validation_manifest.get("run_compatibility")
        validation_fingerprints = (
            validation_compatibility.get("fingerprints")
            if isinstance(validation_compatibility, dict)
            else None
        )
        validation_fingerprint = (
            str(validation_fingerprints.get("B2") or "")
            if isinstance(validation_fingerprints, dict)
            else ""
        )
        validation_contracts = (
            validation_compatibility.get("contracts")
            if isinstance(validation_compatibility, dict)
            else None
        )
        validation_contract = (
            validation_contracts.get("B2") if isinstance(validation_contracts, dict) else None
        )
        validation_runtime = (
            validation_contract.get("resolved_llm_runtime")
            if isinstance(validation_contract, dict)
            else None
        )
        normalized_contract_match = bool(
            isinstance(manifest_contract, dict)
            and isinstance(validation_contract, dict)
            and normalized_cross_run_contract(manifest_contract)
            == normalized_cross_run_contract(validation_contract)
        )
        validation_namespace_sha256 = (
            str(validation_runtime.get("cache_namespace_sha256") or "")
            if isinstance(validation_runtime, dict)
            else ""
        )
        validation_tool_issues = manifest_tool_runtime_issues(validation_manifest)
        full_prompt_hashes = {
            hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            for prompt in expected_prompts.values()
        }
        validation_input_disjoint = bool(
            args.validation_input_jsonl
            and validation_input_task_ids
            and not (validation_input_task_ids & set(expected_prompts))
            and not (validation_input_prompt_hashes & full_prompt_hashes)
        )
        checks.append(
            {
                "name": "canary manifest 与正式运行合同及 preflight 一致",
                "pass": validation_manifest.get("status") == "complete"
                and validation_manifest.get("groups") == ["B2"]
                and bool(SHA256_PATTERN.fullmatch(validation_fingerprint))
                and normalized_contract_match
                and isinstance(validation_runtime, dict)
                and validation_runtime.get("cache_namespace_enabled") is True
                and validation_runtime.get("cache_namespace_required") is True
                and bool(SHA256_PATTERN.fullmatch(validation_namespace_sha256))
                and not validation_tool_issues
                and validation_input_disjoint
                and validation_preflight_counts.get("web_search", 0)
                >= args.validation_external_preflight_call_count
                and validation_preflight_counts.get("web_fetch", 0) >= 1,
                "detail": (
                    f"fingerprint={validation_fingerprint}, "
                    f"benchmark_fingerprint={manifest_fingerprint}, "
                    f"normalized_contract_match={normalized_contract_match}, "
                    f"namespace_present="
                    f"{bool(SHA256_PATTERN.fullmatch(validation_namespace_sha256))}, "
                    f"input_disjoint={validation_input_disjoint}, "
                    f"tool_issues={len(validation_tool_issues)}, "
                    f"preflight={validation_preflight_counts}"
                ),
            }
        )

    effective_config = None
    configuration_summary = None
    if args.effective_config:
        effective_config = json.loads(args.effective_config.read_text(encoding="utf-8"))
        ensemble = effective_config.get("ensemble") or {}
        runner = effective_config.get("runner") or {}
        judge = effective_config.get("judge") or {}
        tools = effective_config.get("tools") or {}
        proposers = [
            str(item.get("model"))
            for item in ensemble.get("proposers", [])
            if isinstance(item, dict)
        ]
        aggregator = (ensemble.get("aggregator") or {}).get("model")
        configuration_summary = {
            "profile_id": effective_config.get("profile_id"),
            "group": effective_config.get("group"),
            "proposers": proposers,
            "aggregator": aggregator,
            "runner": runner,
            "judge": judge,
            "tool_mode": tools.get("mode"),
        }
        config_pass = bool(
            effective_config.get("profile_id") == "opensquilla_b2_quality_first_v2"
            and effective_config.get("group") == "B2"
            and proposers == EXPECTED_PROPOSERS
            and aggregator == EXPECTED_AGGREGATOR
            and runner.get("mode") == "agent_loop"
            and int(number(runner.get("agent_max_iterations")))
            == args.expected_agent_max_iterations
            and int(number(runner.get("concurrency"))) == args.expected_concurrency
            and judge.get("model") == EXPECTED_JUDGE
            and int(number(judge.get("repeats"))) == args.expected_judge_repeats
            and int(number(judge.get("concurrency"))) == args.expected_judge_concurrency
            and tools.get("mode") == args.expected_tool_mode
        )
        checks.append(
            {
                "name": "模型映射与运行配置正确",
                "pass": config_pass,
                "detail": (
                    f"profile={effective_config.get('profile_id')}, "
                    f"proposers={proposers}, aggregator={aggregator}, "
                    f"runner={runner.get('mode')}/max_iter={runner.get('agent_max_iterations')}"
                    f"/concurrency={runner.get('concurrency')}, "
                    f"judge={judge.get('model')}/repeats={judge.get('repeats')}"
                    f"/concurrency={judge.get('concurrency')}, "
                    f"tool_mode={tools.get('mode')}"
                ),
            }
        )

        if args.reference_effective_config:
            reference_effective_config = json.loads(
                args.reference_effective_config.read_text(encoding="utf-8")
            )
            exact_config_match = effective_config == reference_effective_config
            differing_top_level_keys = sorted(
                key
                for key in set(effective_config) | set(reference_effective_config)
                if effective_config.get(key) != reference_effective_config.get(key)
            )
            checks.append(
                {
                    "name": "有效配置与本批次冻结 golden config 完全一致",
                    "pass": exact_config_match,
                    "detail": (
                        "exact_match=true"
                        if exact_config_match
                        else f"differing_top_level_keys={differing_top_level_keys}"
                    ),
                }
            )

    metrics = None
    if args.summary:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
        metrics = (summary.get("groups") or {}).get("B2") or {}
        summary_pass = bool(
            int(number(metrics.get("rows"))) == args.expected_tasks
            and int(number(metrics.get("completed"))) == args.expected_tasks
            and int(number(metrics.get("scored_rows"))) == args.expected_tasks
            and number(metrics.get("score_coverage_pct")) == 100.0
            and int(number(metrics.get("judge_errors"))) == 0
        )
        checks.append(
            {
                "name": "评分汇总完整",
                "pass": summary_pass,
                "detail": (
                    f"rows={metrics.get('rows')}, completed={metrics.get('completed')}, "
                    f"scored={metrics.get('scored_rows')}, "
                    f"coverage={metrics.get('score_coverage_pct')}%, "
                    f"judge_errors={metrics.get('judge_errors')}"
                ),
            }
        )
    for model in EXPECTED_MODELS:
        stats = generation_models.get(model)
        passed = bool(
            stats
            and stats["calls"] > 0
            and stats["billable_calls"] > 0
            and stats["cost_sources"] == {"provider_billed": stats["calls"]}
        )
        detail = "missing"
        if stats:
            detail = (
                f"calls={stats['calls']}, positive={stats['positive_cost_calls']}, "
                f"billable={stats['billable_calls']}, "
                f"sources={stats['cost_sources']}, "
                f"cost=${stats['billed_cost_usd']:.9f}"
            )
        model_checks[model] = {"pass": passed, "detail": detail}
        checks.append({"name": f"{model} 费用完整", "pass": passed, "detail": detail})

    generation_cost_decimal = sum(
        (decimal_number(call.get("billed_cost")) for call in generation_calls),
        Decimal(0),
    )
    judge_cost_decimal = sum(
        (decimal_number(call.get("billed_cost")) for call in judge_calls),
        Decimal(0),
    )
    total_cost_decimal = generation_cost_decimal + judge_cost_decimal
    generation_cost = float(generation_cost_decimal)
    judge_cost = float(judge_cost_decimal)
    total_cost = float(total_cost_decimal)
    row_cost_difference = row_accounting_cost - total_cost_decimal
    checks.append(
        {
            "name": "原始 usage 与行级 OpenRouter 成本逐分对账",
            "pass": not row_accounting_issues
            and abs(row_cost_difference)
            <= required_decimal(
                args.account_cost_tolerance_usd,
                label="account cost tolerance",
            ),
            "detail": (
                f"raw=${total_cost_decimal}, row_accounting=${row_accounting_cost}, "
                f"difference=${row_cost_difference}"
            ),
        }
    )
    external_preflight_call_count = benchmark_preflight_counts.get("web_search", 0)
    validation_external_preflight_call_count = validation_preflight_counts.get("web_search", 0)
    launcher_external_preflight_call_count = (
        external_preflight_call_count + validation_external_preflight_call_count
    )
    checks.append(
        {
            "name": "外部付费工具成本边界已显式保留",
            "pass": external_preflight_call_count >= 1
            and validation_external_preflight_call_count >= 0,
            "detail": (
                f"benchmark_preflight={external_preflight_call_count}, "
                f"launcher_preflight={launcher_external_preflight_call_count}, "
                f"observed_web_search={observed_web_search_invocations}, "
                f"unpriced_call_upper_bound={external_unpriced_call_upper_bound}, "
                "USD=unknown"
            ),
        }
    )
    ensemble_calls_per_iteration = len(EXPECTED_PROPOSERS) + 1
    task_summaries = []
    for row in b2_rows:
        llm_request_count = int(number(row.get("llm_request_count")))
        task_summaries.append(
            {
                "task_id": str(row.get("task_id") or ""),
                "llm_request_count": llm_request_count,
                "ensemble_iterations": (
                    llm_request_count / ensemble_calls_per_iteration
                    if ensemble_calls_per_iteration
                    else 0.0
                ),
                "tool_call_count": int(number(row.get("total_tool_call_count"))),
                "quality_total": number(row.get("quality_total")),
                "generation_cost_usd": number(row.get("generation_attempt_total_billed_cost")),
                "total_elapsed_ms": int(number(row.get("total_elapsed_ms"))),
            }
        )
    task_summaries.sort(key=lambda item: item["task_id"])
    iteration_counts = [item["ensemble_iterations"] for item in task_summaries]
    configured_judge_repeats = int(
        number((configuration_summary or {}).get("judge", {}).get("repeats"))
    )
    judge_repeats = configured_judge_repeats or args.expected_judge_repeats
    judge_unique_criterion_count = (
        judge_judgment_count // judge_repeats if judge_repeats else judge_judgment_count
    )
    judge_baseline_call_count = judge_judgment_count
    account_difference = None
    validation_account_delta = None
    launcher_account_delta = None
    if args.account_usage_delta_usd is not None:
        account_difference_decimal = total_cost_decimal - decimal_number(
            args.account_usage_delta_usd
        )
        account_difference = float(account_difference_decimal)
        checks.append(
            {
                "name": "OpenRouter 账户成本对账",
                "pass": abs(account_difference) <= args.account_cost_tolerance_usd,
                "detail": (
                    f"recorded=${total_cost:.9f}, "
                    f"account_delta=${args.account_usage_delta_usd:.9f}, "
                    f"difference=${account_difference:.9f}, "
                    f"tolerance=${args.account_cost_tolerance_usd:.9f}"
                ),
            }
        )
        settlement = (account_after or {}).get("settlement")
        settlement_matches = False
        settlement_match_detail = "missing settlement evidence"
        if isinstance(settlement, dict):
            try:
                settlement_expected = required_decimal(
                    settlement.get("expected_recorded_cost_usd"),
                    label="settlement expected recorded cost",
                )
                settlement_observed = required_decimal(
                    settlement.get("observed_usage_delta_usd"),
                    label="settlement observed usage delta",
                )
                tolerance = required_decimal(
                    args.account_cost_tolerance_usd,
                    label="account cost tolerance",
                )
                settlement_matches = bool(
                    abs(settlement_expected - total_cost_decimal) <= tolerance
                    and abs(settlement_observed - decimal_number(args.account_usage_delta_usd))
                    <= tolerance
                )
                settlement_match_detail = (
                    f"expected={settlement_expected}, raw={total_cost_decimal}, "
                    f"observed={settlement_observed}, "
                    f"snapshot_delta={args.account_usage_delta_usd}"
                )
            except ValueError as exc:
                settlement_match_detail = str(exc)
        checks.append(
            {
                "name": "结算证据与原始 usage、最终账户差值一致",
                "pass": settlement_matches,
                "detail": settlement_match_detail,
            }
        )
    if args.account_byok_usage_delta_usd is not None:
        byok_delta = decimal_number(args.account_byok_usage_delta_usd)
        checks.append(
            {
                "name": "OpenRouter BYOK 用量为零",
                "pass": byok_delta == 0,
                "detail": f"byok_usage_delta=${byok_delta}",
            }
        )
    if validation_account_before is not None and validation_account_after is not None:
        validation_account_delta = validation_after_usage - validation_before_usage
        if args.account_usage_delta_usd is not None:
            launcher_account_delta = validation_account_delta + decimal_number(
                args.account_usage_delta_usd
            )

    report = {
        "pass": all(check["pass"] for check in checks),
        "source_jsonl": str(args.result_jsonl),
        "expected_tasks": args.expected_tasks,
        "rows": len(rows),
        "unique_tasks": len(task_counts),
        "completed_tasks": completed,
        "duplicate_tasks": duplicates,
        "usage_unknown_count": usage_unknown,
        "trace_jsonl": str(args.trace_jsonl),
        "trace_rows": len(trace_rows),
        "result_evidence_schema": RESULT_EVIDENCE_SCHEMA,
        "trace_integrity_issues": trace_integrity_issues,
        "expected_input_jsonl": (
            str(args.expected_input_jsonl) if args.expected_input_jsonl else None
        ),
        "missing_task_ids": missing_task_ids,
        "unexpected_task_ids": unexpected_task_ids,
        "prompt_mismatch_task_ids": prompt_mismatch_task_ids,
        "task_input_mismatch_task_ids": task_input_mismatch_task_ids,
        "manifest": str(args.manifest) if args.manifest else None,
        "effective_config": str(args.effective_config) if args.effective_config else None,
        "reference_effective_config": (
            str(args.reference_effective_config) if args.reference_effective_config else None
        ),
        "configuration_summary": configuration_summary,
        "summary": str(args.summary) if args.summary else None,
        "metrics": metrics,
        "generation_call_count": len(generation_calls),
        "judge_call_count": len(judge_calls),
        "judge_judgment_count": judge_judgment_count,
        "judge_unique_criterion_count": judge_unique_criterion_count,
        "judge_repeats": judge_repeats,
        "judge_baseline_call_count": judge_baseline_call_count,
        "judge_retry_call_count": max(0, len(judge_calls) - judge_baseline_call_count),
        "generation_billed_cost_usd": generation_cost,
        "judge_billed_cost_usd": judge_cost,
        "total_billed_cost_usd": total_cost,
        "openrouter_llm_judge_cost_exact": not generation_usage_issues
        and not judge_usage_issues
        and not row_accounting_issues,
        "openrouter_llm_judge_recorded_cost_usd": total_cost,
        "all_provider_cost_exact": False,
        "all_provider_total_cost_usd": None,
        "all_provider_recorded_cost_lower_bound_usd": total_cost,
        "external_paid_tool_cost_usd": None,
        "external_preflight_call_count": external_preflight_call_count,
        "validation_external_preflight_call_count": (validation_external_preflight_call_count),
        "launcher_external_preflight_call_count": (launcher_external_preflight_call_count),
        "observed_web_search_invocations": observed_web_search_invocations,
        "observed_web_fetch_invocations": observed_web_fetch_invocations,
        "external_unpriced_tool_call_count_upper_bound": (external_unpriced_call_upper_bound),
        "average_generation_cost_per_task_usd": round(generation_cost / args.expected_tasks, 9),
        "average_judge_cost_per_task_usd": round(judge_cost / args.expected_tasks, 9),
        "average_total_cost_per_task_usd": round(total_cost / args.expected_tasks, 9),
        "ensemble_calls_per_iteration": ensemble_calls_per_iteration,
        "average_llm_requests_per_task": round(len(generation_calls) / args.expected_tasks, 6),
        "average_ensemble_iterations_per_task": round(
            len(generation_calls) / ensemble_calls_per_iteration / args.expected_tasks, 6
        ),
        "min_ensemble_iterations": min(iteration_counts, default=0.0),
        "max_ensemble_iterations": max(iteration_counts, default=0.0),
        "multi_iteration_tasks": sum(value > 1 for value in iteration_counts),
        "total_tool_calls": sum(item["tool_call_count"] for item in task_summaries),
        "average_tool_calls_per_task": round(
            sum(item["tool_call_count"] for item in task_summaries) / args.expected_tasks,
            6,
        ),
        "task_summaries": task_summaries,
        "judge_cost_share_pct": round(100.0 * judge_cost / total_cost, 6) if total_cost else 0.0,
        "account_usage_delta_usd": (
            float(args.account_usage_delta_usd)
            if args.account_usage_delta_usd is not None
            else None
        ),
        "account_byok_usage_delta_usd": (
            float(args.account_byok_usage_delta_usd)
            if args.account_byok_usage_delta_usd is not None
            else None
        ),
        "account_cost_difference_usd": account_difference,
        "account_cost_tolerance_usd": args.account_cost_tolerance_usd,
        "account_before": str(args.account_before) if args.account_before else None,
        "account_after": str(args.account_after) if args.account_after else None,
        "validation_account_before": (
            str(args.validation_account_before) if args.validation_account_before else None
        ),
        "validation_account_after": (
            str(args.validation_account_after) if args.validation_account_after else None
        ),
        "validation_account_usage_delta_usd": (
            float(validation_account_delta) if validation_account_delta is not None else None
        ),
        "launcher_account_usage_delta_usd": (
            float(launcher_account_delta) if launcher_account_delta is not None else None
        ),
        "generation_models": generation_models,
        "judge_models": judge_models,
        "generation_response_models": sorted(generation_response_models),
        "judge_response_models": sorted(judge_response_models),
        "generation_usage_issues": generation_usage_issues,
        "generation_protocol_issues": generation_protocol_issues,
        "judge_usage_issues": judge_usage_issues,
        "judge_protocol_issues": judge_protocol_issues,
        "tool_trace_issues": tool_trace_issues,
        "successful_tool_names": dict(sorted(successful_tool_names.items())),
        "selected_successful_tool_names": dict(sorted(selected_successful_tool_names.items())),
        "selected_failed_tool_names": dict(sorted(selected_failed_tool_names.items())),
        "selected_tool_failure_rate": selected_tool_failure_rate,
        "max_selected_tool_failure_rate": args.max_selected_tool_failure_rate,
        "selected_tool_infrastructure_issues": selected_tool_infrastructure_issues,
        "failed_tool_names": dict(sorted(failed_tool_names.items())),
        "tool_error_reasons": dict(sorted(tool_error_reasons.items())),
        "required_observed_tools": sorted(required_observed_tools),
        "expected_cache_namespace_sha256": args.expected_cache_namespace_sha256,
        "row_accounting_issues": row_accounting_issues,
        "model_checks": model_checks,
        "checks": checks,
    }

    output_json = args.output_json or args.result_jsonl.with_name("cost-audit.json")
    output_md = args.output_md or args.result_jsonl.with_name("EXPERIMENT_RESULTS.md")
    report["output_json"] = str(output_json)
    report["output_md"] = str(output_md)
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
