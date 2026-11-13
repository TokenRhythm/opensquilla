from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from opensquilla.jev_service import JevService, get_jev_service

SESSION = "agent:main:webchat:sample"
DEFAULTS = SimpleNamespace(
    enabled=False, mode="suggest", model="decision-small", provider="tokenrhythm",
    max_prompts_per_hour=3, cooldown_turns=3, confidence_threshold=0.65,
    timeout_seconds=5.0,
)


def decision() -> dict:
    return {
        "action": "switch", "current": {"provider": "tokenrhythm", "model": "large"},
        "target": {"provider": "tokenrhythm", "model": "small"},
        "reason": "A short classification task can use a smaller model.",
        "evidence": ["batch_task"], "risk": "low",
    }


def context_notice() -> dict:
    return {
        **decision(), "action": "new_session", "target": None,
        "handoff": {"sourceSessionKey": SESSION, "contextTokens": 40_000,
                    "contextWindowTokens": 50_000},
    }


@pytest.mark.parametrize("mode,expected", [("suggest", "dismissed"), ("trust", "accepted")])
async def test_ensemble_deadline_works_without_a_direct_target(mode, expected) -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True, "mode": mode}, DEFAULTS)
    try:
        record = await service.publish(SESSION, {
            **decision(), "action": "ensemble", "target": None,
            "ensemble": {"tier": "c4", "configurationFingerprint": "synthetic"},
        }, auto_switch_seconds=5)
        assert record["autoSwitchAt"] == 105_000
        now[0] = 105_001
        await service.settle_deadline(SESSION, record["id"])
        assert (await service.get_decision(SESSION, record["id"]))["status"] == expected
    finally:
        await service.close()


async def test_context_notice_never_auto_migrates_and_coexists_with_model_advice() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True, "mode": "trust"}, DEFAULTS)
    try:
        notice = await service.publish(SESSION, context_notice(), input_id="context")
        model = await service.publish(SESSION, decision(), input_id="model")
        assert "autoSwitchAt" not in notice
        now[0] += 60_000
        await service.settle_deadline(SESSION, notice["id"])
        assert (await service.get_decision(SESSION, notice["id"]))["status"] == "pending"
        snapshot = await service.snapshot(SESSION, DEFAULTS)
        assert snapshot["recommendation"]["id"] == model["id"]
        await service.cancel_checks(SESSION, preserve_context_notices=True)
        assert (await service.get_decision(SESSION, model["id"]))["status"] == "expired"
        assert (await service.snapshot(SESSION, DEFAULTS))["recommendation"]["id"] == notice["id"]
        await service.respond(SESSION, notice["id"], True)
        applied = await service.mark_handoff_applied(SESSION, notice["id"], "synthetic:new-session")
        assert applied["handoff"]["targetSessionKey"] == "synthetic:new-session"
        await service.reopen_handoff(SESSION, notice["id"])
        assert (await service.get_decision(SESSION, notice["id"]))["status"] == "applied"
    finally:
        await service.close()


async def test_draft_checks_replace_stale_work_and_reuse_matching_submission() -> None:
    service = JevService()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def old(generation):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    old_task = await service.start_check(SESSION, "draft-a", old)
    await entered.wait()

    async def new(generation):
        return await service.publish(
            SESSION, decision(), input_id="draft-b", check_generation=generation,
            auto_switch_seconds=0,
        )

    task = await service.start_check(SESSION, "draft-b", new)
    assert await service.start_check(SESSION, "draft-b", new) is task
    assert (await task)["target"]["model"] == "small"
    await cancelled.wait()
    assert old_task.cancelled()
    assert service.current_check(SESSION, "draft-a") is None
    assert service.current_check(SESSION, "draft-b") is task
    assert await service.publish(
        SESSION, decision(), input_id="late-a", check_generation=1,
    ) is None
    await service.close()


async def test_skipped_check_clears_checking_and_notifies_ready() -> None:
    service = JevService()
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    observed = []

    async def changed(key):
        observed.append((await service.snapshot(key, DEFAULTS))["status"])

    service.on_change = changed
    await service.set_status(SESSION, "checking")

    async def exhausted_budget(_generation):
        assert not await service.can_evaluate(SESSION, "draft", max_prompts_per_hour=0)
        return None

    task = await service.start_check(SESSION, "draft", exhausted_budget)
    await task
    assert (await service.snapshot(SESSION, DEFAULTS))["status"] == "ready"
    assert observed == ["ready"]
    await service.close()


