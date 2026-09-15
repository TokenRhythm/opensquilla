#!/usr/bin/env python3
"""Opt-in execution-identity probes through an existing functional relay.

Start live_tokenrhythm_budget.py in functional mode in a separate process with
TOKENRHYTHM_API_KEY. This process must NOT inherit that key. Supply the relay's
private ready file and an OS-temporary report path. No request is sent without
--enable-live. The relay retains the physical HTTP ledger; reports contain only
synthetic observations. Natural-language matches still require human review.

This driver exercises the real Agent/provider path, not Gateway routing RPCs.
It does not claim that these probes replace the full Gateway acceptance matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import structlog

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import AgentConfig
from opensquilla.identity.prompt import assemble_system_prompt
from opensquilla.identity.types import AgentProfile
from opensquilla.provider import types as provider_types
from opensquilla.provider.preset_registry import get_preset
from opensquilla.provider.selector import ProviderConfig, _build_provider
from opensquilla.provider.types import DoneEvent, ErrorEvent
from opensquilla.scheduler.delivery import strip_reply_directives
from scripts.live_harness_security import (
    registry_endpoint,
    require_temporary_report_path,
    write_safe_report,
)
from scripts.live_tokenrhythm_budget import FunctionalRequestLog
from scripts.live_tokenrhythm_transport import install_from_env

PROVIDER = "tokenrhythm"
QUESTIONS = {
    "zh": "这次回答实际使用的底层模型是什么？请给出准确的部署模型 ID。",
    "en": "Which underlying model is executing this response? Give its exact deployment model ID.",
    "provider": "Which provider is serving this response?",
    "product": "你是谁？",
    "task": "Return only the integer result of 19 + 23.",
    "json": 'Return only a JSON object with the key "sum" and the integer result of 19 + 23.',
}


def preset_models() -> list[str]:
    preset = get_preset(PROVIDER)
    if preset is None:
        raise ValueError("missing_tokenrhythm_preset")
    tiers = preset.tier_defaults()
    return list(dict.fromkeys(str(tiers[tier]["model"]) for tier in ("c0", "c1", "c2", "c3")))


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def answer_matches(answer: str, model: str, question: str, aliases: dict[str, list[str]]) -> bool:
    answer = strip_reply_directives(answer) or ""
    if question == "product":
        return "opensquilla" in normalized(answer)
    if question == "provider":
        return "tokenrhythm" in normalized(answer)
    if question == "task":
        return answer.strip() == "42"
    if question == "json":
        try:
            return json.loads(answer) == {"sum": 42}
        except ValueError:
            return False
    folded = normalized(answer)
    matches = [
        alias for alias in aliases[model] if normalized(alias) and normalized(alias) in folded
    ]
    other_models = [
        other
        for other, names in aliases.items()
        if other != model and any(normalized(name) and normalized(name) in folded for name in names)
    ]
    return bool(matches) and not other_models


class CaptureProvider:
    """Observe actual physical calls without modifying provider configuration."""

    def __init__(self, provider: Any, model: str) -> None:
        self.provider = provider
        self.model = model
        self.requests: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def chat(self, messages, tools=None, config=None):
        facts = getattr(config, "execution_identity", None)
        fragments = []
        for message in messages:
            if isinstance(message.content, str):
                span = getattr(message, "execution_identity_span", None)
                if span is not None:
                    fragments.append(message.content[slice(*span)])
            else:
                for block in message.content:
                    span = getattr(block, "execution_identity_span", None)
                    if span is not None:
                        fragments.append(block.text[slice(*span)])
        row: dict[str, Any] = {
            "model": self.model,
            "facts": fragments,
            "usage": None,
            "first_text_ms": None,
            "total_ms": None,
            "error_code": None,
        }
        self.requests.append(row)
        started = time.monotonic()
        async for event in self.provider.chat(messages, tools=tools, config=config):
            if getattr(event, "kind", "") == "text_delta" and row["first_text_ms"] is None:
                row["first_text_ms"] = round((time.monotonic() - started) * 1000, 2)
            if isinstance(event, DoneEvent):
                row["usage"] = {
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "reported_model": event.model,
                    # A parser default of zero is not proof that upstream reported cache usage.
                    "cache_read_observed": None,
                    "parsed_cached_tokens": event.cached_tokens,
                    "billed_usd": event.billed_cost
                    if event.cost_source == "provider_billed"
                    else None,
                }
            if isinstance(event, ErrorEvent):
                row["error_code"] = str(event.code or "unknown")
            yield event
        row["total_ms"] = round((time.monotonic() - started) * 1000, 2)
        if facts is not None:
            from opensquilla.provider.execution_identity import render_execution_identity

        row["facts_match_target"] = (
            len(fragments) == 1
            and facts is not None
            and facts.model == self.model
            and facts.provider == PROVIDER
            and fragments[0] == render_execution_identity(facts)
        )


async def run_case(*, model: str, question: str, placeholder: str, fallback: bool = False) -> dict:
    provider = CaptureProvider(
        _build_provider(
            ProviderConfig(
                provider=PROVIDER,
                model=model,
                api_key=placeholder,
                base_url=registry_endpoint(PROVIDER),
            )
        ),
        model,
    )
    selected_model = model
    actual_provider: Any = provider
    if fallback:
        selected_model = "synthetic-failing-deployment"

        class FailingProvider:
            provider_name = PROVIDER

            async def chat(self, messages, tools=None, config=None):
                yield ErrorEvent(message="synthetic injected unavailable", code="503")

        class Selector:
            current_config = SimpleNamespace(provider=PROVIDER, model=selected_model)

            def next_fallback_after_failure(self, exc):
                self.current_config = SimpleNamespace(provider=PROVIDER, model=model)
                return provider

        actual_provider = _SelectorFallbackProvider(FailingProvider(), Selector(), turn_metadata={})
    identity_fields: dict[str, Any] = {}
    if hasattr(provider_types, "ExecutionIdentity"):
        identity_fields["execution_identity"] = provider_types.ExecutionIdentity(
            provider=PROVIDER,
            model=selected_model,
        )
    elif "execution_identity_context" in AgentConfig.__dataclass_fields__:
        from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
            _selected_execution_identity_context,
        )

        identity_fields["execution_identity_context"] = _selected_execution_identity_context(
            model=selected_model,
            provider=PROVIDER,
            metadata={},
        )
    agent = Agent(
        provider=actual_provider,
        config=AgentConfig(
            **identity_fields,
            system_prompt=assemble_system_prompt(AgentProfile(agent_id="main"), tools=None),
            max_iterations=1,
            max_tokens=4096,
            context_window_tokens=128_000,
            request_timeout=120.0,
            flush_enabled=False,
        ),
    )
    events = [event async for event in agent.run_turn(QUESTIONS[question])]
    done = next((event for event in reversed(events) if event.kind == "done"), None)
    return {
        "model": model,
        "question": question,
        "answer": done.text if done else "",
        "completed": done is not None,
        "local_fault_injected": fallback,
        "physical_calls": provider.requests,
        "execution": agent._execution_status_snapshot()
        if hasattr(agent, "_execution_status_snapshot")
        else {},
    }


async def run(args) -> dict:
    if os.environ.get("TOKENRHYTHM_API_KEY"):
        raise ValueError("real_credential_must_stay_in_relay")
    ready_path = require_temporary_report_path(args.relay_ready)
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    if ready.get("mode") != "functional" or ready.get("enabled") is not True:
        raise ValueError("functional_relay_required")
    placeholder = str(ready["client_key"])
    report_path = require_temporary_report_path(args.report)
    log = FunctionalRequestLog(require_temporary_report_path(ready["request_log"]), enabled=True)
    os.environ.update(
        {
            "OPENSQUILLA_LIVE_TRANSPORT": "1",
            "OPENSQUILLA_LIVE_RELAY_URL": str(ready["base_url"]),
            "OPENSQUILLA_LIVE_RELAY_CLIENT_KEY": placeholder,
            "OPENSQUILLA_LIVE_DISABLE_DOTENV": "1",
            "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
            "OPENSQUILLA_TURN_CALL_LOG": "0",
        }
    )
    restore = install_from_env()
    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-identity-state-") as state:
            os.environ.update(
                {
                    "OPENSQUILLA_STATE_DIR": state + "/state",
                    "OPENSQUILLA_USER_STATE_DIR": state + "/user",
                    "OPENSQUILLA_LOG_DIR": state + "/logs",
                }
            )
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=30
            ) as client:
                response = await client.get(
                    registry_endpoint(PROVIDER) + "/models",
                    headers={
                        "Authorization": "Bearer " + placeholder,
                    },
                )
                if response.status_code != 200:
                    raise ValueError("model_catalog_http_" + str(response.status_code))
                rows = response.json().get("data", [])
            available = {
                str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id")
            }
            models = preset_models()
            if args.model and any(model not in models for model in args.model):
                raise ValueError("model_must_be_in_locked_preset")
            aliases = {model: [model] for model in models}
            report: dict[str, Any] = {
                "suite": args.suite,
                "source": args.source_sha,
                "scope": "agent_physical_request",
                "models": models,
                "cases": [],
                "status": "running",
                "human_review": "required",
                "variant": args.variant,
                "selected_models": args.model or models,
            }
            missing = [model for model in models if model not in available]
            if missing:
                report.update(status="blocked_model_unavailable", missing=missing)
                write_safe_report(report_path, report, (placeholder,))
                return report
            for model in models:
                name = available[model].get("name")
                if isinstance(name, str) and name.strip():
                    aliases[model].append(name)
            report["aliases"] = aliases
            completed_cases = set()
            if args.resume_from:
                previous = json.loads(require_temporary_report_path(args.resume_from).read_text())
                if any(
                    previous.get(key) != report[key]
                    for key in ("source", "suite", "models", "aliases")
                ):
                    raise ValueError("resume_source_or_catalog_mismatch")
                for row in previous["cases"]:
                    matched = answer_matches(row["answer"], row["model"], row["question"], aliases)
                    if (
                        not row["completed"]
                        or not matched
                        or not all(call.get("facts_match_target") for call in row["physical_calls"])
                    ):
                        raise ValueError("cannot_resume_failed_product_case")
                    row["previous_oracle_match"] = row["matched"]
                    row["matched"] = matched
                    report["cases"].append(row)
                    completed_cases.add(row["case_id"])
                report["resumed_cases"] = len(completed_cases)
            questions = (
                ["zh"]
                if args.suite == "smoke"
                else ["zh", "en", "provider", "product", "task", "json"]
            )
            if args.suite == "fallback":
                questions = ["zh", "en"]
            for model in models:
                if args.model and model not in args.model:
                    continue
                for question in questions:
                    count = 5 if args.suite == "direct" and question in {"zh", "en"} else 1
                    if args.suite == "fallback":
                        count = 3
                    for sample in range(count):
                        case_id = f"identity-{args.suite}-{models.index(model)}-{question}-{sample}"
                        if case_id in completed_cases:
                            continue
                        log.select_phase(variant=args.variant, case_id=case_id)
                        row = await run_case(
                            model=model,
                            question=question,
                            placeholder=placeholder,
                            fallback=args.suite == "fallback",
                        )
                        row["case_id"] = case_id
                        row["matched"] = answer_matches(row["answer"], model, question, aliases)
                        report["cases"].append(row)
                        report["physical_http_count"] = len(log.snapshot()["requests"])
                        write_safe_report(report_path, report, (placeholder,))
                        print(
                            json.dumps(
                                {
                                    "case": case_id,
                                    "completed": row["completed"],
                                    "matched": row["matched"],
                                }
                            ),
                            flush=True,
                        )
                        if args.variant == "new" and (
                            not row["completed"]
                            or not row["matched"]
                            or not all(
                                call.get("facts_match_target") for call in row["physical_calls"]
                            )
                        ):
                            report["status"] = "stopped_for_review"
                            write_safe_report(report_path, report, (placeholder,))
                            return report
            report["status"] = "sampled_pending_human_review"
            write_safe_report(report_path, report, (placeholder,))
            return report
    finally:
        restore()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--relay-ready", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--variant", choices=("new", "baseline"), default="new")
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--suite", choices=("smoke", "direct", "fallback"), default="smoke")
    args = parser.parse_args()
    if not args.enable_live:
        print('{"enabled":false}')
        return 0
    if not all((args.relay_ready, args.report, args.source_sha)):
        parser.error("relay-ready, report and exact source-sha are required")
    result = asyncio.run(run(args))
    return 0 if result["status"] == "sampled_pending_human_review" else 1


if __name__ == "__main__":
    # The output filter belongs only to this standalone probe process. Imported
    # callers (including offline tests) must retain their own logging policy.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    sys.exit(main())
