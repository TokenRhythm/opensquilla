"""Local, session-scoped preferences and decision history for JEV routing."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog

from opensquilla.paths import native_io_path

log = structlog.get_logger(__name__)
_HOUR_MS = 3_600_000
_RETENTION_MS = 30 * 24 * _HOUR_MS
_SETTING_NAMES = (
    "enabled", "mode", "model", "provider", "max_prompts_per_hour", "cooldown_turns",
    "confidence_threshold", "timeout_seconds", "loop_interval", "auto_switch_seconds",
    "ensemble_enabled", "session_restart_enabled", "context_restart_ratio",
    "context_restart_min_tokens", "context_restart_min_turns",
)
_WIRE_SETTINGS = {
    "budgetPerHour": "max_prompts_per_hour", "loopInterval": "loop_interval",
    "autoSwitchSeconds": "auto_switch_seconds",
    "ensembleEnabled": "ensemble_enabled", "sessionRestartEnabled": "session_restart_enabled",
    "contextRestartRatio": "context_restart_ratio",
    "contextRestartMinTokens": "context_restart_min_tokens",
    "contextRestartMinTurns": "context_restart_min_turns",
}


def _confirmation_delay(value: Any) -> int:
    """Legacy unlimited confirmations get the supported default deadline."""
    return value if type(value) is int and 1 <= value <= 60 else 10


class JevService:
    """Keep suggestions separate from execution and serialize feedback locally.

    Reads used by routing admission use an in-memory preference projection.
    Database operations are short and guarded across async worker threads.
    No provider credentials or input messages are written to this database.
    """

    def __init__(self, db_path: str | Path = ":memory:", *, clock: Any = None) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._closed = False
        self.persistence_degraded = False
        self.response_lock = asyncio.Lock()
        self._handoff_response_locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}
        self.on_change: Callable[[str], Awaitable[None]] | None = None
        self.on_auto_accept: Callable[[str, str], Awaitable[None]] | None = None
        self.on_deadline: Callable[[str, str], Awaitable[None]] | None = None
        self.defaults_provider: Callable[[], Any] | None = None
        self._global_defaults: Any = None
        self.on_routing_change: Callable[[str], Awaitable[None]] | None = None
        self._checks: dict[str, tuple[str, int, asyncio.Task[Any]]] = {}
        self._generations: dict[str, int] = {}
        self._execution_owners: dict[str, object] = {}
        self._blocked: set[str] = set()
        self._draft_ids: dict[str, str] = {}
        self._auto_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_sessions: dict[str, str] = {}
        if str(db_path) != ":memory:":
            path = native_io_path(db_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            db_path = path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS jev_preferences (
                session_key TEXT PRIMARY KEY, settings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jev_sessions (
                session_key TEXT PRIMARY KEY, turn_index INTEGER NOT NULL DEFAULT 0,
                input_id TEXT NOT NULL DEFAULT '', last_prompt_turn INTEGER,
                runtime_status TEXT NOT NULL DEFAULT 'ready',
                session_id TEXT, session_epoch INTEGER
            );
            CREATE TABLE IF NOT EXISTS jev_decisions (
                id TEXT PRIMARY KEY, session_key TEXT NOT NULL, input_id TEXT NOT NULL,
                routing_revision INTEGER, created_at INTEGER NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(session_key, input_id)
            );
            CREATE INDEX IF NOT EXISTS jev_decisions_session_time
                ON jev_decisions(session_key, created_at DESC);
        """)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jev_sessions)")}
        for name, datatype in (("session_id", "TEXT"), ("session_epoch", "INTEGER")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE jev_sessions ADD COLUMN {name} {datatype}")
        # Even an existing schema can open read-only. Check write admission
        # without modifying a preference before exposing this optional service.
        self._conn.execute("UPDATE jev_preferences SET settings=settings WHERE 0")
        # Draft and running-turn ownership is ephemeral. A restarted process
        # must assess fresh input instead of replaying an old countdown.
        self._conn.execute(
            "UPDATE jev_decisions SET status='expired' WHERE status IN ('pending','accepted')",
        )
        self._conn.execute(
            "UPDATE jev_sessions SET runtime_status='ready' WHERE runtime_status='checking'",
        )
        self._conn.commit()
        self._settings_cache: dict[str, dict[str, Any]] = {}
        for row in self._conn.execute("SELECT * FROM jev_preferences"):
            settings = json.loads(row["settings"])
            if not isinstance(settings, dict):
                raise ValueError("Invalid stored JEV preferences")
            self._settings_cache[str(row["session_key"])] = {
                key: settings[key] for key in _SETTING_NAMES if key in settings
            }

    @staticmethod
    def _defaults(defaults: Any) -> dict[str, Any]:
        if isinstance(defaults, dict):
            return {key: defaults[key] for key in _SETTING_NAMES if key in defaults}
        return {key: getattr(defaults, key) for key in _SETTING_NAMES if hasattr(defaults, key)}

    def is_enabled(self, session_key: str, defaults: Any) -> bool:
        defaults = self._current_defaults(defaults)
        with self._lock:
            return bool(self._settings_cache.get(session_key, {}).get(
                "enabled", self._defaults(defaults).get("enabled", False),
            ))

    async def settings(self, session_key: str, defaults: Any) -> dict[str, Any]:
        defaults = self._current_defaults(defaults)
        with self._lock:
            settings = {**self._defaults(defaults), **self._settings_cache.get(session_key, {})}
            settings["model"] = "NeoHorse-Jev-4B"
            settings.setdefault("loop_interval", 5)
            settings.setdefault("auto_switch_seconds", 10)
            settings["auto_switch_seconds"] = _confirmation_delay(settings["auto_switch_seconds"])
            # Older local previews allowed a wider budget than the public UI.
            # Keep those files readable and expose the supported range.
            settings["max_prompts_per_hour"] = min(
                10, max(0, int(settings.get("max_prompts_per_hour", 3))),
            )
            return settings

    def _current_defaults(self, fallback: Any) -> Any:
        if self.defaults_provider is not None:
            return self.defaults_provider()
        return self._global_defaults if self._global_defaults is not None else fallback

    async def apply_global_settings(self, defaults: Any, changed_fields: set[str]) -> None:
        """Rebase replaced global fields and retire advice made under the old policy."""
        fields = {
            _WIRE_SETTINGS.get(name.removeprefix("jev_router."), name.removeprefix("jev_router."))
            for name in changed_fields
        } & set(_SETTING_NAMES)
        if not fields:
            return

        def commit() -> set[str]:
            with self._lock, self._conn:
                keys = set(self._settings_cache)
                keys.update(row[0] for row in self._conn.execute(
                    "SELECT session_key FROM jev_sessions",
                ))
                keys.update(row[0] for row in self._conn.execute(
                    "SELECT DISTINCT session_key FROM jev_decisions "
                    "WHERE status IN ('pending','accepted')",
                ))
                for key, old in list(self._settings_cache.items()):
                    updated = {name: value for name, value in old.items() if name not in fields}
                    if updated:
                        self._conn.execute(
                            "UPDATE jev_preferences SET settings=? WHERE session_key=?",
                            (json.dumps(updated), key),
                        )
                        self._settings_cache[key] = updated
                    else:
                        self._conn.execute(
                            "DELETE FROM jev_preferences WHERE session_key=?", (key,),
                        )
                        self._settings_cache.pop(key, None)
                self._conn.execute(
                    "UPDATE jev_decisions SET status='expired' "
                    "WHERE status IN ('pending','accepted')",
                )
                return keys

        async with self.response_lock:
            self._global_defaults = defaults
            keys = await asyncio.to_thread(commit)
            keys.update(self._checks)
            keys.update(self._blocked)
            for key in keys:
                await self.cancel_checks(key)
        for key in keys:
            await self.notify(key)

    async def settle_deadline(
        self, session_key: str, decision_id: str, *, defaults: Any = None,
        now_ms: int | None = None,
    ) -> None:
        """Resolve an expired confirmation using the current authorization mode."""
        if self.on_deadline is not None:
            await self.on_deadline(session_key, decision_id)
            return
        if defaults is None and self.defaults_provider is not None:
            defaults = self.defaults_provider()
        if defaults is None:
            defaults = {"enabled": True, "mode": "suggest"}
        async with self.response_lock:
            decision = await self.get_decision(session_key, decision_id)
            if not decision or decision["status"] != "pending":
                return
            if decision.get("action") == "new_session":
                return
            deadline = decision.get("autoSwitchAt")
            now = self._clock() if now_ms is None else now_ms
            if not isinstance(deadline, (int, float)) or deadline > now:
                return
            settings = await self.settings(session_key, defaults)
            if not settings.get("enabled", False):
                await self.expire(session_key, decision_id)
            elif settings.get("mode") == "trust" and self.on_auto_accept is not None:
                await self.on_auto_accept(session_key, decision_id)
            else:
                await self.respond(session_key, decision_id, settings.get("mode") == "trust")
        await self.notify(session_key)

    async def start_check(
        self, session_key: str, fingerprint: str,
        factory: Callable[[int], Awaitable[Any]],
    ) -> asyncio.Task[Any]:
        """Start bounded background work, replacing stale input without waiting for it."""
        existing = self.current_check(session_key, fingerprint)
        if existing is not None:
            return existing
        generation = self._generations.get(session_key, 0) + 1
        self._generations[session_key] = generation
        previous = self._checks.pop(session_key, None)
        if previous is not None and not previous[2].done():
            previous[2].cancel()

        async def run() -> Any:
            try:
                await self.expire_recommendations(
                    session_key, generation=generation, preserve_context_notices=True,
                )
                if self.is_check_current(session_key, generation):
                    return await factory(generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("jev.background_check_failed", reason_code=type(exc).__name__)
                if self.is_check_current(session_key, generation):
                    await self.set_status(session_key, "unavailable")
                    await self.notify(session_key)
            finally:
                await self._finish_check(session_key, generation)
            return None

        task = asyncio.create_task(run(), name="jev-check")
        self._checks[session_key] = (fingerprint, generation, task)
        return task

    def current_check(self, session_key: str, fingerprint: str) -> asyncio.Task[Any] | None:
        check = self._checks.get(session_key)
        return check[2] if check and check[0] == fingerprint and not check[2].cancelled() else None

    def is_check_current(self, session_key: str, generation: int) -> bool:
        return not self._closed and self._generations.get(session_key) == generation

    async def _finish_check(self, session_key: str, generation: int) -> None:
        """Clear a settled check's spinner without replacing a newer status."""
        def commit() -> bool:
            with self._lock:
                if not self.is_check_current(session_key, generation):
                    return False
                with self._conn:
                    cursor = self._conn.execute(
                        "UPDATE jev_sessions SET runtime_status='ready' "
                        "WHERE session_key=? AND runtime_status='checking'", (session_key,),
                    )
                    return cursor.rowcount > 0

        if await asyncio.to_thread(commit):
            await self.notify(session_key)

    def claim_execution(self, session_key: str, owner: object) -> None:
        self._execution_owners[session_key] = owner

    def release_execution(self, session_key: str, owner: object) -> None:
        if self._execution_owners.get(session_key) is owner:
            self._execution_owners.pop(session_key, None)
            self._blocked.discard(session_key)

    def execution_owned(self, session_key: str) -> bool:
        return session_key in self._execution_owners

    async def set_blocked(self, session_key: str, blocked: bool) -> None:
        if blocked:
            self._blocked.add(session_key)
        else:
            self._blocked.discard(session_key)
        await self.notify(session_key)

    def set_draft_id(self, session_key: str, draft_id: str) -> None:
        self._draft_ids[session_key] = draft_id

    async def expire_recommendations(
        self, session_key: str, *, generation: int | None = None,
        preserve_context_notices: bool = False,
    ) -> None:
        def commit() -> None:
            with self._lock, self._conn:
                if generation is not None and not self.is_check_current(session_key, generation):
                    return
                self._conn.execute(
                    "UPDATE jev_decisions SET status='expired' WHERE session_key=? "
                    "AND status IN ('pending','accepted') "
                    "AND (?=0 OR json_extract(payload,'$.action')!='new_session')",
                    (session_key, int(preserve_context_notices)),
                )
        await asyncio.to_thread(commit)

    async def cancel_checks(
        self, session_key: str, *, preserve_context_notices: bool = False,
    ) -> None:
        from opensquilla.engine.cancellation import cancel_task

        generation = self._generations.get(session_key, 0) + 1
        self._generations[session_key] = generation
        check = self._checks.pop(session_key, None)
        self._blocked.discard(session_key)
        self._draft_ids.pop(session_key, None)
        tasks = [check[2]] if check is not None else []
        for identifier, owner in list(self._auto_sessions.items()):
            if owner == session_key:
                timer = self._auto_tasks.pop(identifier, None)
                self._auto_sessions.pop(identifier, None)
                if timer is not None:
                    tasks.append(timer)
        await self.expire_recommendations(
            session_key, generation=generation,
            preserve_context_notices=preserve_context_notices,
        )
        for task in tasks:
            if task is not asyncio.current_task() and not task.done():
                await cancel_task(
                    task, policy="bounded", operation="jev.cancel",
                    grace_seconds=0.25,
                )
        await self._finish_check(session_key, generation)

    async def bind_session(self, session_key: str, session_id: str, epoch: int) -> None:
        """Discard stale suggestions when a session is reset or recreated."""
        if not session_id:
            return
        def commit() -> bool:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT session_id,session_epoch FROM jev_sessions WHERE session_key=?",
                    (session_key,),
                ).fetchone()
                changed = row is not None and row["session_id"] is not None and (
                    row["session_id"] != session_id or row["session_epoch"] != epoch
                )
                if changed:
                    self._conn.execute(
                        "DELETE FROM jev_decisions WHERE session_key=?", (session_key,),
                    )
                    self._conn.execute(
                        "DELETE FROM jev_sessions WHERE session_key=?", (session_key,),
                    )
                self._conn.execute(
                    "INSERT INTO jev_sessions(session_key,session_id,session_epoch) VALUES (?,?,?) "
                    "ON CONFLICT(session_key) DO UPDATE SET "
                    "session_id=excluded.session_id,session_epoch=excluded.session_epoch",
                    (session_key, session_id, epoch),
                )
                return changed
        if await asyncio.to_thread(commit):
            await self.cancel_checks(session_key)

    async def configure(
        self, session_key: str, patch: dict[str, Any], defaults: Any,
    ) -> dict[str, Any]:
        normalized = {_WIRE_SETTINGS.get(key, key): value for key, value in patch.items()}
        if set(normalized) - set(_SETTING_NAMES):
            raise ValueError("Unsupported JEV setting")
        if "enabled" in normalized and not isinstance(normalized["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        for name in ("ensemble_enabled", "session_restart_enabled"):
            if name in normalized and not isinstance(normalized[name], bool):
                raise ValueError(f"{name} must be a boolean")
        if "mode" in normalized and normalized["mode"] not in {"suggest", "trust"}:
            raise ValueError("mode must be suggest or trust")
        if "provider" in normalized and normalized["provider"] != "tokenrhythm":
            raise ValueError("JEV currently requires the tokenrhythm provider")
        if "model" in normalized:
            model = normalized["model"]
            if not isinstance(model, str) or not model.strip() or len(model) > 512:
                raise ValueError("model must be a non-empty bounded string")
            normalized["model"] = "NeoHorse-Jev-4B"
        if "loop_interval" in normalized and (
            isinstance(normalized["loop_interval"], bool)
            or normalized["loop_interval"] not in {3, 5}
        ):
            raise ValueError("loop_interval must be 3 or 5")
        for key, minimum, maximum in (
            ("max_prompts_per_hour", 0, 10), ("cooldown_turns", 0, 100),
            ("auto_switch_seconds", 0, 60),
            ("context_restart_min_tokens", 1024, 2_000_000),
            ("context_restart_min_turns", 2, 1000),
        ):
            value = normalized.get(key)
            if key in normalized and (
                isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
        if "auto_switch_seconds" in normalized:
            normalized["auto_switch_seconds"] = _confirmation_delay(
                normalized["auto_switch_seconds"],
            )
        for key, lower_bound, upper_bound in (
            ("confidence_threshold", 0.0, 1.0), ("timeout_seconds", 0.1, 30.0),
            ("context_restart_ratio", 0.5, 0.95),
        ):
            value = normalized.get(key)
            if key in normalized and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not lower_bound <= value <= upper_bound
            ):
                raise ValueError(f"Invalid {key}")

        def commit() -> None:
            with self._lock, self._conn:
                updated = {**self._settings_cache.get(session_key, {}), **normalized}
                self._conn.execute(
                    "INSERT INTO jev_preferences VALUES (?, ?) ON CONFLICT(session_key) "
                    "DO UPDATE SET settings=excluded.settings",
                    (session_key, json.dumps(updated)),
                )
                if normalized:
                    self._conn.execute(
                        "UPDATE jev_decisions SET status='expired' WHERE session_key=? "
                        "AND status IN ('pending','accepted')", (session_key,),
                    )
                self._settings_cache[session_key] = updated

        async with self.response_lock:
            await asyncio.to_thread(commit)
            if normalized:
                await self.cancel_checks(session_key)
        return await self.settings(session_key, defaults)

    def _expire_pending(self, session_key: str, *, context_notice: bool = False) -> None:
        self._conn.execute(
            "UPDATE jev_decisions SET status='expired' WHERE session_key=? AND status='pending' "
            "AND (json_extract(payload,'$.action')='new_session')=?",
            (session_key, int(context_notice)),
        )

    async def can_evaluate(
        self, session_key: str, input_id: str, *, cooldown_turns: int = 3,
        max_prompts_per_hour: int = 3,
    ) -> bool:
        """Count each durable input once, then check cooldown and prompt budget."""
        def check() -> bool:
            with self._lock, self._conn:
                now = self._clock()
                self._conn.execute(
                    "INSERT OR IGNORE INTO jev_sessions(session_key) VALUES (?)", (session_key,),
                )
                row = self._conn.execute(
                    "SELECT * FROM jev_sessions WHERE session_key=?", (session_key,),
                ).fetchone()
                assert row is not None
                if input_id and row["input_id"] == input_id:
                    return False
                turn = row["turn_index"] + 1
                self._conn.execute(
                    "UPDATE jev_sessions SET turn_index=?,input_id=? WHERE session_key=?",
                    (turn, input_id, session_key),
                )
                if row["last_prompt_turn"] is not None:
                    if turn - row["last_prompt_turn"] <= cooldown_turns:
                        return False
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM jev_decisions WHERE created_at>?",
                    (now - _HOUR_MS,),
                ).fetchone()[0]
                return int(count) < max_prompts_per_hour

        return await asyncio.to_thread(check)

    @staticmethod
    def _wire(row: sqlite3.Row) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(row["payload"])
        payload["status"] = row["status"]
        return payload

    async def publish(
        self, session_key: str, decision: dict[str, Any], *, input_id: str = "",
        routing_revision: int | None = None, max_prompts_per_hour: int = 3,
        check_generation: int | None = None, auto_switch_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Publish one validated decision without changing the session's model."""
        def commit() -> dict[str, Any] | None:
            with self._lock, self._conn:
                if check_generation is not None and not self.is_check_current(
                    session_key, check_generation,
                ):
                    return None
                now = self._clock()
                identity = input_id or uuid.uuid4().hex
                row = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? AND input_id=?",
                    (session_key, identity),
                ).fetchone()
                if row is not None:
                    return self._wire(row)
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM jev_decisions WHERE created_at>?", (now - _HOUR_MS,),
                ).fetchone()[0]
                if count >= max_prompts_per_hour:
                    return None
                payload = copy.deepcopy(decision)
                payload.update(
                    id=str(payload.get("id") or uuid.uuid4().hex), sessionKey=session_key,
                    createdAt=now, status=str(payload.get("status") or "pending"),
                )
                delay = auto_switch_seconds
                if delay is None:
                    delay = self._settings_cache.get(session_key, {}).get(
                        "auto_switch_seconds", 10,
                    )
                if (
                    payload.get("action") in {"switch", "review", "ensemble"}
                    and (payload.get("target") or payload.get("ensemble"))
                ):
                    payload["autoSwitchAt"] = now + _confirmation_delay(delay) * 1000
                self._expire_pending(
                    session_key, context_notice=payload.get("action") == "new_session",
                )
                self._conn.execute(
                    "INSERT INTO jev_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (payload["id"], session_key, identity, routing_revision, now,
                     payload["status"], json.dumps(payload, ensure_ascii=False)),
                )
                if payload.get("action") != "new_session":
                    self._conn.execute(
                        "UPDATE jev_sessions SET last_prompt_turn=turn_index, "
                        "runtime_status='ready' WHERE session_key=?", (session_key,),
                    )
                self._conn.execute("DELETE FROM jev_decisions WHERE created_at<?", (
                    now - _RETENTION_MS,
                ))
                return payload

        record = await asyncio.to_thread(commit)
        if record and record.get("autoSwitchAt") and record["id"] not in self._auto_tasks:
            async def auto_accept() -> None:
                try:
                    delay = max(0, (record["autoSwitchAt"] - self._clock()) / 1000)
                    await asyncio.sleep(delay)
                    await self.settle_deadline(session_key, record["id"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("jev.auto_switch_failed", reason_code=type(exc).__name__)
                finally:
                    self._auto_tasks.pop(record["id"], None)
                    self._auto_sessions.pop(record["id"], None)
            self._auto_sessions[record["id"]] = session_key
            self._auto_tasks[record["id"]] = asyncio.create_task(
                auto_accept(), name="jev-auto-switch",
            )
        return record

    async def get_decision(self, session_key: str, decision_id: str) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? AND id=?",
                    (session_key, decision_id),
                ).fetchone()
                return self._wire(row) if row is not None else None
        return await asyncio.to_thread(read)

    async def routing_revision(self, session_key: str, decision_id: str) -> int | None:
        def read() -> int | None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT routing_revision FROM jev_decisions WHERE session_key=? AND id=?",
                    (session_key, decision_id),
                ).fetchone()
                return row[0] if row is not None else None
        return await asyncio.to_thread(read)

    async def respond(
        self, session_key: str, decision_id: str, accepted: bool,
    ) -> dict[str, Any]:
        return await self._set_status(
            session_key, decision_id, "accepted" if accepted else "dismissed",
        )

    async def _set_status(
        self, session_key: str, decision_id: str, status: str,
        *, routing_revision: int | None = None,
    ) -> dict[str, Any]:
        def commit() -> dict[str, Any]:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? AND id=?",
                    (session_key, decision_id),
                ).fetchone()
                if row is None:
                    raise KeyError("JEV decision not found")
                if row["status"] == status:
                    return self._wire(row)
                if row["status"] not in {"pending", "accepted"}:
                    raise ValueError("JEV decision is no longer pending")
                self._conn.execute(
                    "UPDATE jev_decisions SET status=?,"
                    "routing_revision=COALESCE(?,routing_revision) "
                    "WHERE id=?", (status, routing_revision, decision_id),
                )
                result = self._wire(row)
                result["status"] = status
                return result
        return await asyncio.to_thread(commit)

    async def mark_applied(
        self, session_key: str, decision_id: str, routing_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self._set_status(
            session_key, decision_id, "applied", routing_revision=routing_revision,
        )

    async def mark_handoff_applied(
        self, session_key: str, decision_id: str, target_session_key: str,
    ) -> dict[str, Any]:
        """Record the new session reference; its summary belongs in the new transcript."""
        def commit() -> dict[str, Any]:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? AND id=?",
                    (session_key, decision_id),
                ).fetchone()
                if row is None:
                    raise KeyError("JEV decision not found")
                value = self._wire(row)
                if value.get("action") != "new_session" or row["status"] not in {
                    "accepted", "applied",
                }:
                    raise ValueError("The handoff is no longer approved")
                handoff = dict(value.get("handoff") or {})
                previous = handoff.get("targetSessionKey")
                if previous is not None and previous != target_session_key:
                    raise ValueError("The handoff already points to another session")
                handoff["targetSessionKey"] = target_session_key
                value.update(handoff=handoff, status="applied")
                self._conn.execute(
                    "UPDATE jev_decisions SET status='applied',payload=? WHERE id=?",
                    (json.dumps(value, ensure_ascii=False), decision_id),
                )
                return value
        return await asyncio.to_thread(commit)

    @asynccontextmanager
    async def handoff_response(self, session_key: str, decision_id: str) -> AsyncIterator[None]:
        """Serialize one accepted handoff without delaying other conversations.

        The reference count includes callers waiting for the lock, so a waiter
        cannot be separated from the active operation by premature cleanup.
        Rejection and preference revocation do not acquire this mutex.
        """
        identity = (session_key, decision_id)
        lock, references = self._handoff_response_locks.get(identity, (asyncio.Lock(), 0))
        self._handoff_response_locks[identity] = (lock, references + 1)
        try:
            async with lock:
                yield
        finally:
            _, references = self._handoff_response_locks[identity]
            if references == 1:
                self._handoff_response_locks.pop(identity)
            else:
                self._handoff_response_locks[identity] = (lock, references - 1)

    async def reopen_handoff(self, session_key: str, decision_id: str) -> None:
        """A failed summary can be retried unless its authorization was withdrawn."""
        def commit() -> None:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE jev_decisions SET status='pending' WHERE session_key=? AND id=? "
                    "AND status='accepted' AND json_extract(payload,'$.action')='new_session'",
                    (session_key, decision_id),
                )
        await asyncio.to_thread(commit)

    async def expire(self, session_key: str, decision_id: str) -> dict[str, Any]:
        return await self._set_status(session_key, decision_id, "expired")

    async def applied_target(
        self, session_key: str, routing_revision: int | None,
    ) -> dict[str, str] | None:
        if routing_revision is None:
            return None
        def read() -> dict[str, str] | None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? AND status='applied' "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_key,),
                ).fetchone()
                if row is None or row["routing_revision"] != routing_revision:
                    return None
                target = self._wire(row).get("target")
                return target if isinstance(target, dict) else None
        return await asyncio.to_thread(read)

    async def set_status(self, session_key: str, status: str) -> None:
        if status not in {"ready", "checking", "unavailable"}:
            raise ValueError("Invalid JEV runtime status")
        def commit() -> None:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO jev_sessions(session_key,runtime_status) VALUES (?,?) "
                    "ON CONFLICT(session_key) DO UPDATE SET runtime_status=excluded.runtime_status",
                    (session_key, status),
                )
        await asyncio.to_thread(commit)

    async def snapshot(
        self, session_key: str, defaults: Any, *, available: bool = True,
    ) -> dict[str, Any]:
        settings = await self.settings(session_key, defaults)
        def read() -> tuple[list[dict[str, Any]], str]:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM jev_decisions WHERE session_key=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 30", (session_key,),
                ).fetchall()
                state = self._conn.execute(
                    "SELECT runtime_status FROM jev_sessions WHERE session_key=?", (session_key,),
                ).fetchone()
                return [self._wire(row) for row in rows], state[0] if state else "ready"
        decisions, runtime_status = await asyncio.to_thread(read)
        enabled = bool(settings.get("enabled", False))
        return {
            "sessionKey": session_key, "enabled": enabled,
            "mode": settings.get("mode", "suggest"),
            "budgetPerHour": settings.get("max_prompts_per_hour", 3),
            "model": settings.get("model", "neohorse-4b-jev"),
            "provider": settings.get("provider", "tokenrhythm"),
            "loopInterval": settings["loop_interval"],
            "autoSwitchSeconds": settings["auto_switch_seconds"],
            "contextRestartMinTurns": settings.get("context_restart_min_turns", 20),
            "blocked": session_key in self._blocked,
            "available": available,
            "status": (
                "disabled" if not enabled else "unavailable" if not available else runtime_status
            ),
            "recommendation": next((
                row for row in decisions
                if row["status"] == "pending" and row.get("action") != "new_session"
            ), next((row for row in decisions if row["status"] == "pending"), None)),
            "decisions": decisions,
            **({"draftId": self._draft_ids[session_key]} if session_key in self._draft_ids else {}),
        }

    async def close(self) -> None:
        from opensquilla.engine.cancellation import cancel_task

        self._generations = {key: value + 1 for key, value in self._generations.items()}
        tasks = [check[2] for check in self._checks.values()] + list(self._auto_tasks.values())
        self._checks.clear()
        self._auto_tasks.clear()
        self._auto_sessions.clear()
        for task in tasks:
            if task is not asyncio.current_task() and not task.done():
                await cancel_task(
                    task, policy="bounded", operation="jev.shutdown", grace_seconds=0.25,
                )
        def close() -> None:
            with self._lock:
                if not self._closed:
                    self._conn.close()
                    self._closed = True
                    self.on_change = None
                    self.on_auto_accept = None
                    self.on_deadline = None
                    self.defaults_provider = None
                    self.on_routing_change = None
        await asyncio.to_thread(close)

    async def forget_session(self, session_key: str) -> None:
        """Purge the local decision history with its owning conversation."""
        await self.cancel_checks(session_key)
        def commit() -> None:
            with self._lock, self._conn:
                for table in ("jev_decisions", "jev_preferences", "jev_sessions"):
                    self._conn.execute(f"DELETE FROM {table} WHERE session_key=?", (session_key,))
                self._settings_cache.pop(session_key, None)
        await asyncio.to_thread(commit)

    async def notify(self, session_key: str) -> None:
        """Deliver a best-effort refresh without coupling the runtime to a transport."""
        if self.on_change is not None:
            try:
                await self.on_change(session_key)
            except Exception:
                # Disconnected clients refresh from the durable ledger on reconnect.
                pass

    async def notify_routing_change(self, session_key: str) -> None:
        """Publish an approved live deployment to subscribed control surfaces."""
        if self.on_routing_change is not None:
            try:
                await self.on_routing_change(session_key)
            except Exception:
                pass