async def test_cancelled_check_clears_checking_without_losing_unavailable_status() -> None:
    service = JevService()
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    started = asyncio.Event()

    async def pending(_generation):
        await service.set_status(SESSION, "checking")
        started.set()
        await asyncio.Event().wait()

    task = await service.start_check(SESSION, "pending", pending)
    await started.wait()
    await service.cancel_checks(SESSION)
    assert task.cancelled()
    assert (await service.snapshot(SESSION, DEFAULTS))["status"] == "ready"

    async def unavailable(_generation):
        await service.set_status(SESSION, "unavailable")

    await (await service.start_check(SESSION, "unavailable", unavailable))
    await service.cancel_checks(SESSION)
    assert (await service.snapshot(SESSION, DEFAULTS))["status"] == "unavailable"
    await service.close()


async def test_stale_check_cleanup_does_not_clear_newer_checking_status() -> None:
    service = JevService()
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    entered = asyncio.Event()
    cancellation_received = asyncio.Event()
    finish_old = asyncio.Event()
    new_started = asyncio.Event()
    finish_new = asyncio.Event()

    async def old(_generation):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await finish_old.wait()

    async def current(_generation):
        await service.set_status(SESSION, "checking")
        new_started.set()
        await finish_new.wait()

    old_task = await service.start_check(SESSION, "old", old)
    await entered.wait()
    new_task = await service.start_check(SESSION, "current", current)
    await cancellation_received.wait()
    await new_started.wait()
    finish_old.set()
    await old_task
    assert (await service.snapshot(SESSION, DEFAULTS))["status"] == "checking"
    finish_new.set()
    await new_task
    assert (await service.snapshot(SESSION, DEFAULTS))["status"] == "ready"
    await service.close()


async def test_auto_deadline_accepts_once_and_decline_cancels_the_choice() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True, "mode": "trust"}, DEFAULTS)
    called = asyncio.Event()
    calls = []

    async def accepted(key, identifier):
        calls.append((key, identifier))
        await service.respond(key, identifier, True)
        called.set()

    service.on_auto_accept = accepted
    record = await service.publish(
        SESSION, decision(), input_id="deadline", auto_switch_seconds=5,
    )
    assert record["autoSwitchAt"] == 105_000
    now[0] = 105_001
    await asyncio.wait_for(called.wait(), 1)
    assert calls == [(SESSION, record["id"])]
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "accepted"
    second = await service.publish(
        SESSION, decision(), input_id="declined", auto_switch_seconds=5,
    )
    await service.respond(SESSION, second["id"], False)
    now[0] += 5001
    await asyncio.sleep(0)
    assert (await service.get_decision(SESSION, second["id"]))["status"] == "dismissed"
    assert len(calls) == 1
    await service.close()


async def test_suggest_deadline_dismisses_without_calling_auto_accept() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True, "mode": "suggest"}, DEFAULTS)
    called = []

    async def accept(key, identifier):
        called.append(identifier)

    service.on_auto_accept = accept
    record = await service.publish(SESSION, decision(), auto_switch_seconds=5)
    now[0] += 5_001
    await service.settle_deadline(SESSION, record["id"])
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "dismissed"
    assert called == []
    await service.close()


async def test_explicit_accept_before_suggest_deadline_is_preserved() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    record = await service.publish(SESSION, decision(), auto_switch_seconds=5)
    await service.respond(SESSION, record["id"], True)
    now[0] += 5_001
    await service.settle_deadline(SESSION, record["id"])
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "accepted"
    await service.close()


