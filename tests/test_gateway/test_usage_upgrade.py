"""Upgrade and boot contracts for the durable Usage ledger.

These tests exercise the production order used by an existing installation:
yoyo migrations first, ``SessionStorage.connect`` second, and the one-time
legacy cutover before a provider-backed ``TurnRunner`` can be constructed.
"""

from __future__ import annotations

import inspect
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.boot import build_services
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import PROTOCOL_VERSION
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.usage_backfill import run_usage_backfill
from opensquilla.gateway.websocket import _build_features
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import _CREATE_SESSIONS, SessionStorage
from opensquilla.tools.registry import ToolRegistry
from opensquilla.usage_reasons import normalize_usage_unknown_reason

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
V021_ID = "V021__usage_ledger"

_MIGRATION_NUMBER = re.compile(r"^V(?P<number>\d{3})__")
_YOYO_TABLES = {"_yoyo_migration", "_yoyo_log", "_yoyo_version"}
_V007_COST_COLUMNS = {
    "total_cost_usd",
    "billed_cost_usd",
    "estimated_cost_component_usd",
    "cost_source",
    "missing_cost_entries",
}


def _legacy_sessions_ddl() -> str:
    """Return the supported pre-migration session shape used by 0.4 clients."""

    omitted = _V007_COST_COLUMNS | {"schema_version", "epoch"}
    lines: list[str] = []
    for line in _CREATE_SESSIONS.strip().splitlines():
        stripped = line.strip()
        column = stripped.split(maxsplit=1)[0].rstrip(",") if stripped else ""
        if column in omitted:
            continue
        lines.append(line)

    # ``epoch`` is the final current column. Removing it leaves the preceding
    # legacy ``agent_id`` definition with a trailing comma.
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == ")":
            continue
        lines[index] = lines[index].rstrip().removesuffix(",")
        break
    ddl = "\n".join(lines)
    for column in omitted:
        assert not re.search(rf"^\s*{re.escape(column)}\s", ddl, re.MULTILINE)
    return ddl


def _migration_number(path: Path) -> int:
    match = _MIGRATION_NUMBER.match(path.name)
    assert match is not None, path.name
    return int(match.group("number"))


def _copy_migrations_through(target: Path, version: int) -> None:
    target.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        if _migration_number(path) <= version:
            shutil.copy2(path, target / path.name)


def _create_synthetic_legacy_db(
    db_path: Path,
    tmp_path: Path,
    *,
    applied_through: int | None,
) -> None:
    """Create one synthetic historical session and optional yoyo history."""

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_legacy_sessions_ddl())
        conn.execute(
            """
            INSERT INTO sessions (
                session_key, session_id, created_at, updated_at,
                input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                cache_read, cache_write, agent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "agent:main:usage-upgrade",
                "legacy-session",
                1_700_000_000_000,
                1_700_000_001_000,
                11,
                5,
                16,
                0.0092,
                3,
                2,
                "main",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if applied_through is not None:
        historical_migrations = tmp_path / f"migrations-v{applied_through:03d}"
        _copy_migrations_through(historical_migrations, applied_through)
        applied = apply_pending(str(db_path), historical_migrations)
        assert applied

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "usage_events" not in tables
        # No-yoyo 0.4 fixtures intentionally have none of these tables yet.
        if applied_through is None:
            assert tables.isdisjoint(_YOYO_TABLES)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("fixture_name", "applied_through"),
    (
        ("0.4", None),
        ("pre-v010", 9),
        ("v010", 10),
        ("v020", 20),
    ),
)
async def test_supported_old_databases_upgrade_and_cut_over_once(
    tmp_path: Path,
    fixture_name: str,
    applied_through: int | None,
) -> None:
    db_path = tmp_path / f"sessions-{fixture_name}.db"
    _create_synthetic_legacy_db(
        db_path,
        tmp_path,
        applied_through=applied_through,
    )

    applied = apply_pending(str(db_path), MIGRATIONS_DIR)
    assert V021_ID in applied
    assert apply_pending(str(db_path), MIGRATIONS_DIR) == []

    storage = await SessionStorage.open(str(db_path))
    try:
        assert await storage.get_usage_ledger_state() is None

        state = await storage.initialize_usage_ledger(1_800_000_000_000)
        repeated = await storage.initialize_usage_ledger(1_900_000_000_000)
        baselines = await storage.list_usage_legacy_baselines()

        assert state.ledger_started_at_ms == 1_800_000_000_000
        assert repeated == state
        assert len(baselines) == 1
        baseline = baselines[0]
        assert baseline.session_id == "legacy-session"
        assert baseline.session_epoch == 0
        assert baseline.input_tokens == 11
        assert baseline.output_tokens == 5
        assert baseline.total_tokens == 16
        assert baseline.cache_read_tokens == 3
        assert baseline.cache_write_tokens == 2
        assert baseline.cost_nanos == 9_200_000
        assert baseline.billed_cost_nanos == 0
        assert baseline.estimated_cost_nanos == 9_200_000
        assert baseline.cost_source == "opensquilla_estimate"

        async with storage.conn.execute("PRAGMA quick_check") as cursor:
            quick_check = await cursor.fetchone()
        assert quick_check is not None
        assert quick_check[0] == "ok"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_v020_database_reaches_service_ready_with_live_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real service builder migrates, cuts over, and returns usable services."""

    db_path = tmp_path / "sessions.db"
    _create_synthetic_legacy_db(db_path, tmp_path, applied_through=20)

    async def build_no_memory_managers(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        "opensquilla.memory.manager.build_memory_managers",
        build_no_memory_managers,
    )
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_FAST_START", "1")
    monkeypatch.setenv("OPENSQUILLA_SCHEDULER_DB", str(tmp_path / "scheduler.db"))
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config,
        tool_registry=ToolRegistry(),
        session_db_path=str(db_path),
        seed_agent_workspaces=False,
    )
    try:
        storage = services.session_manager.storage
        state = await storage.get_usage_ledger_state()
        baselines = await storage.list_usage_legacy_baselines()

        assert services.usage_event_sink is not None
        assert state is not None
        assert state.backfill_status == "pending"
        assert [row.session_id for row in baselines] == ["legacy-session"]
    finally:
        await services.close()
        # build_services is a real boot: it configures the process-global
        # sandbox runtime (with this config's bypass permissions the runtime
        # reports sandbox-disabled, which makes every later ApprovalGate in
        # the same process short-circuit to full-host ALLOW) and binds the
        # approval-queue singleton. Drop both so later tests in this process
        # rebuild them instead of inheriting boot state.
        from opensquilla.gateway.approval_queue import reset_approval_queue
        from opensquilla.sandbox.integration import reset_runtime

        reset_runtime()
        reset_approval_queue()


