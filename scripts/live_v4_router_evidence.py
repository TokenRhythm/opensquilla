#!/usr/bin/env python3
"""Run a small live V4 router evidence check through the OpenSquilla gateway.

The script intentionally runs only three representative turns so live evidence
is cheap but still covers model routing, thinking controls, and prompt hints.
"""

from __future__ import annotations

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

from opensquilla.env import load_env  # noqa: E402
from scripts.smoke_v4_phase3_router import (  # noqa: E402
    _free_port,
    _post_json,
    _read_turn_call_records,
    _stop_gateway,
    _usage_from_llm_responses,
    _wait_for_assistant_reply,
    _wait_for_gateway_health,
    _write_live_gateway_config,
)

CASES = [
    {
        "id": "r0_prompt_hint",
        "expected_model": "qwen/qwen3.7-flash",
        "expected_thinking": False,
        "expected_response_policy": True,
        "message": "谢谢。",
    },
    {
        "id": "r0_prompt_hint_en",
        "expected_model": "qwen/qwen3.7-flash",
        "expected_thinking": False,
        "expected_response_policy": True,
        "message": "Thanks.",
    },
    {
        "id": "r1_standard",
        "expected_model": "deepseek/deepseek-v4-flash",
        "expected_thinking": True,
        "expected_thinking_level": "medium",
        "expected_response_policy": False,
        "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
    },
    {
        "id": "r2_thinking_medium",
        "expected_model": "deepseek/deepseek-v4-pro",
        "expected_thinking": True,
        "expected_thinking_level": "medium",
        "expected_response_policy": False,
        "message": (
            "下面是一个异步服务偶发超时的日志片段，请定位可能原因并给出排查步骤："
            "连接池耗尽、慢查询、重试风暴、队列积压同时出现。"
        ),
    },
    {
        "id": "r3_thinking_high",
        "expected_model": "z-ai/glm-5.3",
        "expected_thinking": True,
        "expected_thinking_level": "high",
        "expected_response_policy": False,
        "message": (
            "请设计一个跨机房分布式任务调度系统，要求解释一致性、故障恢复和容量评估。"
        ),
    },
]

CONTEXT_CASE = {
    "id": "dialogue_context",
    "turns": [
        {
            "message": "比较 PostgreSQL 和 MySQL 在事务和索引方面的差异，用一句话回答。",
            "intent": "new_chat",
        },
        {
            "message": "继续上一轮，补充复制机制差异，用一句话回答。",
            "intent": "continue",
        },
    ],
}


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", ""))


def _last_user_message(messages: list[Any]) -> str:
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "user":
            return _message_content(message)
    return ""


def _message_roles(messages: list[Any]) -> list[str | None]:
    return [
        message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        for message in messages
    ]


def _first_record(records: list[dict[str, Any]], *, session_key: str, kind: str) -> dict[str, Any]:
    for record in records:
        if record.get("session_key") == session_key and record.get("kind") == kind:
            return record
    return {}


