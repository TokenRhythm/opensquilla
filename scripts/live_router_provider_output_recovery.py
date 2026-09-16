#!/usr/bin/env python3
"""Live router-enabled provider output recovery evidence.

Opt-in maintainer script. It uses OpenRouter and temporary OpenSquilla state to
capture evidence for two provider-output failure modes:

* provider output stopped by the length cap
* large-input reasoning-only responses with no visible text
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.env import load_env  # noqa: E402
from scripts.smoke_v4_phase3_router import (  # noqa: E402
    _live_tier_model_map,
    _read_turn_call_records,
    _write_live_gateway_config,
)


def _read_jsonl_records(log_dir: Path, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob(f"{prefix}-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _turn_records(records: list[dict[str, Any]], session_key: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("session_key") == session_key]


def _response_usage(record: dict[str, Any]) -> dict[str, Any]:
    return ((record.get("payload") or {}).get("usage") or {})


def _finish_reasons(records: list[dict[str, Any]]) -> list[str | None]:
    reasons: list[str | None] = []
    for record in records:
        if record.get("kind") == "llm_response":
            usage = _response_usage(record)
            reasons.append(usage.get("stop_reason"))
    return reasons


def _llm_request_configs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") != "llm_request":
            continue
        payload = record.get("payload") or {}
        config = payload.get("config") or {}
        configs.append(config)
    return configs


def _decision_steps(decisions: list[dict[str, Any]], session_key: str) -> list[dict[str, Any]]:
    for row in reversed(decisions):
        if row.get("session_key") == session_key:
            steps = row.get("pipeline_steps")
            return steps if isinstance(steps, list) else []
    return []


def _router_step(decisions: list[dict[str, Any]], session_key: str) -> dict[str, Any]:
    for step in _decision_steps(decisions, session_key):
        if isinstance(step, dict) and step.get("step_name") == "squilla_router":
            return step
    return {}


def _classify_direct_response(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    reasoning_content = message.get("reasoning_content") or message.get("reasoning")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
    visible = content if isinstance(content, str) else ""
    reasoning = reasoning_content if isinstance(reasoning_content, str) else ""
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    if finish_reason == "length":
        kind = "length_capped"
    elif visible.strip():
        kind = "ok"
    elif reasoning.strip() or reasoning_tokens > 0:
        kind = "reasoning_only"
    else:
        kind = "malformed_empty"
    return {
        "kind": kind,
        "finish_reason": finish_reason,
        "content_len": len(visible),
        "reasoning_len": len(reasoning),
        "usage": usage,
    }


def _openrouter_chat(
    *,
    model: str,
    message: str,
    max_tokens: int,
    thinking: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "max_tokens": max_tokens,
        "max_completion_tokens": max_tokens,
        "temperature": 0,
    }
    if thinking:
        payload["reasoning"] = {"effort": "high"}
    else:
        payload["reasoning"] = {"enabled": False}
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "content-type": "application/json",
            "http-referer": "https://github.com/TokenRhythm/opensquilla",
            "x-title": "OpenSquilla provider output recovery live evidence",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _length_prompt() -> str:
    return (
        "Do not call tools. Answer directly in plain text. Write exactly 18 "
        "numbered lines. Each line must be one complete sentence with marker "
        "PROVIDER_OUTPUT_RECOVERY_LENGTH and at least 14 words. Do not summarize "
        "and do not stop early."
    )


def _large_reasoning_prompt(chars: int) -> str:
    line = (
        "large-context-provider-output-recovery marker data: "
        "alpha beta gamma delta epsilon zeta eta theta iota kappa.\n"
    )
    filler = (line * max(1, chars // len(line) + 1))[:chars]
    return (
        "Read the following large material. Reply with exactly one short visible "
        "sentence containing marker LARGE_REASONING_VISIBLE. Do not call tools.\n\n"
        f"{filler}\n\n"
        "Final instruction: output only the visible sentence now."
    )


def _router_sanity_prompt() -> str:
    return "Compare PostgreSQL and MySQL replication tradeoffs in three concise bullets."


def _write_config(
    path: Path,
    *,
    live_model: str,
    max_tokens: int,
) -> None:
    _write_live_gateway_config(path, live_model)
    text = path.read_text(encoding="utf-8")
    text = text.replace("max_tokens = 192", f"max_tokens = {max_tokens}", 1)
    path.write_text(text, encoding="utf-8")


def _runtime_router_available() -> dict[str, Any]:
    try:
        from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

        V4Phase3Strategy(require_router_runtime=True)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - preflight report
        return {"ok": False, "error": str(exc)}


def _run_cli_case(
    *,
    tmp_path: Path,
    case_id: str,
    message: str,
    live_model: str,
    max_tokens: int,
    timeout_seconds: float,
    length_capped_continuations: int | None,
    max_provider_retries: int = 3,
    thinking: str | None = None,
) -> dict[str, Any]:
    config_path = tmp_path / f"{case_id}-config.toml"
    log_dir = tmp_path / f"{case_id}-logs"
    turn_log_dir = tmp_path / f"{case_id}-turn-calls"
    state_dir = tmp_path / f"{case_id}-state"
    workspace = tmp_path / f"{case_id}-workspace"
    scratch = tmp_path / f"{case_id}-scratch"
    session_db = tmp_path / f"{case_id}-sessions.sqlite"
    _write_config(config_path, live_model=live_model, max_tokens=max_tokens)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    env["OPENSQUILLA_LOG_DIR"] = str(log_dir)
    env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)
    env["OPENSQUILLA_SANDBOX_SANDBOX"] = "false"
    env["OPENSQUILLA_SANDBOX_SECURITY_GRADING"] = "false"
    env["OPENSQUILLA_TOOL_PROFILE"] = "channel_default"
    env.pop("OPENSQUILLA_LLM_THINKING", None)

    session_id = f"live-provider-output-{case_id}-{int(time.time() * 1000)}"
    cmd = [
        sys.executable,
        "-m",
        "opensquilla.cli.main",
        "agent",
        "--message",
        message,
        "--json",
        "--session-id",
        session_id,
        "--session-db-path",
        str(session_db),
        "--workspace",
        str(workspace),
        "--scratch-dir",
        str(scratch),
        "--permissions",
        "restricted",
        "--no-memory-capture",
        "--timeout",
        str(int(timeout_seconds)),
        "--request-timeout-seconds",
        str(int(timeout_seconds)),
        "--max-provider-retries",
        str(max_provider_retries),
    ]
    if length_capped_continuations is not None:
        cmd.extend(["--length-capped-continuations", str(length_capped_continuations)])
    if thinking:
        cmd.extend(["--thinking", thinking])

    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds + 30,
        check=False,
    )
    stdout = proc.stdout.strip()
    result: dict[str, Any]
    try:
        result = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except json.JSONDecodeError:
        result = {"raw_stdout_tail": stdout[-2000:]}
    session_key = str(result.get("session_key") or f"agent:main:{session_id}")
    turn_records = _turn_records(_read_turn_call_records(turn_log_dir), session_key)
    decisions = _read_jsonl_records(log_dir, "decisions")
    return {
        "case_id": case_id,
        "session_key": session_key,
        "returncode": proc.returncode,
        "result": result,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-4000:],
        "turn_records": turn_records,
        "decisions": [row for row in decisions if row.get("session_key") == session_key],
        "router_step": _router_step(decisions, session_key),
        "request_configs": _llm_request_configs(turn_records),
        "finish_reasons": _finish_reasons(turn_records),
        "llm_response_count": sum(1 for r in turn_records if r.get("kind") == "llm_response"),
        "llm_request_count": sum(1 for r in turn_records if r.get("kind") == "llm_request"),
    }


def _summarize_case(raw: dict[str, Any]) -> dict[str, Any]:
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    router_step = raw.get("router_step") if isinstance(raw.get("router_step"), dict) else {}
    request_configs = raw.get("request_configs") or []
    return {
        "case_id": raw.get("case_id"),
        "session_key": raw.get("session_key"),
        "returncode": raw.get("returncode"),
        "status": result.get("status"),
        "text_len": len(str(result.get("text") or "")),
        "errors": errors,
        "routing": result.get("routing"),
        "router_step": router_step,
        "request_models": [
            record.get("model")
            for record in raw.get("turn_records", [])
            if record.get("kind") == "llm_request"
        ],
        "request_thinking": [config.get("thinking") for config in request_configs],
        "request_thinking_levels": [config.get("thinking_level") for config in request_configs],
        "finish_reasons": raw.get("finish_reasons"),
        "llm_request_count": raw.get("llm_request_count"),
        "llm_response_count": raw.get("llm_response_count"),
        "stderr_tail": str(raw.get("stderr_tail") or "")[-1200:],
    }


def _error_codes(summary: dict[str, Any]) -> set[str]:
    return {
        str(error.get("code"))
        for error in summary.get("errors", [])
        if isinstance(error, dict) and error.get("code")
    }


def run_live(
    *,
    mode: str,
    timeout_seconds: float,
    length_capped_continuations: int,
    large_chars: int,
    max_tokens: int,
    large_max_tokens: int,
    reasoning_model: str,
) -> dict[str, Any]:
    load_env(REPO_ROOT)
    if not os.environ.get("OPENROUTER_API_KEY"):
        return {"ok": False, "error": "OPENROUTER_API_KEY is required"}

    router_runtime = _runtime_router_available()
    if not router_runtime.get("ok"):
        return {
            "ok": False,
            "error": "router runtime unavailable",
            "router_runtime": router_runtime,
            "hint": "Run `uv sync --extra dev --extra recommended` before live router checks.",
        }

    tier_models = _live_tier_model_map("")
    direct_length: dict[str, Any]
    direct_reasoning: dict[str, Any]
    try:
        direct_length = _classify_direct_response(
            _openrouter_chat(
                model=tier_models["c1"],
                message=_length_prompt(),
                max_tokens=min(max_tokens, 96),
                thinking=False,
                timeout_seconds=timeout_seconds,
            )
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        direct_length = {"kind": "api_error", "error": str(exc)}
    try:
        direct_reasoning = _classify_direct_response(
            _openrouter_chat(
                model=reasoning_model,
                message=_large_reasoning_prompt(large_chars),
                max_tokens=large_max_tokens,
                thinking=True,
                timeout_seconds=timeout_seconds,
            )
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        direct_reasoning = {"kind": "api_error", "error": str(exc)}

    with tempfile.TemporaryDirectory(
        prefix="opensquilla-live-provider-output-",
        ignore_cleanup_errors=True,
    ) as tmp:
        tmp_path = Path(tmp)
        sanity_raw = _run_cli_case(
            tmp_path=tmp_path,
            case_id="router-sanity",
            message=_router_sanity_prompt(),
            live_model="",
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            length_capped_continuations=length_capped_continuations,
        )
        truncation_raw = _run_cli_case(
            tmp_path=tmp_path,
            case_id=f"truncation-{mode}",
            message=_length_prompt(),
            live_model="",
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            length_capped_continuations=length_capped_continuations,
            thinking="off",
        )
        large_raw: dict[str, Any] | None = None
        if direct_reasoning.get("kind") == "reasoning_only":
            large_raw = _run_cli_case(
                tmp_path=tmp_path,
                case_id=f"large-reasoning-{mode}",
                message=_large_reasoning_prompt(large_chars),
                live_model=reasoning_model,
                max_tokens=large_max_tokens,
                timeout_seconds=timeout_seconds,
                length_capped_continuations=length_capped_continuations,
                max_provider_retries=1,
            )

    sanity = _summarize_case(sanity_raw)
    truncation = _summarize_case(truncation_raw)
    large_summary = _summarize_case(large_raw) if large_raw is not None else None

    sanity_routing = sanity.get("routing") if isinstance(sanity.get("routing"), dict) else {}
    sanity_ok = (
        sanity.get("returncode") == 0
        and sanity.get("status") == "ok"
        and (
            (sanity.get("router_step") or {}).get("routing_source") == "v4_phase3"
            or sanity_routing.get("routing_source") == "v4_phase3"
        )
    )
    truncation_errors = _error_codes(truncation)
    saw_length = "length" in (truncation.get("finish_reasons") or [])
    if mode == "reproduce":
        truncation_ok = saw_length and "provider_output_truncated" in truncation_errors
    else:
        truncation_ok = (
            saw_length
            and truncation.get("status") == "ok"
            and "provider_output_truncated" not in truncation_errors
            and int(truncation.get("llm_request_count") or 0) >= 2
        )

    large_ok = True
    large_note = "skipped_provider_drift"
    if large_summary is not None:
        thinking_values = large_summary.get("request_thinking") or []
        large_errors = _error_codes(large_summary)
        large_ok = (
            len(thinking_values) >= 2
            and thinking_values[0] is True
            and False in thinking_values[1:]
            and large_summary.get("status") == "ok"
            and "empty_response" not in large_errors
        )
        large_note = "verified_recovery"

    return {
        "ok": sanity_ok and truncation_ok and large_ok,
        "mode": mode,
        "length_capped_continuations": length_capped_continuations,
        "max_tokens": max_tokens,
        "large_max_tokens": large_max_tokens,
        "router_runtime": router_runtime,
        "tier_models": tier_models,
        "direct_calibration": {
            "length": direct_length,
            "large_reasoning": direct_reasoning,
        },
        "router_sanity": {
            "ok": sanity_ok,
            **sanity,
        },
        "truncation_case": {
            "ok": truncation_ok,
            **truncation,
        },
        "large_reasoning_case": {
            "ok": large_ok,
            "note": large_note,
            "direct_kind": direct_reasoning.get("kind"),
            **(large_summary or {}),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["reproduce", "verify"], default="verify")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--length-capped-continuations", type=int, default=None)
    parser.add_argument("--large-chars", type=int, default=140_000)
    parser.add_argument("--max-tokens", type=int, default=192)
    parser.add_argument("--large-max-tokens", type=int, default=1024)
    parser.add_argument("--reasoning-model", default="z-ai/glm-5.1")
    args = parser.parse_args()

    length_budget = args.length_capped_continuations
    if length_budget is None:
        length_budget = 1 if args.mode == "reproduce" else 3
    report = run_live(
        mode=args.mode,
        timeout_seconds=args.timeout_seconds,
        length_capped_continuations=max(1, int(length_budget)),
        large_chars=max(1_000, int(args.large_chars)),
        max_tokens=max(32, int(args.max_tokens)),
        large_max_tokens=max(32, int(args.large_max_tokens)),
        reasoning_model=str(args.reasoning_model),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
