from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opensquilla.observability import install_telemetry, network_policy, usage_telemetry
from opensquilla.observability.daily_usage_store import DailyUsageStore
from opensquilla.session.storage import SessionStorage

_IDENTITY_KEY = "telemetry.daily_usage_store_id"
_TODAY = date(2026, 7, 21)


def _enable_telemetry_for_test(monkeypatch) -> None:
    for name in (
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        network_policy.LEGACY_TELEMETRY_DISABLED_ENV,
        network_policy.LEGACY_UPDATE_CHECK_DISABLED_ENV,
        network_policy.DO_NOT_TRACK_ENV,
        network_policy.PRODUCT_ANALYTICS_DISABLED_ENV,
        install_telemetry.TELEMETRY_TESTING_ENV,
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        usage_telemetry.USAGE_TELEMETRY_ENDPOINT_ENV, "https://example.test/v1/usage"
    )
    monkeypatch.setattr(
        install_telemetry, "_collect_mac_address_candidates", lambda: ["02:00:00:00:00:21"]
    )
    monkeypatch.setattr(install_telemetry, "_collect_ip_address_candidates", lambda: [])


def _config(profile: Path, *, disabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        state_dir=str(profile / "state"),
        privacy=SimpleNamespace(disable_network_observability=disabled),
    )


async def _record_turns(storage: usage_telemetry.DailyUsageStorage, count: int = 1) -> None:
    for index in range(count):
        await storage.record_daily_usage(
            day="2026-07-20",
            input_tokens=10,
            output_tokens=2,
            cached_tokens=3,
            cache_write_tokens=1,
            updated_at=index + 1,
        )