async def test_global_settings_rebase_legacy_overrides_and_cancel_old_advice(tmp_path) -> None:
    service = JevService(tmp_path / "jev.db")
    await service.configure(SESSION, {
        "enabled": True, "mode": "trust", "budgetPerHour": 7, "loopInterval": 5,
    }, DEFAULTS)
    record = await service.publish(SESSION, decision())
    await service.set_blocked(SESSION, True)
    changed = []

    async def observe(key):
        changed.append(key)

    service.on_change = observe
    global_settings = {**vars(DEFAULTS), "enabled": True, "mode": "suggest"}
    await service.apply_global_settings(global_settings, {"mode", "max_prompts_per_hour"})
    settings = await service.settings(SESSION, DEFAULTS)
    assert settings["mode"] == "suggest"
    assert settings["max_prompts_per_hour"] == 3
    assert settings["loop_interval"] == 5
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "expired"
    assert not (await service.snapshot(SESSION, DEFAULTS))["blocked"]
    assert SESSION in changed
    await service.close()
    reopened = JevService(tmp_path / "jev.db")
    settings = await reopened.settings(SESSION, global_settings)
    assert settings["mode"] == "suggest" and settings["loop_interval"] == 5
    await reopened.close()


async def test_global_disable_expires_accepted_advice_and_blocks_auto_switch() -> None:
    service = JevService()
    await service.configure(SESSION, {"enabled": True, "mode": "trust"}, DEFAULTS)
    record = await service.publish(SESSION, decision())
    await service.respond(SESSION, record["id"], True)
    await service.apply_global_settings(DEFAULTS, {"enabled"})
    assert not service.is_enabled(SESSION, DEFAULTS)
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "expired"
    await service.close()


async def test_pending_trust_advice_cannot_survive_authorization_downgrade() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    await service.configure(SESSION, {"enabled": True, "mode": "trust"}, DEFAULTS)
    record = await service.publish(SESSION, decision(), auto_switch_seconds=5)
    await service.configure(SESSION, {"mode": "suggest"}, DEFAULTS)
    now[0] += 5_001
    await service.settle_deadline(SESSION, record["id"])
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "expired"
    await service.close()


async def test_reset_cancels_background_check_and_clears_gate() -> None:
    service = JevService()
    await service.bind_session(SESSION, "synthetic-owner", 0)
    started = asyncio.Event()

    async def check(generation):
        started.set()
        await asyncio.Event().wait()

    task = await service.start_check(SESSION, "draft", check)
    await started.wait()
    await service.set_blocked(SESSION, True)
    await service.bind_session(SESSION, "synthetic-owner", 1)
    assert task.cancelled()
    assert service.current_check(SESSION, "draft") is None
    assert not (await service.snapshot(SESSION, DEFAULTS))["blocked"]
    await service.close()


async def test_new_settings_roundtrip_and_legacy_chat_model_uses_native_jev(tmp_path) -> None:
    service = JevService(tmp_path / "jev.db")
    await service.configure(SESSION, {
        "loopInterval": 5, "autoSwitchSeconds": 0, "model": "legacy-chat-model",
    }, DEFAULTS)
    await service.close()
    service = JevService(tmp_path / "jev.db")
    snapshot = await service.snapshot(SESSION, DEFAULTS)
    assert snapshot["loopInterval"] == 5
    assert snapshot["autoSwitchSeconds"] == 10
    assert snapshot["model"] == "NeoHorse-Jev-4B"
    with pytest.raises(ValueError):
        await service.configure(SESSION, {"loopInterval": 4}, DEFAULTS)
    with pytest.raises(ValueError):
        await service.configure(SESSION, {"autoSwitchSeconds": -1}, DEFAULTS)
    await service.close()


async def test_legacy_zero_wait_always_produces_a_finite_switch_deadline() -> None:
    from opensquilla.gateway.config import JevRouterConfig

    assert JevRouterConfig(auto_switch_seconds=0).auto_switch_seconds == 10
    assert JevRouterConfig(auto_switch_seconds="0").auto_switch_seconds == 10
    now = 200_000
    service = JevService(clock=lambda: now)
    await service.configure(SESSION, {"autoSwitchSeconds": 0}, DEFAULTS)
    settings = await service.settings(SESSION, DEFAULTS)
    assert settings["auto_switch_seconds"] == 10
    record = await service.publish(
        SESSION, decision(), input_id="bounded-legacy", auto_switch_seconds=0,
    )
    assert record["autoSwitchAt"] == now + 10_000
    await service.close()


