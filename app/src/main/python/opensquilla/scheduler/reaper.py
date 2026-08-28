"""Session reaper — cleans up expired isolated cron sessions."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def _is_isolated_cron_session(key: str) -> bool:
    """Return True if key matches the pattern cron:<job_id>:run:<run_id>."""
    parts = key.split(":")
    return len(parts) == 4 and parts[0] == "cron" and parts[2] == "run"


class SessionReaper:
    """Periodically deletes expired isolated cron sessions from the session store."""

    DEFAULT_RETENTION_SECONDS = 86400  # 24 hours
    MIN_REAP_INTERVAL = 300  # 5 minutes

    def __init__(self, session_store, retention_seconds: int = DEFAULT_RETENTION_SECONDS) -> None:
        self._session_store = session_store
        self._retention = retention_seconds
        self._last_reap: float = 0  # monotonic time

    async def maybe_reap(self) -> None:
        """Reap if MIN_REAP_INTERVAL has elapsed since the last reap."""
        now = time.monotonic()
        if now - self._last_reap < self.MIN_REAP_INTERVAL:
            return
        self._last_reap = now
        await self._do_reap()

    async def _do_reap(self) -> None:
        """Delete expired isolated cron sessions."""
        if self._session_store is None:
            return

        cutoff_ms = int((time.time() - self._retention) * 1000)

        sessions = await self._session_store.list_sessions()
        to_delete: list[str] = []

        for session in sessions:
            key = getattr(session, "session_key", None) or getattr(session, "key", None)
            updated_at = getattr(session, "updated_at", None)
            if key and updated_at is not None:
                if _is_isolated_cron_session(key) and updated_at < cutoff_ms:
                    to_delete.append(key)

        for key in to_delete:
            await self._session_store.delete_session(key)

        if to_delete:
            logger.info("reaper.deleted count=%d", len(to_delete))
