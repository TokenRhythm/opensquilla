"""KV cache keep-alive loop for prompt-cache warming.

Idle-timeout mode: only sends keep-alive pings when the session has been
idle (no new user messages) for longer than ``interval_seconds``.

Once keep-alive starts, pings continue at ``interval_seconds`` pace until
the user sends a new message — at which point the idle timer resets and
the keep-alive loop goes silent again (real requests take over warming).

Inspired by claude-code-coffee (https://github.com/cnighswonger/claude-code-coffee).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from opensquilla.compat import aiosqlite
from opensquilla.gateway.config import GatewayConfig, KeepaliveConfig

logger = structlog.get_logger(__name__)

DEFAULT_SESSION_DB = "sessions.db"


def _resolve_db_path(config: GatewayConfig) -> str:
    """Resolve the session database path from the gateway config."""
    state_dir = getattr(config, "state_dir", None)
    if state_dir:
        db_path = Path(state_dir) / DEFAULT_SESSION_DB
    else:
        home = Path.home() / ".opensquilla" / "state"
        db_path = home / DEFAULT_SESSION_DB
    return str(db_path)


class KeepaliveLoop:
    """Background loop that keeps KV cache warm during idle periods.

    State machine:
      ACTIVE   — user is chatting, no pings sent
      IDLE     — no messages for > interval_seconds → start pinging
      KEEPALIVE — pinging at interval_seconds pace until activity resumes

    Activity is detected by monitoring the transcript_entries table's
    latest created_at timestamp. Keep-alive pings do NOT write to the
    session DB, so they never reset the idle timer.
    """

    def __init__(
        self,
        *,
        config: GatewayConfig,
        db_path: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._keepalive_cfg: KeepaliveConfig = config.keepalive
        self._db_path = db_path or _resolve_db_path(config)
        self._http_client = http_client or httpx.AsyncClient(timeout=30.0)
        self._task: asyncio.Task[None] | None = None
        self._started = False

        # Idle tracking
        self._last_ts: int | None = None  # last known transcript timestamp (epoch ms)
        self._is_keepalive_phase = False  # whether we're currently in keep-alive mode
        self._pings_sent = 0  # keep-alive pings sent in current idle session

    async def _get_latest_timestamp(self) -> int | None:
        """Get the latest transcript entry timestamp (epoch ms) for the most recent session."""
        if not os.path.isfile(self._db_path):
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    "SELECT MAX(created_at) FROM transcript_entries"
                )
                row = await cursor.fetchone()
                return row[0] if row and row[0] else None
        except Exception:
            return None

    async def _get_latest_session_messages(self) -> list[dict[str, Any]]:
        """Read the latest active session's transcript entries."""
        if not os.path.isfile(self._db_path):
            return []

        try:
            async with aiosqlite.connect(self._db_path) as db:
                # Find the most recent session
                cursor = await db.execute(
                    """
                    SELECT session_key FROM sessions
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    return []
                session_key = row[0]

                cursor = await db.execute(
                    """
                    SELECT role, content, tool_calls, tool_call_id, reasoning_content
                    FROM transcript_entries
                    WHERE session_key = ?
                    ORDER BY id ASC
                    """,
                    (session_key,),
                )
                rows = await cursor.fetchall()
                if not rows:
                    return []

                messages: list[dict[str, Any]] = []
                for row in rows:
                    role, content, tool_calls_json, tool_call_id, reasoning_content = row
                    msg: dict[str, Any] = {"role": role}
                    if content:
                        msg["content"] = content
                    if tool_calls_json:
                        try:
                            msg["tool_calls"] = json.loads(tool_calls_json)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if tool_call_id:
                        msg["tool_call_id"] = tool_call_id
                    if reasoning_content:
                        msg["reasoning_content"] = reasoning_content
                    messages.append(msg)

                return messages

        except Exception as e:
            logger.warning("keepalive: failed to read session db", error=str(e))
            return []

    @property
    def _llm_config(self) -> dict[str, Any]:
        cfg = self._config.llm
        return {
            "api_key": cfg.api_key or "",
            "base_url": cfg.base_url or "",
            "model": cfg.model or "",
        }

    async def send_keepalive(self) -> bool:
        """Send a single keep-alive ping."""
        messages = await self._get_latest_session_messages()
        if not messages:
            return False

        llm = self._llm_config
        if not llm["base_url"] or not llm["api_key"]:
            return False

        if self._keepalive_cfg.history_turns > 0:
            messages = messages[-self._keepalive_cfg.history_turns:]

        ping_messages = list(messages)
        ping_messages.append({
            "role": "user",
            "content": self._keepalive_cfg.message,
        })

        payload: dict[str, Any] = {
            "model": llm["model"],
            "messages": ping_messages,
            "max_tokens": self._keepalive_cfg.max_tokens,
        }
        if self._keepalive_cfg.tool_choice_none:
            payload["tool_choice"] = "none"

        base = llm["base_url"].rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        elif "/chat/completions" in base:
            url = base
        else:
            url = f"{base}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {llm['api_key']}",
            "Content-Type": "application/json",
        }

        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

            usage = data.get("usage", {})
            hit = usage.get("prompt_cache_hit_tokens", 0)
            miss = usage.get("prompt_cache_miss_tokens", 0)
            total = hit + miss
            hit_pct = 100.0 * hit / total if total > 0 else 0.0

            logger.info(
                "keepalive: ping sent",
                hit_tokens=hit,
                miss_tokens=miss,
                hit_pct=f"{hit_pct:.1f}%",
                total_tokens=total,
                message_count=len(messages),
                cost_cny=data.get("cost_cny", "N/A"),
            )
            return True
        except httpx.HTTPStatusError as e:
            logger.warning("keepalive: HTTP error", status_code=e.response.status_code)
            return False
        except httpx.RequestError as e:
            logger.warning("keepalive: request failed", error=str(e))
            return False
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("keepalive: response parse error", error=str(e))
            return False

    async def _run_loop(self) -> None:
        """Main loop: poll every 10s, detect idle, send pings when idle."""
        interval = self._keepalive_cfg.interval_seconds
        logger.info(
            "keepalive: loop started (idle-timeout mode)",
            idle_timeout_seconds=interval,
            db_path=self._db_path,
        )

        while True:
            await asyncio.sleep(10)  # poll every 10 seconds

            latest_ts = await self._get_latest_timestamp()
            now_ms = int(time.time() * 1000)

            if latest_ts is None:
                # No session data yet
                if self._is_keepalive_phase:
                    self._is_keepalive_phase = False
                    logger.debug("keepalive: no session, exiting keep-alive phase")
                continue

            # Track the latest timestamp
            if self._last_ts is not None and latest_ts > self._last_ts:
                # New message detected! User is active.
                self._last_ts = latest_ts
                self._pings_sent = 0  # reset ping counter
                if self._is_keepalive_phase:
                    self._is_keepalive_phase = False
                    logger.info("keepalive: user activity detected, stopping keep-alive")
                continue

            # Update last_ts on first run
            if self._last_ts is None:
                self._last_ts = latest_ts
                continue

            # Calculate idle time
            idle_ms = now_ms - latest_ts
            idle_seconds = idle_ms / 1000.0

            if idle_seconds < interval:
                # Not idle enough yet
                if self._is_keepalive_phase:
                    # We're in keep-alive phase but idle time was reset
                    # by a new message (already handled above)
                    pass
                continue

            # Idle timeout reached — send keep-alive ping
            if not self._is_keepalive_phase:
                self._is_keepalive_phase = True
                self._pings_sent = 0
                logger.info(
                    "keepalive: entering keep-alive phase",
                    idle_seconds=f"{idle_seconds:.0f}s",
                )

            # Check max pings limit
            max_pings = self._keepalive_cfg.max_pings
            if max_pings > 0 and self._pings_sent >= max_pings:
                if self._pings_sent == max_pings:
                    logger.info(
                        "keepalive: max pings reached, stopping",
                        max_pings=max_pings,
                    )
                    self._pings_sent += 1  # prevent repeated log spam
                continue

            self._pings_sent += 1
            await self.send_keepalive()

    def start(self) -> None:
        if self._started:
            return
        if not self._keepalive_cfg.enabled:
            logger.debug("keepalive: disabled by config")
            return
        self._task = asyncio.create_task(self._run_loop())
        self._started = True
        logger.info("keepalive: started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._started = False
        self._is_keepalive_phase = False
        logger.info("keepalive: stopped")

    async def close(self) -> None:
        await self.stop()
        await self._http_client.aclose()