async def test_restart_expires_orphan_draft_deadlines_and_preserves_preferences(tmp_path) -> None:
    path = tmp_path / "jev.db"
    service = JevService(path)
    await service.configure(SESSION, {"enabled": True, "loopInterval": 5}, DEFAULTS)
    record = await service.publish(SESSION, decision(), input_id="orphan")
    await service.set_status(SESSION, "checking")
    await service.close()
    service = JevService(path)
    snapshot = await service.snapshot(SESSION, DEFAULTS)
    assert snapshot["enabled"]
    assert snapshot["loopInterval"] == 5
    assert snapshot["status"] == "ready"
    assert snapshot["recommendation"] is None
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "expired"
    await service.close()


async def test_preferences_and_feedback_survive_restart(tmp_path) -> None:
    path = tmp_path / "jev.db"
    service = JevService(path)
    assert service.is_enabled(SESSION, DEFAULTS) is False
    await service.configure(SESSION, {"enabled": True, "budgetPerHour": 2}, DEFAULTS)
    assert service.is_enabled(SESSION, DEFAULTS) is True
    result = await service.publish(SESSION, decision(), input_id="input-1", routing_revision=4)
    assert result is not None
    await service.respond(SESSION, result["id"], False)
    await service.close()

    reopened = JevService(path)
    snapshot = await reopened.snapshot(SESSION, DEFAULTS)
    assert snapshot["enabled"] is True
    assert snapshot["budgetPerHour"] == 2
    assert snapshot["recommendation"] is None
    assert snapshot["decisions"][0]["status"] == "dismissed"
    await reopened.close()


async def test_prompt_budget_is_global_and_publish_is_idempotent() -> None:
    service = JevService(clock=lambda: 100_000)
    first = await service.publish(SESSION, decision(), input_id="first", max_prompts_per_hour=1)
    assert first is not None
    retry = await service.publish(SESSION, decision(), input_id="first", max_prompts_per_hour=1)
    assert retry == first
    assert await service.publish(
        "agent:main:webchat:other", decision(), input_id="other", max_prompts_per_hour=1,
    ) is None
    await service.close()


async def test_cooldown_counts_unique_inputs_and_resets_after_budget_window() -> None:
    now = [100_000]
    service = JevService(clock=lambda: now[0])
    assert await service.can_evaluate(SESSION, "input-1", max_prompts_per_hour=1)
    await service.publish(SESSION, decision(), input_id="input-1", max_prompts_per_hour=1)
    assert not await service.can_evaluate(SESSION, "input-1", max_prompts_per_hour=1)
    assert not await service.can_evaluate(SESSION, "input-2", max_prompts_per_hour=1)
    assert not await service.can_evaluate(SESSION, "input-3", max_prompts_per_hour=1)
    assert not await service.can_evaluate(SESSION, "input-4", max_prompts_per_hour=1)
    assert not await service.can_evaluate(SESSION, "input-5", max_prompts_per_hour=1)
    now[0] += 3_600_001
    assert await service.can_evaluate(SESSION, "input-6", max_prompts_per_hour=1)
    await service.close()


async def test_auto_choice_requires_exact_session_routing_revision() -> None:
    service = JevService()
    published = await service.publish(SESSION, decision(), input_id="input", routing_revision=2)
    assert published is not None
    assert await service.applied_target(SESSION, 2) is None
    await service.mark_applied(SESSION, published["id"])
    assert await service.applied_target(SESSION, 2) == decision()["target"]
    assert await service.applied_target(SESSION, 3) is None
    assert await service.applied_target(SESSION, None) is None
    await service.close()


async def test_session_reset_discards_choices_even_when_routing_revision_repeats() -> None:
    service = JevService()
    await service.bind_session(SESSION, "synthetic-session", 0)
    published = await service.publish(SESSION, decision(), input_id="input", routing_revision=0)
    assert published is not None
    await service.mark_applied(SESSION, published["id"])
    await service.bind_session(SESSION, "synthetic-session", 1)
    assert await service.applied_target(SESSION, 0) is None
    assert (await service.snapshot(SESSION, DEFAULTS))["decisions"] == []
    await service.close()


