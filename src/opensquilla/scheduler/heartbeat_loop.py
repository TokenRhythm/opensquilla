"""Minimal background heartbeat loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.asyncio_utils import create_background_task
from opensquilla.scheduler.heartbeat import (
    HeartbeatLoopOverrides,
    is_heartbeat_content_effectively_empty,
    parse_loop_overrides,
)
from opensquilla.scheduler.heartbeat_service import HeartbeatRunResult
from opensquilla.session.keys import build_main_key
from opensquilla.tools.types import (
    CRON_AGENT_ALLOW,
    CRON_AGENT_DENY,
    CallerKind,
    InteractionMode,
    ToolContext,
)

log = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_PROMPT = (
    "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. "
    "If nothing needs attention, reply HEARTBEAT_OK."
)


class HeartbeatLoop:
    def __init__(
        self,
        *,
        config: Any,
        heartbeat_service: Any,
    ) -> None:
        self._config = config
        self._heartbeat_service = heartbeat_service
        self._overrides = HeartbeatLoopOverrides()
        self._nudge_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        workspace_dir = resolve_agent_workspace_dir("main", config)
        workspace_strict = getattr(config, "workspace_strict", None)
        if not isinstance(workspace_strict, bool):
            workspace_strict = bool(workspace_dir)
        self._tool_context = ToolContext(
            is_owner=False,
            caller_kind=CallerKind.CRON,
            interaction_mode=InteractionMode.UNATTENDED,
            agent_id="main",
            workspace_dir=str(workspace_dir),
            workspace_strict=workspace_strict,
            session_key=build_main_key("main"),
            channel_kind="cron",
            channel_id="heartbeat",
            sender_id="heartbeat-loop",
            source_kind="scheduler",
            source_name="heartbeat",
            allowed_tools=set(CRON_AGENT_ALLOW),
            denied_tools=set(CRON_AGENT_DENY),
        )

    def nudge(self) -> None:
        self._nudge_event.set()

    def request_now(
        self,
        *,
        reason: str | None = None,
        agent_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Heartbeat wake hook used by cron.

        The current loop has a single nudge queue; reason/agent/session are
        accepted so cron can request a wake without coupling to loop internals.
        """
        self.nudge()

    def apply_overrides(self, overrides: HeartbeatLoopOverrides) -> None:
        """Replace the live HEARTBEAT.md-sourced overrides.

        Called by ``HeartbeatConfigWatcher.reload_now`` on every parse. Pure
        attribute swap — the in-flight tick keeps its snapshot from
        ``_snapshot_cfg`` and is unaffected.
        """
        self._overrides = overrides

    def _snapshot_cfg(self) -> dict[str, Any]:
        """Resolve effective values once per tick: overrides win, config falls back.

        Returned as a plain dict so the tick body cannot accidentally observe
        a hot-reload mid-flight.
        """
        cfg = getattr(self._config, "heartbeat", None)
        ov = self._overrides

        def _pick(name: str, default: Any) -> Any:
            ov_val = getattr(ov, name, None)
            if ov_val is not None:
                return ov_val
            if cfg is None:
                return default
            return getattr(cfg, name, default)

        return {
            "enabled": _pick("enabled", False),
            "interval_ms": _pick("interval_ms", 30 * 60 * 1000),
            "target": _pick("target", "last"),
            "prompt": _pick("prompt", None),
            "ack_max_chars": _pick("ack_max_chars", 300),
            "light_context": _pick("light_context", False),
            "active_hours": ov.active_hours,
            # bootstrap-only fields (not in frontmatter)
            "to": getattr(cfg, "to", "") if cfg is not None else "",
            "account_id": getattr(cfg, "account_id", "") if cfg is not None else "",
            "thread_id": getattr(cfg, "thread_id", "") if cfg is not None else "",
        }

    def _heartbeat_md_path(self) -> Path:
        cfg = getattr(self._config, "heartbeat", None)
        configured = getattr(cfg, "config_path", None) if cfg is not None else None
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
        workspace_dir = getattr(self._config, "workspace_dir", None)
        if isinstance(workspace_dir, str) and workspace_dir.strip():
            return Path(workspace_dir).expanduser() / "HEARTBEAT.md"
        return Path.home() / ".opensquilla" / "workspace" / "HEARTBEAT.md"

    @staticmethod
    def _within_active_hours(window: tuple[int, int] | None, moment: datetime) -> bool:
        if window is None:
            return True
        start, end = window
        hour = moment.hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._task = create_background_task(self._loop())

    async def stop(self) -> None:
        self._started = False
        self._nudge_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        while self._started:
            interval_ms = max(1, int(self._snapshot_cfg()["interval_ms"]))
            self._nudge_event.clear()
            try:
                await asyncio.wait_for(self._nudge_event.wait(), timeout=interval_ms / 1000.0)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            if not self._started:
                break
            await self._tick()

    async def _tick(self) -> None:
        snap = self._snapshot_cfg()
        if not snap["enabled"]:
            return
        if not self._within_active_hours(snap["active_hours"], datetime.now(UTC)):
            return
        heartbeat_md_path = self._heartbeat_md_path()
        heartbeat_file_has_overrides = (
            heartbeat_md_path.is_file() and not parse_loop_overrides(heartbeat_md_path).is_empty()
        )
        if (
            heartbeat_md_path.is_file()
            and not heartbeat_file_has_overrides
            and is_heartbeat_content_effectively_empty(heartbeat_md_path)
        ):
            recorder = getattr(self._heartbeat_service, "record_skip", None)
            if callable(recorder):
                recorder(
                    session_key=build_main_key("main"),
                    reason="empty-heartbeat-file",
                )
            return

        prompt = snap["prompt"] or DEFAULT_HEARTBEAT_PROMPT
        target = snap["target"]
        delivery_override = None
        if target not in {"none", "last"} or snap["to"] or snap["account_id"] or snap["thread_id"]:
            delivery_override = {
                "channel_name": target if target not in {"none", "last"} else "",
                "channel_id": snap["to"],
                "account_id": snap["account_id"],
                "thread_id": snap["thread_id"],
            }

        kwargs = {
            "reason": "heartbeat:loop",
            "agent_id": "main",
            "session_key": build_main_key("main"),
            "prompt": prompt,
            "target": target,
            "heartbeat_ack_max_chars": snap["ack_max_chars"],
            "heartbeat_light_context": snap["light_context"],
            "tool_context": self._tool_context,
        }
        if delivery_override is not None:
            kwargs["delivery_override"] = delivery_override

        try:
            await self._heartbeat_service.run_once(
                **kwargs,
            )
        except Exception:
            log.warning("heartbeat_loop.tick_failed", exc_info=True)

    async def run_once_now(
        self,
        *,
        reason: str,
        agent_id: str,
        session_key: str,
        target: str = "last",
        tool_context: Any = None,
        timeout: float | None = None,
        delivery_override: dict[str, str] | None = None,
    ) -> HeartbeatRunResult:
        """Run one heartbeat immediately with the loop's normal gates.

        For wakeMode="now", the cron event is already queued in the main
        session, then cron asks the heartbeat runner to run once with
        heartbeat.target="last".
        """
        snap = self._snapshot_cfg()
        ran_at_ms = int(datetime.now(UTC).timestamp() * 1000)
        if not snap["enabled"]:
            return HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                reason="disabled",
                ran_at_ms=ran_at_ms,
            )
        if not self._within_active_hours(snap["active_hours"], datetime.now(UTC)):
            return HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                reason="quiet-hours",
                ran_at_ms=ran_at_ms,
            )
        heartbeat_md_path = self._heartbeat_md_path()
        heartbeat_file_has_overrides = (
            heartbeat_md_path.is_file() and not parse_loop_overrides(heartbeat_md_path).is_empty()
        )
        if (
            heartbeat_md_path.is_file()
            and not heartbeat_file_has_overrides
            and is_heartbeat_content_effectively_empty(heartbeat_md_path)
        ):
            recorder = getattr(self._heartbeat_service, "record_skip", None)
            if callable(recorder):
                recorder(session_key=session_key, reason="empty-heartbeat-file")
            return HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                reason="empty-heartbeat-file",
                ran_at_ms=ran_at_ms,
            )

        service_kwargs: dict[str, Any] = {
            "reason": reason,
            "agent_id": agent_id,
            "session_key": session_key,
            "prompt": snap["prompt"] or DEFAULT_HEARTBEAT_PROMPT,
            "target": target,
            "heartbeat_ack_max_chars": snap["ack_max_chars"],
            "heartbeat_light_context": snap["light_context"],
            "tool_context": tool_context or self._tool_context,
            "timeout": timeout,
        }
        if delivery_override is not None:
            service_kwargs["delivery_override"] = delivery_override
        return cast(
            HeartbeatRunResult,
            await self._heartbeat_service.run_once(**service_kwargs),
        )
