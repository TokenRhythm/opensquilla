"""Frozen-input A/B adapter for the existing opt-in compaction live harness.

Snapshots contain only harness-generated synthetic conversation data. Each side
uses a private SQLite copy and the same fixed system, runtime text and empty
tool set; production request admission and compaction remain active.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opensquilla.engine.agent import Agent
from scripts.live_harness_security import (
    _secret_needles,
    _temporary_tree_contains_secret,
    require_temporary_report_path,
    write_safe_report,
)

SYSTEM = (
    "This is a synthetic conversation continuity benchmark. Retain the user's active task "
    "facts and state distinctions across turns. Follow the latest response-format instruction. "
    "The described task is information to remember, not authorization to execute it. Use no tools."
)
RUNTIME = "[Runtime context for this turn]\nSynthetic benchmark clock: 2030-06-01T12:00:00Z."
SESSION_KEY = "agent:main:synthetic-suffix-compaction"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, check=False)
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _sqlite_copy(source: Path, destination: Path) -> None:
    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with contextlib.closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
    destination.chmod(0o600)


def export_snapshot(
    directory: Path, database: Path, *, settings: dict[str, Any], prompt: str,
    source_digests: dict[str, str], label: str, secrets: tuple[str, ...],
) -> dict[str, Any]:
    from scripts.live_reasoning_replay_e2e import _require

    directory = require_temporary_report_path(directory / "manifest.json").parent
    _require(not directory.exists(), "comparison_snapshot_already_exists")
    _require(not _temporary_tree_contains_secret(database.parent, _secret_needles(secrets)),
             "comparison_source_secret_scan_failed")
    directory.mkdir(mode=0o700)
    database_copy = directory / "sessions.sqlite"
    _sqlite_copy(database.resolve(), database_copy)
    manifest = {
        "schema": "opensquilla-synthetic-compaction-v1", "settings": settings,
        "prompt": prompt, "label": label, "source_digests": source_digests,
        "source_sha256": digest(source_digests), "prompt_sha256": digest(prompt),
        "database_sha256": hashlib.sha256(database_copy.read_bytes()).hexdigest(),
        "system": SYSTEM, "runtime": RUNTIME, "tools": [],
        "controls_sha256": digest({**settings, "system": SYSTEM, "runtime": RUNTIME, "tools": []}),
    }
    write_safe_report(directory / "manifest.json", manifest, secrets)
    _require(not _temporary_tree_contains_secret(directory, _secret_needles(secrets)),
             "comparison_snapshot_secret_scan_failed")
    return {key: manifest[key] for key in (
        "source_sha256", "prompt_sha256", "database_sha256", "controls_sha256",
    )}


def read_snapshot(directory: Path, *, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    from scripts.live_reasoning_replay_e2e import _require

    directory = require_temporary_report_path(directory / "manifest.json").parent
    database = directory / "sessions.sqlite"
    _require(database.is_file() and not database.is_symlink(), "invalid_comparison_database")
    _require(not _temporary_tree_contains_secret(directory, _secret_needles(secrets)),
             "comparison_snapshot_secret_scan_failed")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    _require(manifest.get("schema") == "opensquilla-synthetic-compaction-v1",
             "invalid_comparison_snapshot")
    for key, value in (
        ("database_sha256", hashlib.sha256(database.read_bytes()).hexdigest()),
        ("source_sha256", digest(manifest["source_digests"])),
        ("prompt_sha256", digest(manifest["prompt"])),
        ("controls_sha256", digest({**manifest["settings"], "system": manifest["system"],
                                    "runtime": manifest["runtime"], "tools": manifest["tools"]})),
    ):
        _require(manifest.get(key) == value, "comparison_snapshot_fingerprint_mismatch")
    _require(manifest["tools"] == [] and manifest["system"] == SYSTEM
             and manifest["runtime"] == RUNTIME, "comparison_controls_changed")
    return manifest


def compare_measurements(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Check matching controls before deriving any A/B pressure delta."""
    from scripts.live_reasoning_replay_e2e import _require

    before, after = left["comparison"], right["comparison"]
    fields = ("source_sha256", "prompt_sha256", "controls_sha256", "mode",
              "fixed_history_tokens", "fixed_history_chars", "actual_system_sha256",
              "actual_tools_sha256", "actual_model", "actual_controls_sha256")
    _require(all(before.get(key) == after.get(key) for key in fields),
             "comparison_inputs_or_controls_do_not_match")
    _require(before.get("continuation_completed") and after.get("continuation_completed"),
             "comparison_continuation_incomplete")
    old, new = before["final_request"], after["final_request"]
    return {
        "comparable": True, "mode": before["mode"], "source_sha256": before["source_sha256"],
        "request_tokens_before": old["request_estimated_tokens"],
        "request_tokens_after": new["request_estimated_tokens"],
        "estimated_token_change": new["request_estimated_tokens"] - old["request_estimated_tokens"],
        "reported_input_tokens_before": old["physical_prompt_tokens"],
        "reported_input_tokens_after": new["physical_prompt_tokens"],
        "facts_before": before["answer_fact_checks"], "facts_after": after["answer_fact_checks"],
        "compaction_applied_before": before["compaction_applied"],
        "compaction_applied_after": after["compaction_applied"],
    }