async def test_new_suggestion_expires_old_and_disabling_expires_pending() -> None:
    service = JevService()
    old = await service.publish(SESSION, decision(), input_id="old")
    new = await service.publish(SESSION, decision(), input_id="new")
    assert old is not None and new is not None
    assert (await service.get_decision(SESSION, old["id"]))["status"] == "expired"
    await service.configure(SESSION, {"enabled": False}, DEFAULTS)
    assert (await service.get_decision(SESSION, new["id"]))["status"] == "expired"
    with pytest.raises(ValueError, match="no longer pending"):
        await service.respond(SESSION, old["id"], True)
    assert await service.get_decision("agent:main:webchat:other", new["id"]) is None
    await service.close()


@pytest.mark.parametrize("patch", [
    {"enabled": "yes"}, {"budgetPerHour": True}, {"budgetPerHour": -1},
    {"mode": "automatic"}, {"provider": "other"}, {"model": " "},
    {"confidence_threshold": float("nan")}, {"timeout_seconds": float("inf")},
])
async def test_invalid_preferences_are_rejected(patch) -> None:
    service = JevService()
    with pytest.raises(ValueError):
        await service.configure(SESSION, patch, DEFAULTS)
    await service.close()


async def test_notifications_are_optional_and_transport_failure_is_nonfatal() -> None:
    service = JevService()
    seen = []

    async def callback(key):
        seen.append(key)
        raise RuntimeError("Disconnected")

    service.on_change = callback
    await service.notify(SESSION)
    assert seen == [SESSION]
    await service.close()


async def test_memory_session_manager_does_not_create_disk_state(tmp_path) -> None:
    manager = SimpleNamespace(storage=SimpleNamespace(_db_path=":memory:"))
    runner = SimpleNamespace()
    service = get_jev_service(SimpleNamespace(state_dir=str(tmp_path)), manager, runner)
    assert get_jev_service(None, manager, runner) is service
    assert list(tmp_path.iterdir()) == []
    await service.close()


async def test_zero_budget_and_session_deletion() -> None:
    service = JevService()
    await service.configure(SESSION, {"enabled": True, "budgetPerHour": 0}, DEFAULTS)
    assert not await service.can_evaluate(SESSION, "input", max_prompts_per_hour=0)
    await service.forget_session(SESSION)
    assert service.is_enabled(SESSION, DEFAULTS) is False
    assert (await service.snapshot(SESSION, DEFAULTS))["decisions"] == []
    await service.close()


async def test_ledger_close_is_idempotent() -> None:
    service = JevService()
    await service.close()
    await service.close()


async def test_corrupt_optional_ledger_preserves_file_and_uses_temporary_state(tmp_path):
    ledger = tmp_path / "jev.db"
    damaged = b"synthetic-secret-that-must-not-enter-diagnostics"
    ledger.write_bytes(damaged)
    manager = SimpleNamespace(storage=SimpleNamespace(_db_path=tmp_path / "sessions.db"))
    runner = SimpleNamespace()
    with capture_logs() as events:
        service = get_jev_service(None, manager, runner)
    assert service.persistence_degraded
    assert ledger.read_bytes() == damaged
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    assert service.is_enabled(SESSION, DEFAULTS)
    assert events[0]["event"] == "jev.persistence_unavailable"
    assert "lost on restart" in events[0]["notice"]
    assert damaged.decode() not in str(events)
    assert str(ledger) not in str(events)
    await service.close()


async def test_readonly_optional_ledger_falls_back_before_first_turn(tmp_path, monkeypatch):
    ledger = tmp_path / "jev.db"
    original_service = JevService(ledger)
    await original_service.close()
    original_connect = sqlite3.connect

    def readonly_connect(path, **kwargs):
        if str(path) == str(ledger):
            return original_connect(f"file:{ledger}?mode=ro", uri=True, **kwargs)
        return original_connect(path, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    manager = SimpleNamespace(storage=SimpleNamespace(_db_path=tmp_path / "sessions.db"))
    service = get_jev_service(None, manager, SimpleNamespace())
    assert service.persistence_degraded
    assert not service.is_enabled(SESSION, DEFAULTS)
    await service.configure(SESSION, {"enabled": True}, DEFAULTS)
    assert service.is_enabled(SESSION, DEFAULTS)
    await service.close()
