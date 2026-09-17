"""Minimal background heartbeat loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.asyncio_utils import create_background_task
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
    "Process any queued system events. If nothing needs attention, reply HEARTBEAT_OK."
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

    def _snapshot_cfg(self) -> dict[str, Any]:
        """Capture configured values once per tick; retired files have no effect."""
        cfg = getattr(self._config, "heartbeat", None)
        return {
            "enabled": getattr(cfg, "enabled", False),
            "interval_ms": getattr(cfg, "interval_ms", 30 * 60 * 1000),
            "target": getattr(cfg, "target", "last"),
            "prompt": getattr(cfg, "prompt", None),
            "ack_max_chars": getattr(cfg, "ack_max_chars", 300),
            "light_context": getattr(cfg, "light_context", False),
            "to": getattr(cfg, "to", ""),
            "account_id": getattr(cfg, "account_id", ""),
            "thread_id": getattr(cfg, "thread_id", ""),
        }

    async def start(self) -> None:
        if self._started:
            return
        cfg = getattr(self._config, "heartbeat", None)
        if getattr(cfg, "enabled", False) or getattr(cfg, "config_path", None) is not None:
            log.warning(
                "heartbeat_loop.workspace_file_retired",
                detail="HEARTBEAT.md body and frontmatter are ignored; only heartbeat "
                "configuration applies. Old file-based disabling, quiet hours and "
                "empty-file suppression no longer apply.",
            )
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