def _write_legacy_daily_rows(path: Path, rows: list[tuple[Any, ...]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE telemetry_daily_usage (
                day TEXT PRIMARY KEY,
                conversation_turns INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                uploaded_at INTEGER
            )
            """
        )
        conn.executemany("INSERT INTO telemetry_daily_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


class _IdempotentCollector:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.lose_next_ack = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/v1/usage"
        payload = json.loads(request.content)
        event_id = payload["event_id"]
        assert request.headers["Idempotency-Key"] == event_id
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", event_id)
        self.attempts.append(payload)
        self.events.setdefault(event_id, payload)
        if self.lose_next_ack:
            self.lose_next_ack = False
            raise httpx.ReadTimeout("synthetic lost acknowledgement", request=request)
        return httpx.Response(200)


@pytest.fixture
def collector(monkeypatch) -> _IdempotentCollector:
    collector = _IdempotentCollector()
    original_client = httpx.AsyncClient

    def client(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(collector.handle), timeout=timeout)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return collector


@pytest.mark.parametrize("existing_daily_schema", [False, True])
async def test_independent_profiles_preserve_all_daily_totals(
    tmp_path, monkeypatch, collector, existing_daily_schema
):
    _enable_telemetry_for_test(monkeypatch)
    profiles = [tmp_path / "profile-a", tmp_path / "profile-b"]
    stores = []
    try:
        for turns, profile in enumerate(profiles, start=1):
            profile.mkdir()
            path = profile / "sessions.db"
            if existing_daily_schema:
                _write_legacy_daily_rows(
                    path,
                    [("2026-07-20", turns, 10 * turns, 2 * turns, 3 * turns, turns, 1, None)],
                )
            storage = await SessionStorage.open(str(path))
            stores.append(storage)
            if not existing_daily_schema:
                await _record_turns(storage, turns)
            assert (
                await usage_telemetry.upload_pending_daily_usage(
                    storage, config=_config(profile), today=_TODAY
                )
                == 1
            )
            assert await storage.list_pending_daily_usage(before_day=_TODAY.isoformat()) == []

        assert len(collector.events) == 2
        payloads = list(collector.events.values())
        assert payloads[0]["install_id"] == payloads[1]["install_id"]
        assert payloads[0]["event_id"] != payloads[1]["event_id"]
        assert sum(payload["conversation_turns"] for payload in payloads) == 3
        assert sum(payload["input_tokens"] for payload in payloads) == 30
        assert sum(payload["output_tokens"] for payload in payloads) == 6
        assert sum(payload["cached_tokens"] for payload in payloads) == 9
        assert sum(payload["cache_write_tokens"] for payload in payloads) == 3
        for payload in payloads:
            assert set(payload) == {
                "schema_version",
                "event",
                "event_id",
                "install_id",
                "opensquilla_version",
                "day",
                "sent_at",
                "conversation_turns",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cache_write_tokens",
            }
    finally:
        for storage in stores:
            await storage.close()


async def test_lost_ack_retry_keeps_identity_after_database_move(tmp_path, monkeypatch, collector):
    _enable_telemetry_for_test(monkeypatch)
    original_path = tmp_path / "original.db"
    moved_path = tmp_path / "relocated" / "sessions.db"
    config = _config(tmp_path)
    storage = await SessionStorage.open(str(original_path))
    try:
        await _record_turns(storage, 2)
        collector.lose_next_ack = True
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 0
        )
        identity = await storage.get_runtime_preference(_IDENTITY_KEY)
        assert identity
        assert len(await storage.list_pending_daily_usage(before_day=_TODAY.isoformat())) == 1
        assert len(collector.events) == 1
    finally:
        await storage.close()

    moved_path.parent.mkdir()
    original_path.replace(moved_path)
    monkeypatch.setattr(
        install_telemetry, "_collect_mac_address_candidates", lambda: ["02:00:00:00:00:22"]
    )
    config = _config(moved_path.parent)
    storage = await SessionStorage.open(str(moved_path))
    try:
        assert await storage.get_runtime_preference(_IDENTITY_KEY) == identity
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 1
        )
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 0
        )
        assert await storage.list_pending_daily_usage(before_day=_TODAY.isoformat()) == []
    finally:
        await storage.close()

    assert len(collector.attempts) == 2
    assert collector.attempts[0]["event_id"] == collector.attempts[1]["event_id"]
    assert collector.attempts[0]["install_id"] != collector.attempts[1]["install_id"]
    assert len(collector.events) == 1
    assert sum(payload["conversation_turns"] for payload in collector.events.values()) == 2
    assert sum(payload["input_tokens"] for payload in collector.events.values()) == 20


async def test_connections_to_same_database_choose_one_identity(tmp_path):
    path = str(tmp_path / "sessions.db")
    first = await SessionStorage.open(path)
    second = await SessionStorage.open(path)
    try:
        assert await first.get_runtime_preference(_IDENTITY_KEY) is None
        identities = await asyncio.gather(
            *(storage.ensure_daily_usage_store_id() for storage in (first, second) * 8)
        )
        assert identities[0]
        assert len(set(identities)) == 1
        assert await first.get_runtime_preference(_IDENTITY_KEY) == identities[0]
        assert await second.get_runtime_preference(_IDENTITY_KEY) == identities[0]
    finally:
        await first.close()
        await second.close()


async def test_independent_memory_databases_choose_distinct_identities():
    first = await SessionStorage.open(":memory:")
    second = await SessionStorage.open(":memory:")
    try:
        identities = await asyncio.gather(
            first.ensure_daily_usage_store_id(), second.ensure_daily_usage_store_id()
        )
        assert all(identities)
        assert identities[0] != identities[1]
        assert await first.ensure_daily_usage_store_id() == identities[0]
        assert await second.ensure_daily_usage_store_id() == identities[1]
    finally:
        await first.close()
        await second.close()


async def test_existing_daily_rows_survive_upgrade_without_replaying_successes(
    tmp_path, monkeypatch, collector
):
    _enable_telemetry_for_test(monkeypatch)
    path = tmp_path / "legacy.db"
    legacy_rows = [
        ("2026-07-18", 9, 90, 18, 27, 9, 1, 123),
        ("2026-07-19", 1, 10, 2, 3, 1, 2, None),
        ("2026-07-20", 2, 20, 4, 6, 2, 3, None),
    ]
    _write_legacy_daily_rows(path, legacy_rows)

    config = _config(tmp_path)
    storage = await SessionStorage.open(str(path))
    try:
        assert await storage.get_runtime_preference(_IDENTITY_KEY) is None
        with sqlite3.connect(path) as conn:
            assert (
                conn.execute("SELECT * FROM telemetry_daily_usage ORDER BY day").fetchall()
                == legacy_rows
            )
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 2
        )
        identity = await storage.get_runtime_preference(_IDENTITY_KEY)
        assert identity
        assert [payload["day"] for payload in collector.attempts] == [
            "2026-07-19",
            "2026-07-20",
        ]
        assert sum(payload["conversation_turns"] for payload in collector.events.values()) == 3
        with sqlite3.connect(path) as conn:
            assert (
                conn.execute(
                    "SELECT * FROM telemetry_daily_usage WHERE day = ?", ("2026-07-18",)
                ).fetchone()
                == legacy_rows[0]
            )
    finally:
        await storage.close()

    storage = await SessionStorage.open(str(path))
    try:
        assert await storage.get_runtime_preference(_IDENTITY_KEY) == identity
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 0
        )
        assert len(collector.attempts) == 2
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "veto_env",
    [
        None,
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        network_policy.LEGACY_TELEMETRY_DISABLED_ENV,
        network_policy.LEGACY_UPDATE_CHECK_DISABLED_ENV,
        network_policy.DO_NOT_TRACK_ENV,
        network_policy.PRODUCT_ANALYTICS_DISABLED_ENV,
        "CI",
    ],
)
async def test_opt_out_does_not_create_store_identity(tmp_path, monkeypatch, collector, veto_env):
    _enable_telemetry_for_test(monkeypatch)
    config = _config(tmp_path, disabled=veto_env is None)
    if veto_env is not None:
        monkeypatch.setenv(veto_env, "true")
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _record_turns(storage)
        pending = await storage.list_pending_daily_usage(before_day=_TODAY.isoformat())
        assert await storage.get_runtime_preference(_IDENTITY_KEY) is None
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 0
        )
        assert await storage.get_runtime_preference(_IDENTITY_KEY) is None
        assert await storage.list_pending_daily_usage(before_day=_TODAY.isoformat()) == pending
        assert not (tmp_path / "state" / "install_telemetry.json").exists()
        assert collector.attempts == []
    finally:
        await storage.close()


async def test_standalone_counter_store_concurrent_writers_survive_reopen(tmp_path):
    path = tmp_path / "state" / "telemetry" / "standalone-usage.sqlite3"
    stores = await asyncio.gather(*(DailyUsageStore.open(path) for _ in range(4)))
    try:
        identities = await asyncio.gather(
            *(storage.ensure_daily_usage_store_id() for storage in stores)
        )
        assert len(set(identities)) == 1
        assert re.fullmatch(r"[0-9a-f]{32}", identities[0])
        await asyncio.gather(*(_record_turns(storage, 8) for storage in stores))
    finally:
        await asyncio.gather(*(storage.close() for storage in stores))

    reopened = await DailyUsageStore.open(path)
    try:
        assert await reopened.ensure_daily_usage_store_id() == identities[0]
        [row] = await reopened.list_pending_daily_usage(before_day=_TODAY.isoformat())
        assert row["conversation_turns"] == 32
        assert row["input_tokens"] == 320
        assert row["output_tokens"] == 64
        assert row["cached_tokens"] == 96
        assert row["cache_write_tokens"] == 32
        with sqlite3.connect(path) as connection:
            assert {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } == {"telemetry_daily_usage", "runtime_preferences"}
    finally:
        await reopened.close()


async def test_standalone_counter_store_waits_for_another_connection_writer(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage.sqlite3"
    storage = await DailyUsageStore.open(path)
    holder = sqlite3.connect(path)
    write_started = threading.Event()
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(statement):
            if statement.lstrip().startswith("INSERT INTO telemetry_daily_usage"):
                write_started.set()

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    holder.execute("BEGIN IMMEDIATE")
    write = asyncio.create_task(_record_turns(storage))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        # Start the hold period only once the real writer reaches SQLite.
        # This exceeds the old one-second busy timeout without relying on
        # thread-pool startup time to create contention.
        await asyncio.sleep(1.2)
        assert not write.done()
        holder.rollback()
        await asyncio.wait_for(write, timeout=5)
    finally:
        holder.close()
        await asyncio.gather(write, return_exceptions=True)
        await storage.close()

    reopened = await DailyUsageStore.open(path)
    try:
        [row] = await reopened.list_pending_daily_usage(before_day=_TODAY.isoformat())
        assert row["conversation_turns"] == 1
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 2
        assert row["cached_tokens"] == 3
        assert row["cache_write_tokens"] == 1
    finally:
        await reopened.close()


async def test_standalone_counter_store_ack_preserves_newer_totals(tmp_path):
    storage = await DailyUsageStore.open(tmp_path / "usage.sqlite3")
    try:
        await _record_turns(storage)
        assert await storage.list_pending_daily_usage(before_day="2026-07-20") == []
        [snapshot] = await storage.list_pending_daily_usage(before_day=_TODAY.isoformat())
        await _record_turns(storage)
        assert not await storage.mark_daily_usage_uploaded(
            day="2026-07-20",
            uploaded_at=100,
            expected_conversation_turns=snapshot["conversation_turns"],
        )
        [newer] = await storage.list_pending_daily_usage(before_day=_TODAY.isoformat())
        assert newer["conversation_turns"] == 2
        assert await storage.mark_daily_usage_uploaded(
            day="2026-07-20", uploaded_at=101, expected_conversation_turns=2
        )
        assert await storage.list_pending_daily_usage(before_day=_TODAY.isoformat()) == []
    finally:
        await storage.close()
        await storage.close()


async def test_standalone_counter_store_lost_ack_replays_after_move(
    tmp_path, monkeypatch, collector
):
    _enable_telemetry_for_test(monkeypatch)
    path = tmp_path / "usage.sqlite3"
    storage = await DailyUsageStore.open(path)
    config = _config(tmp_path)
    try:
        await _record_turns(storage, 2)
        collector.lose_next_ack = True
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 0
        )
    finally:
        await storage.close()

    moved_path = tmp_path / "moved.sqlite3"
    path.replace(moved_path)
    storage = await DailyUsageStore.open(moved_path)
    try:
        assert (
            await usage_telemetry.upload_pending_daily_usage(storage, config=config, today=_TODAY)
            == 1
        )
        assert len(collector.events) == 1
        assert len(collector.attempts) == 2
        assert collector.attempts[0]["event_id"] == collector.attempts[1]["event_id"]
        assert collector.attempts[1]["conversation_turns"] == 2
        assert await storage.list_pending_daily_usage(before_day=_TODAY.isoformat()) == []
    finally:
        await storage.close()


async def test_standalone_counter_store_does_not_replace_damaged_identity(tmp_path):
    path = tmp_path / "usage.sqlite3"
    storage = await DailyUsageStore.open(path)
    try:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO runtime_preferences VALUES (?, ?, ?)",
                (_IDENTITY_KEY, "synthetic-damaged-identity", 1),
            )
        with pytest.raises(ValueError, match="Invalid daily usage store identity"):
            await storage.ensure_daily_usage_store_id()
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT preference_value FROM runtime_preferences WHERE preference_key = ?",
                (_IDENTITY_KEY,),
            ).fetchone()[0] == "synthetic-damaged-identity"
    finally:
        await storage.close()


async def test_standalone_counter_store_close_drains_repeatedly_cancelled_write(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage.sqlite3"
    storage = await DailyUsageStore.open(path)
    entered = threading.Event()
    release = threading.Event()
    with_connection = storage._with_connection

    def blocked_write(operation):
        entered.set()
        assert release.wait(timeout=5)
        return with_connection(operation)

    monkeypatch.setattr(storage, "_with_connection", blocked_write)
    write = asyncio.create_task(_record_turns(storage))
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        write.cancel()
        await asyncio.sleep(0)
        write.cancel()
        await asyncio.sleep(0)
        close = asyncio.create_task(storage.close())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
        if close is not None:
            await close
        await storage.close()

    restored = await DailyUsageStore.open(path)
    try:
        [row] = await restored.list_pending_daily_usage(before_day=_TODAY.isoformat())
        assert row["conversation_turns"] == 1
    finally:
        await restored.close()