def main() -> int:
    load_env(REPO_ROOT)
    if not os.environ.get("OPENROUTER_API_KEY"):
        print(json.dumps({"ok": False, "error": "OPENROUTER_API_KEY is required"}))
        return 2

    port = _free_port()
    tmp_path = Path(tempfile.mkdtemp(prefix="opensquilla-router-live-evidence-"))
    config_path = tmp_path / "live-config.toml"
    turn_log_dir = tmp_path / "turn-calls"
    _write_live_gateway_config(config_path, "")

    env = os.environ.copy()
    env.pop("OPENSQUILLA_LLM_THINKING", None)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["OPENSQUILLA_STATE_DIR"] = str(tmp_path / "state")
    env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
    env["OPENSQUILLA_TOOL_PROFILE"] = "channel_default"
    env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
    env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

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
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    rows: list[dict[str, Any]] = []
    context_row: dict[str, Any] = {}
    health: dict[str, Any] | None = None
    error: str | None = None
    try:
        health, error = _wait_for_gateway_health(proc, port)
        if error is None:
            for case in CASES:
                session_key = f"live-evidence:{case['id']}:{int(time.time() * 1000)}"
                _post_json(
                    f"http://127.0.0.1:{port}/api/chat",
                    {
                        "sessionKey": session_key,
                        "message": case["message"],
                        "intent": "new_chat",
                    },
                    timeout=10.0,
                )
                assistant, _history, turn_error = _wait_for_assistant_reply(
                    port=port,
                    session_key=session_key,
                    previous_assistant_count=0,
                )
                rows.append(
                    {
                        "case_id": case["id"],
                        "session_key": session_key,
                        "expected": case,
                        "assistant_text": str((assistant or {}).get("text", "")).strip(),
                        "turn_error": turn_error,
                    }
                )
            context_session_key = (
                f"live-evidence:{CONTEXT_CASE['id']}:{int(time.time() * 1000)}"
            )
            assistant_count = 0
            context_turns: list[dict[str, Any]] = []
            for index, turn_spec in enumerate(CONTEXT_CASE["turns"], start=1):
                _post_json(
                    f"http://127.0.0.1:{port}/api/chat",
                    {
                        "sessionKey": context_session_key,
                        "message": turn_spec["message"],
                        "intent": turn_spec["intent"],
                    },
                    timeout=10.0,
                )
                assistant, history, turn_error = _wait_for_assistant_reply(
                    port=port,
                    session_key=context_session_key,
                    previous_assistant_count=assistant_count,
                )
                assistant_text = str((assistant or {}).get("text", "")).strip()
                context_turns.append(
                    {
                        "index": index,
                        "intent": turn_spec["intent"],
                        "assistant_text": assistant_text[:220],
                        "history_message_count": len((history or {}).get("messages", [])),
                        "turn_error": turn_error,
                    }
                )
                if turn_error:
                    break
                assistant_count += 1
            context_row = {
                "case_id": CONTEXT_CASE["id"],
                "session_key": context_session_key,
                "turns": context_turns,
            }
    finally:
        stdout_tail, stderr_tail = _stop_gateway(proc)
        records = _read_turn_call_records(turn_log_dir)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        session_key = row["session_key"]
        request = _first_record(records, session_key=session_key, kind="llm_request")
        response = _first_record(records, session_key=session_key, kind="llm_response")
        request_payload = request.get("payload") or {}
        response_payload = response.get("payload") or {}
        request_config = request_payload.get("config") or {}
        request_messages = request_payload.get("messages") or []
        last_user = _last_user_message(request_messages)
        usage = response_payload.get("usage") or {}
        expected = row["expected"]
        actual_model = response.get("model") or usage.get("model")
        actual_thinking = bool(request_config.get("thinking"))
        response_policy = "[RESPONSE_POLICY:" in last_user
        thinking_level = request_config.get("thinking_level")
        ok = (
            not row.get("turn_error")
            and actual_model == expected["expected_model"]
            and actual_thinking is expected["expected_thinking"]
            and response_policy is expected["expected_response_policy"]
            and (
                "expected_thinking_level" not in expected
                or thinking_level == expected["expected_thinking_level"]
            )
        )
        enriched.append(
            {
                "case_id": row["case_id"],
                "ok": ok,
                "expected_model": expected["expected_model"],
                "actual_request_model": request.get("model"),
                "actual_response_model": usage.get("model"),
                "request_thinking": request_config.get("thinking"),
                "request_thinking_level": thinking_level,
                "response_policy_in_prompt": response_policy,
                "last_user_excerpt": last_user[:220],
                "assistant_excerpt": row["assistant_text"][:220],
                "usage": {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "reasoning_tokens": usage.get("reasoning_tokens"),
                    "cached_tokens": usage.get("cached_tokens"),
                    "billed_cost": usage.get("billed_cost"),
                },
                "turn_error": row.get("turn_error"),
            }
        )

    context_summary: dict[str, Any] = {}
    if context_row:
        context_requests = [
            record
            for record in records
            if record.get("session_key") == context_row["session_key"]
            and record.get("kind") == "llm_request"
        ]
        second_request = context_requests[-1] if len(context_requests) >= 2 else {}
        second_messages = (second_request.get("payload") or {}).get("messages") or []
        roles = _message_roles(second_messages)
        previous_roles = roles[:-1]
        context_summary = {
            **context_row,
            "ok": (
                len(context_requests) >= 2
                and "user" in previous_roles
                and "assistant" in previous_roles
                and not any(turn.get("turn_error") for turn in context_row.get("turns", []))
            ),
            "llm_request_count": len(context_requests),
            "second_request_model": second_request.get("model"),
            "second_request_message_count": len(roles),
            "second_request_roles_tail": roles[-6:],
            "second_request_has_prev_user": "user" in previous_roles,
            "second_request_has_prev_assistant": "assistant" in previous_roles,
        }

    llm_responses = [record for record in records if record.get("kind") == "llm_response"]
    report = {
        "ok": (
            error is None
            and bool(enriched)
            and all(row["ok"] for row in enriched)
            and bool(context_summary.get("ok"))
        ),
        "health": health or {},
        "config_path": str(config_path),
        "turn_log_dir": str(turn_log_dir),
        "turn_log_records": len(records),
        "cases": enriched,
        "dialogue_context": context_summary,
        "usage_from_turn_logs": _usage_from_llm_responses(llm_responses),
        "error": error,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
