"""Task-style heartbeat execution for main-session automation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from opensquilla.engine.types import done_text_snapshot
from opensquilla.scheduler.delivery import infer_delivery


@dataclass
class HeartbeatRunResult:
    status: str
    session_key: str
    summary: str | None = None
    delivery_status: str = "skipped"
    reason: str | None = None
    ran_at_ms: int | None = None


class HeartbeatService:
    def __init__(
        self,
        *,
        turn_runner: Any,
        session_storage: Any,
        channel_manager_ref: Any,
    ) -> None:
        self._turn_runner = turn_runner
        self._session_storage = session_storage
        self._channel_manager_ref = channel_manager_ref
        self._last_run_status: dict[str, Any] | None = None

    @property
    def last_run_status(self) -> dict[str, Any] | None:
        return self._last_run_status

    def record_skip(self, *, session_key: str, reason: str) -> None:
        ran_at_ms = int(time.time() * 1000)
        self._last_run_status = {
            "ts": ran_at_ms,
            "status": "skipped",
            "reason": reason,
            "session_key": session_key,
        }

    async def run_once(
        self,
        *,
        reason: str,
        agent_id: str,
        session_key: str,
        prompt: str,
        target: str = "last",
        delivery_override: dict[str, str] | None = None,
        turn_source: dict[str, Any] | None = None,
        tool_context: Any = None,
        timeout: float | None = None,
        heartbeat_ack_max_chars: int = 300,
        heartbeat_light_context: bool = False,
    ) -> HeartbeatRunResult:
        summary = await self._collect_output(
            prompt=prompt,
            session_key=session_key,
            agent_id=agent_id,
            tool_context=tool_context,
            timeout=timeout,
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            heartbeat_light_context=heartbeat_light_context,
        )

        if target == "none":
            ran_at_ms = int(time.time() * 1000)
            result = HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                summary=summary,
                reason="delivery_disabled",
                ran_at_ms=ran_at_ms,
            )
            self._last_run_status = {
                "ts": ran_at_ms,
                "status": result.status,
                "reason": result.reason,
            }
            return result

        if not summary.strip():
            ran_at_ms = int(time.time() * 1000)
            result = HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                summary=summary,
                reason="heartbeat_ack",
                ran_at_ms=ran_at_ms,
            )
            self._last_run_status = {
                "ts": ran_at_ms,
                "status": result.status,
                "reason": result.reason,
            }
            return result

        override = {
            key: value
            for key, value in (delivery_override or {}).items()
            if isinstance(value, str) and value
        }
        turn_source_thread_id = (
            turn_source.get("thread_id")
            if turn_source and isinstance(turn_source.get("thread_id"), str)
            else ""
        )

        if target == "last":
            delivery = await infer_delivery(self._session_storage, session_key, None)
            if override.get("channel_name"):
                delivery.channel_name = override["channel_name"]
            if override.get("channel_id"):
                delivery.channel_id = override["channel_id"]
            if override.get("account_id"):
                delivery.account_id = override["account_id"]
            if override.get("thread_id"):
                delivery.thread_id = override["thread_id"]
            elif not turn_source_thread_id:
                delivery.thread_id = ""
        elif isinstance(target, str) and target.strip():
            delivery = await infer_delivery(
                self._session_storage,
                session_key,
                {
                    "channel_name": override.get("channel_name", target.strip()),
                    "channel_id": override.get("channel_id", ""),
                    "account_id": override.get("account_id", ""),
                    "thread_id": override.get("thread_id", ""),
                },
            )
        else:
            result = HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                summary=summary,
                reason="unsupported_target",
                ran_at_ms=int(time.time() * 1000),
            )
            self._last_run_status = {
                "ts": result.ran_at_ms,
                "status": result.status,
                "reason": result.reason,
            }
            return result

        if not delivery.channel_name:
            result = HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                summary=summary,
                reason="no_delivery_target",
                ran_at_ms=int(time.time() * 1000),
            )
            self._last_run_status = {
                "ts": result.ran_at_ms,
                "status": result.status,
                "reason": result.reason,
            }
            return result

        if turn_source_thread_id:
            delivery.thread_id = turn_source_thread_id

        ran_at_ms = int(time.time() * 1000)
        delivery_error = await self._send_delivery(delivery, summary or "")
        if delivery_error is not None:
            result = HeartbeatRunResult(
                status="skipped",
                session_key=session_key,
                summary=summary,
                delivery_status=delivery_error,
                reason=delivery_error,
                ran_at_ms=ran_at_ms,
            )
            self._last_run_status = {
                "ts": ran_at_ms,
                "status": result.status,
                "reason": result.reason,
            }
            return result

        result = HeartbeatRunResult(
            status="delivered",
            session_key=session_key,
            summary=summary,
            delivery_status="delivered|ws:skipped|fwd:skipped",
            ran_at_ms=ran_at_ms,
        )
        self._last_run_status = {"ts": ran_at_ms, "status": result.status, "reason": reason}
        return result

    async def _collect_output(
        self,
        *,
        prompt: str,
        session_key: str,
        agent_id: str,
        tool_context: Any,
        timeout: float | None,
        heartbeat_ack_max_chars: int,
        heartbeat_light_context: bool,
    ) -> str:
        parts: list[str] = []
        done_text_present = False
        done_text = ""
        async for event in self._turn_runner.run(
            message=prompt,
            session_key=session_key,
            tool_context=tool_context,
            agent_id=agent_id,
            timeout=timeout,
            input_mode="system_event",
            persist_input=False,
            history_has_persisted_user=False,
            run_kind="heartbeat",
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            bootstrap_context_mode=("heartbeat_light" if heartbeat_light_context else None),
        ):
            kind = getattr(event, "kind", "")
            if kind == "done":
                done_text_present, done_text = done_text_snapshot(event)
                continue
            if kind not in {"state_change", "tool_use_start", "tool_result"}:
                text = getattr(event, "text", "")
                if text:
                    parts.append(text)
        summary = done_text if done_text_present else "".join(parts)

        # Most providers publish an authoritative terminal text snapshot, which
        # TurnRunner already normalizes for heartbeat runs.  Legacy/custom
        # providers may finish with an empty DoneEvent and force this service to
        # fall back to streamed deltas instead.  Normalize again at the delivery
        # boundary so that fallback can never reintroduce private <think> text.
        from opensquilla.engine.runtime import _normalize_heartbeat_text

        return _normalize_heartbeat_text(
            summary,
            run_kind="heartbeat",
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
        )

    async def _send_delivery(self, delivery: Any, text: str) -> str | None:
        manager = self._channel_manager_ref()
        if manager is None:
            return "channel_manager_unavailable"

        adapter = None
        resolved_authoritatively = False
        channel_name = delivery.channel_name
        channel_id = delivery.channel_id
        thread_id = delivery.thread_id

        resolver = getattr(manager, "resolve_delivery_target", None)
        if callable(resolver):
            resolved = resolver(
                target=delivery.channel_name,
                to=delivery.channel_id,
                account_id=delivery.account_id,
                thread_id=delivery.thread_id,
            )
            if isinstance(getattr(resolved, "ok", None), bool):
                resolved_authoritatively = True
                if not resolved.ok:
                    return getattr(resolved, "reason", None) or "unsupported_target"
                adapter = getattr(resolved, "adapter", None)
                channel_name = getattr(resolved, "channel_type", "") or channel_name
                channel_id = getattr(resolved, "to", "") or channel_id
                thread_id = getattr(resolved, "thread_id", "") or ""

        if adapter is None:
            if resolved_authoritatively:
                return "unsupported_target"
            adapter = manager.get(delivery.channel_name)
            if adapter is None:
                return "unsupported_target"

        from opensquilla.channels.types import OutgoingMessage

        if channel_name == "slack":
            if thread_id:
                metadata = {"channel": channel_id} if channel_id else {}
                msg = OutgoingMessage(
                    content=text,
                    reply_to=thread_id,
                    metadata=metadata,
                )
            else:
                metadata = {"thread_ts": None}
                if channel_id:
                    metadata["channel"] = channel_id
                msg = OutgoingMessage(
                    content=text,
                    reply_to=("cron" if channel_id else None),
                    metadata=metadata,
                )
        else:
            msg = OutgoingMessage(content=text, reply_to=channel_id or None)
        try:
            await adapter.send(msg)
        except Exception:
            return "delivery_failed"
        return None