def get_jev_service(config: Any, session_manager: Any = None, runner: Any = None) -> JevService:
    """Reuse the runner's local ledger; in-memory session stores stay isolated."""
    owner = runner if runner is not None else session_manager
    existing = getattr(owner, "jev_service", None)
    if isinstance(existing, JevService):
        if hasattr(config, "jev_router"):
            existing.defaults_provider = lambda: config.jev_router
        return existing
    storage = getattr(session_manager, "storage", None)
    if storage is None:
        storage = getattr(session_manager, "_storage", None)
    raw_path = getattr(storage, "_db_path", ":memory:")
    session_path = str(raw_path) if isinstance(raw_path, (str, Path)) else ":memory:"
    ledger_path: str | Path = ":memory:"
    if (
        session_path != ":memory:"
        and "://" not in session_path
        and not session_path.startswith("file:")
    ):
        ledger_path = Path(session_path).parent / "jev.db"
    try:
        service = JevService(ledger_path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Never include database paths, exception messages, or row contents:
        # malformed storage may itself contain secrets or user-authored text.
        log.warning(
            "jev.persistence_unavailable",
            reason_code=type(exc).__name__,
            notice="Temporary JEV history and settings will be lost on restart",
        )
        service = JevService()
        service.persistence_degraded = True
    if owner is not None:
        owner.jev_service = service
    if hasattr(config, "jev_router"):
        service.defaults_provider = lambda: config.jev_router
    return service