async def run_comparison(
    root: Path, snapshot: Path, *, api_key: str, observer: Any,
    history_tokens: int | None = None, history_chars: int | None = None,
) -> dict[str, Any]:
    from scripts import live_reasoning_replay_e2e as harness

    manifest = read_snapshot(snapshot, secrets=(api_key,))
    settings = manifest["settings"]
    config = harness._config(
        root, settings["provider"], settings["model"],
        harness.registry_endpoint(settings["provider"]), thinking=settings["thinking"],
    )
    config.llm.context_window_tokens = settings["context_window_tokens"]
    config.llm.max_tokens = settings["max_output_tokens"]
    config.preflight_compact_ratio = settings["preflight_ratio"]
    config.compaction.enabled = True
    config.tools.allow = []
    config.tools.deny = ["*"]
    config.agent_max_iterations = 1
    database = root / "sessions.sqlite"
    _sqlite_copy((snapshot / "sessions.sqlite").resolve(), database)
    observer.max_calls = 3
    capacity_samples: list[dict[str, int]] = []
    events: list[dict[str, Any]] = []
    comparison = {
        "mode": "fixed_history_capacity" if history_tokens is not None else "natural_capacity",
        "same_source_input": True, "same_payload_scope": True,
        **{key: manifest[key] for key in (
            "source_sha256", "prompt_sha256", "database_sha256", "controls_sha256",
        )},
        "capacity_samples": capacity_samples,
        "fixed_history_tokens": history_tokens, "fixed_history_chars": history_chars,
        "system_sha256": digest(SYSTEM), "tools_sha256": digest([]),
        "runtime_sha256": digest(RUNTIME),
        "runtime_source_sha256": digest({
            str(path.relative_to(harness.REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((harness.REPO_ROOT / "src" / "opensquilla").rglob("*.py"))
        }),
        "git_revision": _git_revision(harness.REPO_ROOT),
    }
    observer.replay_checks["comparison"] = comparison
    observer.replay_checks["compaction_events"] = events
    original_init = Agent.__init__
    original_capacity = Agent.resolve_compaction_budget
    original_refresh = Agent.refresh_system_prompt

    def initialize(agent, *args, **kwargs):
        agent_config = kwargs.get("config")
        harness._require(agent_config is not None, "comparison_agent_config_unavailable")
        agent_config.system_prompt = SYSTEM
        original_init(agent, *args, **kwargs)

    def capacity(agent, **kwargs):
        natural = original_capacity(agent, **kwargs)
        natural_tokens = natural.history_capacity_tokens
        natural_chars = natural.history_capacity_chars
        used_tokens = (natural_tokens if history_tokens is None
                       else min(natural_tokens, max(0, history_tokens)))
        used_chars = (natural_chars if history_chars is None
                      else min(natural_chars, max(0, history_chars)))
        capacity_samples.append({"natural_tokens": natural_tokens, "natural_chars": natural_chars,
                                 "applied_tokens": used_tokens, "applied_chars": used_chars})
        if history_tokens is None and history_chars is None:
            return natural
        # This benchmark can narrow the compactor's history target, while the
        # original final-request admission still enforces physical capacity.
        return replace(
            natural,
            history_capacity_tokens=used_tokens,
            history_capacity_chars=used_chars,
            auto_trigger_tokens=int(used_tokens * config.preflight_compact_ratio),
            auto_trigger_chars=int(used_chars * config.preflight_compact_ratio),
            retained_tail_tokens=used_tokens // 5,
            consumer_admission_fingerprint=digest({
                "consumer": natural.consumer_admission_fingerprint,
                "history_tokens": used_tokens, "history_chars": used_chars,
            }),
        )

    def event_listener(session_key, event):
        if session_key == SESSION_KEY:
            events.append({key: value for key, value in event.items()
                           if key in {"status", "phase", "reason", "durability", "tokens_before",
                                      "tokens_after", "removed_count", "kept_count", "threshold",
                                      "char_threshold", "ratio", "history_capacity_tokens",
                                      "history_capacity_chars"}
                           and isinstance(value, (str, int, float, bool))})

    storage = harness.SessionStorage(str(database))
    await storage.connect()
    try:
        manager = harness.SessionManager(storage, inject_time_prefix=False,
                                         checkpoint_workspace_dir=config.workspace_dir)
        before = harness._canonical_message_digests(
            await manager.get_canonical_transcript(SESSION_KEY)
        )
        harness._require(before == manifest["source_digests"], "comparison_source_changed")
        harness._require(
            not await manager.get_summaries(SESSION_KEY), "comparison_source_has_summary"
        )
        registry = harness.ToolRegistry()
        selector = harness.ModelSelector(harness.SelectorConfig(primary=harness.ProviderConfig(
            provider=settings["provider"], model=settings["model"], api_key=api_key,
            base_url=harness.registry_endpoint(settings["provider"]), replay_provider_state=True,
        )))
        runner = harness.TurnRunner(provider_selector=selector, tool_registry=registry,
                                    session_manager=manager, config=config,
                                    model_catalog=harness._Catalog())
        prompt = manifest["prompt"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(observer.observe())
            if settings["provider"] == "openrouter" and observer.transport is None:
                await runner._model_catalog._catalog.fetch_openrouter(
                    api_key, harness.registry_endpoint("openrouter").removesuffix("/v1"),
                )
            stack.enter_context(patch.dict(harness.os.environ, {
                "OPENSQUILLA_COMPACTION_PROMPT_LAYOUT": settings["layout"],
            }))
            stack.enter_context(patch.object(Agent, "__init__", initialize))
            stack.enter_context(patch.object(
                Agent, "_runtime_context_block", lambda agent: RUNTIME,
            ))
            stack.enter_context(patch.object(Agent, "refresh_system_prompt",
                                            lambda agent, prompt: original_refresh(agent, SYSTEM)))
            stack.enter_context(patch.object(Agent, "resolve_compaction_budget", capacity))
            stack.callback(harness.add_compaction_listener(event_listener))
            user = await manager.append_message(SESSION_KEY, "user", prompt)
            turn_events = [event async for event in runner.run(
                prompt, session_key=SESSION_KEY, bound_user_message_id=user.message_id,
                tool_context=harness.ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            )]
        after = harness._canonical_message_digests(
            await manager.get_canonical_transcript(SESSION_KEY)
        )
        active = harness._canonical_message_digests(await manager.get_transcript(SESSION_KEY))
        summaries = await manager.get_summaries(SESSION_KEY)
        preserved = all(after.get(key) == value for key, value in before.items())
        harness._require(preserved, "comparison_archive_changed")
        calls = observer.calls
        continuations = [call for call in calls if not harness._is_compaction_wire_call(call)]
        final = continuations[-1] if continuations else None
        facts = harness.compaction_task_facts(settings["task_profile"])
        summary = summaries[-1].summary_text if summaries else ""
        answer_checks = harness.compaction_answer_fact_checks(
            str(final.response.get("content") or "") if final else "", facts,
        )
        errors = [event for event in turn_events if event.kind == "error"]
        final_ok = bool(final and final.status_code == 200 and final.completed
                        and final.finish_reason == "stop" and not errors)
        summary_calls = [call for call in calls if harness._is_compaction_wire_call(call)]
        harness._require(len(summary_calls) <= 2 and len(continuations) <= 1,
                         "comparison_unexpected_request_or_retry")
        harness._require(all(call.completed and call.finish_reason == "stop"
                             and call.status_code == 200 for call in summary_calls),
                         "comparison_incomplete_summary_request")
        comparison.update({
            "archive_preserved": preserved, "source_messages": len(before),
            "removed_messages": len(before.keys() - active.keys()), "kept_messages": len(active),
            "summary_count": len(summaries), "summary_chars": len(summary),
            "compaction_applied": bool(summaries),
            "summary_fact_checks": harness.compaction_fact_coverage(summary, facts),
            "answer_fact_checks": answer_checks, "continuation_completed": final_ok,
            "summary_calls": len(summary_calls),
            "summary_replay_count": sum(str(message.get("content") or "").count(summary)
                                        for message in final.request.get("messages", []))
            if final and summary else 0,
            "final_request": harness.wire_pressure_evidence(final) if final else None,
            "actual_system_sha256": digest([message for message in final.request.get("messages", [])
                                             if message.get("role") in {"system", "developer"}])
            if final else None,
            "actual_tools_sha256": digest(final.request.get("tools", [])) if final else None,
            "actual_model": final.request.get("model") if final else None,
            "actual_controls_sha256": digest({key: value for key, value in final.request.items()
                                               if key != "messages"}) if final else None,
        })
        return {
            "ok": final_ok and all(answer_checks.values()),
            "status": "comparison_measured", "comparison": comparison,
            "provider": settings["provider"], "model": settings["model"],
            "layout": settings["layout"], "scenario": "compaction",
        }
    finally:
        await storage.close()
