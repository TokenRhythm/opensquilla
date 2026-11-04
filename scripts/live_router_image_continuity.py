#!/usr/bin/env python3
"""Bounded real TokenRhythm acceptance for model-policy image continuity.

Only synthetic images/messages are used. Provider requests and answers stay in
memory or disposable private session storage; the report contains facts only.
Deterministic classifier cases exercise the real policy/runner/provider boundary;
the natural-history case also uses the bundled production classifier.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import copy
import hashlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.live_harness_security import (  # noqa: E402
    classify_failure,
    registry_endpoint,
    require_temporary_report_path,
    scan_and_remove_temporary_tree,
    write_safe_report,
)
from scripts.live_reasoning_replay_e2e import WireObserver, _config, _usage_report  # noqa: E402

CASES = (
    "text_control", "forward_image", "no_backward", "all_text_history",
    "natural_history", "tool_image",
)
MARKER_FRAGMENTS = ("图片未分析", "图片分析失败", "历史图片本回合未重新读取")


class AcceptanceError(RuntimeError):
    """A bounded diagnostic code safe for the public report."""


def require(value: object, code: str) -> None:
    if not value:
        raise AcceptanceError(code)


class FixedClassifier:
    """Control semantic decisions without replacing any provider response."""

    def __init__(self, tier: str):
        self.tier, self.intent = tier, "new_task"

    def predict(self, _value: Any) -> SimpleNamespace:
        return SimpleNamespace(
            intent_probabilities={
                name: float(name == self.intent) for name in ("new_task", "continue", "redo")
            },
            tier_probabilities={f"c{i}": float(self.tier == f"c{i}") for i in range(4)},
            intent_decision=self.intent, tier_decision=self.tier,
        )


def synthetic_image() -> tuple[bytes, str]:
    from PIL import Image, ImageDraw, ImageFont

    code = "TEST" + secrets.token_hex(3).upper()
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (35, 70), code, fill="black", font=ImageFont.load_default(size=72),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), code


def payload_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """Count physical input facts without serializing prompt/image contents."""
    images = markers = tools = 0

    def visit(value: Any) -> None:
        nonlocal images, markers, tools
        if isinstance(value, dict):
            if value.get("type") in {"image_url", "input_image", "image"}:
                images += 1
            if value.get("role") == "tool":
                tools += 1
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            markers += sum(value.count(fragment) for fragment in MARKER_FRAGMENTS)

    visit(payload.get("messages", []))
    return {"model": str(payload.get("model", "")), "native_images": images,
            "image_markers": markers, "tool_results": tools}


def admitted_semantic_facts(record: Any) -> dict[str, Any]:
    """Read persisted policy state, independently of physical execution metadata."""
    require(isinstance(record, dict), "durable_router_decision_missing")
    require(record.get("status") == "admitted" and record.get("state_committed") is True,
            "durable_router_decision_not_admitted")
    state = record.get("state") or {}
    tier = state.get("tier")
    require(tier in {"c0", "c1", "c2", "c3"}, "durable_semantic_tier_invalid")
    intent = (record.get("decision") or {}).get("intent")
    require(intent in {"new_task", "continue", "redo"}, "durable_semantic_intent_invalid")
    return {"semantic_tier": tier, "semantic_intent": intent, "state_admitted": True}


async def load_catalog(api_key: str, text_model: str, vision_model: str) -> dict[str, Any]:
    from opensquilla.provider.model_catalog import shared_catalog
    from opensquilla.provider.tokenrhythm_catalog import (
        TokenRhythmModelMetadata,
        fetch_tokenrhythm_declared,
        fetch_tokenrhythm_published,
        tokenrhythm_authority_identity,
        tokenrhythm_published_catalog_entries,
    )

    endpoint = registry_endpoint("tokenrhythm")
    published, declared = await asyncio.gather(
        fetch_tokenrhythm_published(timeout=30),
        fetch_tokenrhythm_declared(endpoint, api_key=api_key, timeout=30),
    )
    for model in (text_model, vision_model):
        require(model in published and model in declared, "selected_model_not_authorized")
    catalog = shared_catalog()
    catalog.set_live_provider_entries(
        "tokenrhythm", tokenrhythm_published_catalog_entries(published),
    )
    catalog.set_provider_model_metadata("tokenrhythm", {
        model: TokenRhythmModelMetadata(published=published.get(model), declared=row)
        for model, row in declared.items()
    })
    authority = tokenrhythm_authority_identity(
        provider="tokenrhythm", base_url=endpoint, api_key=api_key,
    )
    require(authority, "catalog_authority_missing")
    catalog.set_tokenrhythm_snapshot_sidecars(
        published=published, declared_by_authority={authority: declared},
    )
    facts = {}
    for model, expected in ((text_model, False), (vision_model, True)):
        support = catalog.resolve_deployment_vision_support(
            model, provider="tokenrhythm", base_url=endpoint, api_key=api_key,
        )
        require(support == ("supported" if expected else "unsupported"),
                "catalog_vision_fact_does_not_match_case")
        facts[model] = {"authorized": True, "vision_support": support}
    return facts


async def run_case(
    root: Path, case: str, *, api_key: str, text_model: str, vision_model: str,
    observer: WireObserver, max_output: int,
) -> dict[str, Any]:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.steps import squilla_router
    from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
    from opensquilla.provider.model_catalog import shared_catalog
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolContext, ToolSpec, current_tool_context

    require(case in CASES, "unknown_case")
    endpoint = registry_endpoint("tokenrhythm")
    config = _config(root, "tokenrhythm", text_model, endpoint, thinking="off")
    config.llm.api_key_env = "TOKENRHYTHM_API_KEY"
    config.llm.max_tokens = max_output
    config.llm.context_window_tokens = 0
    config.squilla_router.enabled = True
    config.squilla_router.strategy = "lightgbm_model"
    config.squilla_router.rollout_phase = "full"
    config.squilla_router.default_tier = "c2"
    config.squilla_router.tiers = {
        f"c{i}": {"provider": "tokenrhythm", "model": text_model} for i in range(4)
    }
    if case in {"forward_image", "natural_history", "tool_image"}:
        config.squilla_router.tiers["c3"]["model"] = vision_model
    if case in {"no_backward", "natural_history"}:
        config.squilla_router.tiers["c0"]["model"] = vision_model
    config.tools.allow = ["load_test_image"] if case == "tool_image" else []
    config.tools.profile = "minimal"
    config.agent_max_iterations = 3
    config.agent_max_provider_retries = 1
    config.log_file_enabled = False
    config.privacy.reliability_diagnostics_enabled = False
    config.privacy.product_analytics_enabled = False
    config.privacy.disable_network_observability = True
    original_tiers = copy.deepcopy(config.squilla_router.tiers)
    image_bytes, code = synthetic_image()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    registry, tool_calls = ToolRegistry(), []

    async def load_test_image(_tool_use_id: str = "") -> str:
        context = current_tool_context.get()
        require(context is not None and _tool_use_id, "tool_context_missing")
        tool_calls.append(_tool_use_id)
        context.tool_result_media[_tool_use_id] = [{"mime": "image/png", "data": encoded}]
        return json.dumps({"status": "loaded", "note": "Read the returned test image."})

    if case == "tool_image":
        registry.register(ToolSpec(
            name="load_test_image", description="Load the synthetic test image exactly once.",
            parameters={}, runtime_only_arguments=frozenset({"_tool_use_id"}),
        ), load_test_image)

    classifier = FixedClassifier("c3" if case == "no_backward" else "c2")
    natural = case == "natural_history"
    patch_strategy = contextlib.nullcontext() if natural else patch.object(
        squilla_router, "_get_strategy", lambda _: classifier,
    )
    image_prompt = (
        "Read the single uppercase code in the attached image. Reply only with that code."
    )
    text_prompt = "This is a synthetic check. Reply only TEXT_CONTINUITY_OK. Do not call tools."
    first = (
        "Call load_test_image exactly once, then read its single uppercase code and reply only "
        "with that code. Do not call the tool again."
        if case == "tool_image" else text_prompt if case == "text_control" else image_prompt
    )
    prompts = [first]
    if case in {"natural_history", "all_text_history"}:
        prompts += [
            "Continue the preceding image task. Reply only TEXT_CONTINUITY_OK.",
            "The previous answer was wrong. Redo that image task. Reply only TEXT_CONTINUITY_OK.",
        ]
    key = "agent:main:synthetic-router-images"
    rows: list[dict[str, Any]] = []
    async with SessionStorage(str(root / "sessions.sqlite")) as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(session_key=key, agent_id="main")
        with patch_strategy:
            for index, prompt in enumerate(prompts):
                classifier.intent = ("new_task", "continue", "redo")[min(index, 2)]
                attachments = ([{"type": "image/png", "name": "synthetic.png",
                                 "data": encoded, "_was_staged": True}]
                               if index == 0 and case not in {"text_control", "tool_image"}
                               else [])
                content = prompt
                if attachments:
                    content, _ = build_transcript_attachment_envelope(
                        text=prompt, attachments=attachments, session_id=session.session_id,
                        media_root=Path(config.attachments.media_root), persist_enabled=True,
                    )
                user = await manager.append_message(key, "user", content)
                runner = TurnRunner(
                    provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
                        provider="tokenrhythm", model=text_model, api_key=api_key,
                        base_url=endpoint,
                    ))),
                    tool_registry=registry, session_manager=manager, config=config,
                    model_catalog=shared_catalog(),
                )
                start = len(observer.calls)
                events = [event async for event in runner.run(
                    prompt, session_key=key, bound_user_message_id=user.message_id,
                    attachments=attachments,
                    tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
                )]
                calls = observer.calls[start:]
                errors = [event for event in events if event.kind == "error"]
                for event in errors:
                    value = str(getattr(event, "code", "unknown"))
                    observer.engine_error_codes.append(
                        value if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else "unknown"
                    )
                done = [event for event in events if event.kind == "done"]
                facts = [payload_facts(call.request) for call in calls]
                text = "".join(str(getattr(event, "text", ""))
                               for event in events if event.kind == "text_delta")
                # Some engine adapters publish only the completed answer body.
                if not text and done:
                    text = str(getattr(done[-1], "text", ""))
                require(not errors and done and calls, "turn_did_not_complete")
                require(all(call.completed and call.status_code == 200 for call in calls),
                        "provider_call_did_not_complete")
                require(config.squilla_router.tiers == original_tiers,
                        "panel_configuration_changed")
                require(text.strip(), "empty_answer")
                if case in {"forward_image", "tool_image"}:
                    require(code in text, "image_answer_not_verified")
                    require(facts[-1]["model"] == vision_model and facts[-1]["native_images"],
                            "forward_visual_execution_missing")
                if case in {"no_backward", "all_text_history"}:
                    require(all(fact["model"] == text_model for fact in facts),
                            "backward_model_jump")
                    require(not any(fact["native_images"] for fact in facts),
                            "image_sent_to_text_model")
                    require(any(fact["image_markers"] for fact in facts), "text_projection_missing")
                if case == "natural_history" and index == 0:
                    require(code in text, "natural_image_answer_not_verified")
                if case == "text_control" or index > 0:
                    require("TEXT_CONTINUITY_OK" in text, "text_answer_not_verified")
                routes = [{"tier": getattr(event, "tier", None),
                           "model": getattr(event, "model", None)}
                          for event in events if event.kind == "router_decision"]
                transcript = await manager.get_model_router_transcript(key)
                admitted = next((entry for entry in transcript
                                 if entry.message_id == user.message_id), None)
                policy = admitted_semantic_facts(
                    (admitted.turn_context or {}).get("router_model_decision")
                    if admitted is not None else None,
                )
                if not natural:
                    expected_tier = "c3" if case == "no_backward" or index == 2 else "c2"
                    require(policy["semantic_tier"] == expected_tier,
                            "physical_image_route_changed_semantic_tier")
                rows.append({"turn": index + 1, "passed": True, "routes": routes,
                             "calls": facts, "answer_verified": True, **policy})
    require(case != "tool_image" or len(tool_calls) == 1, "tool_execution_not_exactly_once")
    retained = [path for path in Path(config.attachments.media_root).rglob("*")
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == image_hash]
    require(case in {"text_control", "tool_image"} or retained, "original_image_not_retained")
    return {"case": case, "status": "passed", "classifier": "production" if natural else "fixed",
            "configuration_unchanged": True, "original_image_retained": bool(retained),
            "tool_calls": len(tool_calls), "turns": rows}


async def run_acceptance(args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    import structlog

    import opensquilla.engine.runtime

    require(Path(opensquilla.engine.runtime.__file__).resolve().is_relative_to(REPO_ROOT / "src"),
            "imported_source_root_mismatch")
    logging.disable(logging.CRITICAL)
    structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())
    root = Path(tempfile.mkdtemp(prefix="opensquilla-router-images-"))
    observer = WireObserver(registry_endpoint("tokenrhythm"), max_calls=args.max_calls,
                            endpoints={"tokenrhythm": registry_endpoint("tokenrhythm")})
    rows: list[dict[str, Any]] = []
    env = {"OPENSQUILLA_TURN_CALL_LOG": "0", "OPENSQUILLA_STATE_DIR": str(root / "state"),
           "OPENSQUILLA_LOG_DIR": str(root / "logs"),
           "OPENSQUILLA_USER_STATE_DIR": str(root / "profile"),
           "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1"}
    try:
        with patch.dict(os.environ, env), observer.observe():
            catalog = await load_catalog(api_key, args.text_model, args.vision_model)
            for case in args.case or CASES:
                first_call, first_error = len(observer.calls), len(observer.engine_error_codes)
                try:
                    row = await run_case(
                        root / case, case, api_key=api_key, text_model=args.text_model,
                        vision_model=args.vision_model, observer=observer,
                        max_output=args.max_output,
                    )
                except Exception as exc:  # One bounded diagnosis; never serialize external prose.
                    row = {"case": case, "status": "failed", "error_type": type(exc).__name__,
                           "failure": str(exc) if isinstance(exc, AcceptanceError)
                           else classify_failure(str(exc))}
                    row["calls"] = [payload_facts(call.request)
                                    for call in observer.calls[first_call:]]
                    row["engine_error_codes"] = observer.engine_error_codes[first_error:]
                rows.append(row)
                print(json.dumps({"case": case, "status": row["status"]}), flush=True)
        return {"ok": all(row["status"] == "passed" for row in rows), "cases": rows,
                "catalog": catalog, "synthetic_only": True, "frontend_modified": False,
                "physical_call_limit": args.max_calls, **_usage_report(observer.calls)}
    finally:
        scan_and_remove_temporary_tree(root, {"TOKENRHYTHM_API_KEY": api_key})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--text-model", required=True)
    parser.add_argument("--vision-model", required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-output", type=int, default=1024)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "status": "live_opt_in_required"}))
        return 2
    require_temporary_report_path(args.report)
    if not 1 <= args.max_calls <= 32 or not 256 <= args.max_output <= 2048:
        parser.error("max-calls must be 1..32 and max-output must be 256..2048")
    api_key = os.environ.get("TOKENRHYTHM_API_KEY", "").strip()
    if not api_key:
        print(json.dumps({"ok": False, "status": "missing_credential"}))
        return 2
    try:
        result = asyncio.run(run_acceptance(args, api_key))
    except Exception as exc:
        result = {"ok": False, "status": "harness_failed", "error_type": type(exc).__name__,
                  "failure": str(exc) if isinstance(exc, AcceptanceError)
                  else classify_failure(str(exc))}
    result["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
    ).strip()
    result["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["source_files_sha256"] = {
        relative: hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        for relative in (
            "src/opensquilla/engine/runtime.py",
            "src/opensquilla/engine/model_router_quality.py",
            "src/opensquilla/engine/model_policy_recovery.py",
            "src/opensquilla/engine/steps/model_router.py",
            "src/opensquilla/engine/steps/squilla_router.py",
            "src/opensquilla/provider/image_projection.py",
        )
    }
    write_safe_report(args.report, result, {"TOKENRHYTHM_API_KEY": api_key})
    print(json.dumps({"ok": result["ok"], "report": str(args.report)}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
