#!/usr/bin/env python3
"""Measure delegated-agent propensity on a small synthetic case set.

The evaluator intentionally stops after the first provider response. It tests
the parent model's decision to emit ``delegate_task`` calls, not child task
quality or end-to-end benchmark success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for path in (REPO_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from opensquilla.engine.subagent_delegation import (  # noqa: E402
    DELEGATION_OWNERSHIP_PROMPT,
)
from opensquilla.env import load_env  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from opensquilla.provider.types import (  # noqa: E402
    ToolDefinition,
    ToolInputSchema,
)

DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "subagent_delegation" / "cases.json"
BASE_SYSTEM_PROMPT = "You are a capable assistant. Complete the user's request accurately."
BASELINE_TOOL_DESCRIPTION = (
    "Delegate one bounded task to a persistent child agent. Foreground is the default and "
    "returns the child result directly; set background=true only when concurrency is useful. "
    "The task must be self-contained and preserve exact output constraints."
)
BASELINE_TASK_DESCRIPTION = (
    "Initial task / user message for the session. Include the complete delegated "
    "instruction, required output format, and exact-reply constraints."
)


def _candidate_tool() -> ToolDefinition:
    # Importing the built-in registers the authoritative live schema.
    from opensquilla.tools.builtin import delegation as _delegation  # noqa: F401, PLC0415
    from opensquilla.tools.registry import get_default_registry  # noqa: PLC0415

    registered = get_default_registry().get("delegate_task")
    if registered is None:
        raise RuntimeError("delegate_task is not registered")
    return ToolDefinition(
        name=registered.spec.name,
        description=registered.spec.description,
        input_schema=ToolInputSchema(
            properties=registered.spec.parameters,
            required=registered.spec.required,
        ),
    )


def _baseline_tool() -> ToolDefinition:
    return ToolDefinition(
        name="delegate_task",
        description=BASELINE_TOOL_DESCRIPTION,
        input_schema=ToolInputSchema(
            properties={
                "task": {"type": "string", "description": BASELINE_TASK_DESCRIPTION},
                "task_key": {
                    "type": "string",
                    "description": "Stable semantic key for this delegated work.",
                },
                "background": {"type": "boolean", "default": False},
                "agent": {
                    "type": "string",
                    "enum": ["inherit", "worker", "explorer", "researcher", "reviewer"],
                    "default": "inherit",
                },
                "title": {"type": "string"},
            },
            required=["task", "task_key"],
        ),
    )


def _system_prompt(variant: str) -> str:
    if variant == "baseline":
        return BASE_SYSTEM_PROMPT
    return (
        f"{BASE_SYSTEM_PROMPT}\n\n## Subagent Delegation Policy\n\n"
        f"{DELEGATION_OWNERSHIP_PROMPT}"
    )


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("delegation dataset must be a non-empty JSON array")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise ValueError("every delegation case must be an object")
        case = dict(raw)
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case id: {case_id!r}")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError(f"case {case_id!r} has no prompt")
        if not isinstance(case.get("expected_delegation"), bool):
            raise ValueError(f"case {case_id!r} has no boolean expected_delegation")
        seen.add(case_id)
        cases.append(case)
    return cases


def _minimum_task_contract(task: object) -> bool:
    if not isinstance(task, str):
        return False
    return "Goal" in task and "Output format" in task


async def _run_case(
    client: httpx.AsyncClient,
    case: Mapping[str, Any],
    *,
    variant: str,
    endpoint: str,
    model: str,
    timeout: float,
    max_tokens: int,
) -> dict[str, Any]:
    tool = _baseline_tool() if variant == "baseline" else _candidate_tool()
    tool_calls: list[dict[str, Any]] = []
    text_response = False
    assistant_text = ""
    reasoning_text = ""
    usage: Mapping[str, Any] = {}
    response_model = ""
    finish_reason = ""
    error = ""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            response = await client.post(
                endpoint,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _system_prompt(variant)},
                        {"role": "user", "content": str(case["prompt"])},
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.input_schema.model_dump(
                                    mode="json",
                                    by_alias=True,
                                    exclude_none=True,
                                ),
                            },
                        },
                    ],
                    "tool_choice": "auto",
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=timeout,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("provider response is not an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ValueError("provider response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ValueError("provider response has no assistant message")
        assistant_text = str(message.get("content") or "").strip()
        reasoning_text = str(message.get("reasoning") or "").strip()
        text_response = bool(assistant_text)
        finish_reason = str(choices[0].get("finish_reason") or "")
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, Mapping):
                    continue
                arguments: dict[str, Any] = {}
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        parsed = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, Mapping):
                        arguments = dict(parsed)
                tool_calls.append(
                    {"name": str(function.get("name") or ""), "arguments": arguments}
                )
        raw_usage = payload.get("usage")
        usage = raw_usage if isinstance(raw_usage, Mapping) else {}
        response_model = str(payload.get("model") or "")
    except (TimeoutError, httpx.TimeoutException):
        error = f"TimeoutError: provider response exceeded {timeout:.1f}s"
    except Exception as exc:  # noqa: BLE001 - report a bounded live-eval failure
        error = f"{type(exc).__name__}: {exc}"

    delegate_calls = [call for call in tool_calls if call["name"] == "delegate_task"]
    minimum = max(0, int(case.get("minimum_delegate_calls") or 0))
    return {
        "id": str(case["id"]),
        "category": str(case.get("category") or ""),
        "expected_delegation": bool(case["expected_delegation"]),
        "minimum_delegate_calls": minimum,
        "delegated": bool(delegate_calls),
        "delegate_call_count": len(delegate_calls),
        "minimum_fanout_satisfied": len(delegate_calls) >= minimum,
        "minimum_task_contract_count": sum(
            _minimum_task_contract(call["arguments"].get("task")) for call in delegate_calls
        ),
        "text_response": text_response,
        "assistant_text": assistant_text,
        "reasoning_text": reasoning_text,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "model": response_model,
        },
        "finish_reason": finish_reason,
        "error": error,
    }


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("error")]
    expected = [row for row in valid if row.get("expected_delegation")]
    simple = [row for row in valid if not row.get("expected_delegation")]
    true_positive = sum(bool(row.get("delegated")) for row in expected)
    true_negative = sum(not bool(row.get("delegated")) for row in simple)
    delegate_calls = sum(int(row.get("delegate_call_count") or 0) for row in valid)
    contract_calls = sum(int(row.get("minimum_task_contract_count") or 0) for row in valid)
    fanout = [row for row in expected if int(row.get("minimum_delegate_calls") or 0) > 0]
    recalls = true_positive / len(expected) if expected else 0.0
    simple_direct_rate = true_negative / len(simple) if simple else 0.0
    return {
        "case_count": len(rows),
        "valid_case_count": len(valid),
        "error_count": len(rows) - len(valid),
        "length_limited_count": sum(
            str(row.get("finish_reason") or "") == "length" for row in valid
        ),
        "decomposable_delegation_rate": recalls,
        "simple_direct_rate": simple_direct_rate,
        "balanced_accuracy": (recalls + simple_direct_rate) / 2,
        "minimum_fanout_satisfied_rate": (
            sum(bool(row.get("minimum_fanout_satisfied")) for row in fanout) / len(fanout)
            if fanout
            else 0.0
        ),
        "delegate_call_count": delegate_calls,
        "minimum_task_contract_rate": (
            contract_calls / delegate_calls if delegate_calls else 0.0
        ),
        "median_latency_ms": (
            int(statistics.median(int(row.get("latency_ms") or 0) for row in valid))
            if valid
            else 0
        ),
        "input_tokens": sum(
            int((row.get("usage") or {}).get("input_tokens") or 0) for row in valid
        ),
        "output_tokens": sum(
            int((row.get("usage") or {}).get("output_tokens") or 0) for row in valid
        ),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_env(REPO_ROOT)
    spec = get_provider_spec(args.provider)
    api_key = os.environ.get(spec.env_key, "").strip()
    if spec.requires_api_key() and not api_key:
        raise RuntimeError(f"missing credential: {spec.env_key}")
    if spec.backend != "openai_compat":
        raise RuntimeError("delegation evaluator requires an OpenAI-compatible provider")
    endpoint = f"{spec.default_base_url.rstrip('/')}/chat/completions"
    cases = _load_cases(Path(args.dataset))
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case["id"] in selected]
        missing = selected - {str(case["id"]) for case in cases}
        if missing:
            raise ValueError(f"unknown delegation case ids: {sorted(missing)}")
    if args.max_cases is not None:
        cases = cases[: args.max_cases]
    variants = ("baseline", "candidate") if args.variant == "both" else (args.variant,)
    reports: dict[str, Any] = {}
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        follow_redirects=False,
    ) as client:
        for variant in variants:
            rows = []
            for case in cases:
                rows.append(
                    await _run_case(
                        client,
                        case,
                        variant=variant,
                        endpoint=endpoint,
                        model=args.model,
                        timeout=args.timeout,
                        max_tokens=args.max_tokens,
                    )
                )
            reports[variant] = {"metrics": _metrics(rows), "cases": rows}
    return {
        "provider": args.provider,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "dataset": str(Path(args.dataset).resolve()),
        "variants": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model", default="z-ai/glm-5.2")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--variant", choices=("baseline", "candidate", "both"), default="both")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-tokens", type=int, default=3200)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return int(any(v["metrics"]["error_count"] for v in report["variants"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