def test_usage_query_is_advertised_without_protocol_or_legacy_rpc_breakage() -> None:
    dispatcher = get_dispatcher()
    features = _build_features(dispatcher)

    assert PROTOCOL_VERSION == 3
    assert {"usage.status", "usage.cost", "usage.query"} <= set(features.methods)
    for method in ("usage.status", "usage.cost", "usage.query"):
        entry = dispatcher.get_entry(method)
        assert entry is not None
        assert entry.required_scope == "operator.read"


def test_boot_recovery_reason_stays_inside_the_closed_usage_taxonomy() -> None:
    """Restart-orphaned reservations must keep a restart-specific reason.

    ``normalize_usage_unknown_reason`` collapses any string outside the closed
    taxonomy to the generic ``usage_unknown``, which would make restart orphans
    indistinguishable from providers that returned no usage receipt.
    """
    build_source = inspect.getsource(boot.build_services)
    match = re.search(r'await recover_started\(reason="(?P<reason>[^"]+)"\)', build_source)
    assert match is not None, "boot must pass an explicit recovery reason"
    reason = match.group("reason")
    assert normalize_usage_unknown_reason(reason) == reason
    assert reason == "process_restarted"


def test_usage_ledger_boot_order_is_cutover_then_ready_then_backfill() -> None:
    build_source = inspect.getsource(boot.build_services)
    start_source = inspect.getsource(boot.start_gateway_server)

    initialize_index = build_source.index("await session_storage.initialize_usage_ledger()")
    recover_index = build_source.index("await recover_started(")
    provider_index = build_source.index("# ── Provider selector")
    assert initialize_index < recover_index < provider_index

    not_ready_index = start_source.index("app.state.gateway_ready = False")
    ready_index = start_source.index("app.state.gateway_ready = True")
    backfill_index = start_source.index("run_usage_backfill", ready_index)
    assert not_ready_index < ready_index < backfill_index


@pytest.mark.asyncio
async def test_backfill_storage_failure_never_escapes_into_gateway_readiness() -> None:
    calls: list[str] = []

    class UnavailableStorage:
        async def get_usage_ledger_state(self) -> None:
            calls.append("read")
            raise sqlite3.OperationalError("database is locked")

        async def update_usage_backfill_progress(self, **_kwargs: Any) -> None:
            calls.append("record-failure")
            raise sqlite3.OperationalError("database is locked")

    await run_usage_backfill(UnavailableStorage())

    assert calls == ["read", "record-failure"]
