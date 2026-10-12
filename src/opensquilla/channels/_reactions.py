"""Capability-gated channel status reactions."""

from __future__ import annotations

# fmt: off
# ruff: noqa: E501,E701,E702
from collections import defaultdict
from typing import Any, Protocol

from opensquilla.channels.types import IncomingMessage

SLACK_STATUS_EMOJI = {"received": "white_check_mark", "running": "eyes", "failed": "x"}
FEISHU_STATUS_EMOJI = {"received": "OK", "running": "EYES", "failed": "X"}

class StatusReactor(Protocol):
    async def received(self, message: IncomingMessage) -> None: ...
    async def running(self, message: IncomingMessage) -> None: ...
    async def failed(self, message: IncomingMessage) -> None: ...
    async def completed(self, message: IncomingMessage) -> None: ...

class NullStatusReactor:
    async def received(self, message: IncomingMessage) -> None: return None
    running = failed = completed = received

class _BaseStatusReactor:
    def __init__(self, adapter: str, logger: Any) -> None:
        self._adapter = adapter; self._log = logger; self._disabled = False; self._active: dict[str, list[Any]] = defaultdict(list)
    async def received(self, message: IncomingMessage) -> None: await self._add_state(message, "received")
    async def running(self, message: IncomingMessage) -> None: await self._add_state(message, "running")
    async def failed(self, message: IncomingMessage) -> None: await self._add_state(message, "failed")
    async def completed(self, message: IncomingMessage) -> None:
        key = self._message_key(message)
        for token in self._active.pop(key, []):
            try:
                await self._remove(token)
            except Exception as exc:
                self._warn_failure("remove", exc)
                self._disable(f"remove_failed:{type(exc).__name__}")
    async def _add_state(self, message: IncomingMessage, state: str) -> None:
        if self._disabled:
            return
        try:
            token = await self._add(message, state)
        except Exception as exc:
            self._warn_failure(f"add:{state}", exc)
            self._disable(f"add_failed:{type(exc).__name__}")
            return
        if token is not None:
            self._active[self._message_key(message)].append(token)
    async def _add(self, message: IncomingMessage, state: str) -> Any: raise NotImplementedError
    async def _remove(self, token: Any) -> None: raise NotImplementedError
    @staticmethod
    def _message_key(message: IncomingMessage) -> str:
        metadata = message.metadata or {}
        for key in ("message_id", "ts", "thread_ts"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
        return f"{message.channel_id}:{message.sender_id}:{id(message)}"
    def _disable(self, reason: str) -> None:
        if not self._disabled:
            self._disabled = True; self._log.warning("channel.status_reaction_disabled", adapter=self._adapter, reason=reason)
    def _warn_failure(self, operation: str, exc: Exception) -> None:
        self._log.warning("channel.status_reaction_failed", adapter=self._adapter, operation=operation, error_type=type(exc).__name__, error=str(exc))

class SlackStatusReactor(_BaseStatusReactor):
    def __init__(self, channel: Any, logger: Any) -> None:
        super().__init__("slack", logger); self._channel = channel
    async def _remove(self, payload: dict[str, str]) -> None: await self._post("/reactions.remove", payload)
    async def _add(self, message: IncomingMessage, state: str) -> dict[str, str] | None:
        ts = message.metadata.get("ts") or message.metadata.get("thread_ts")
        if not isinstance(ts, str) or not ts: return None
        payload = {"channel": message.channel_id, "timestamp": ts, "name": SLACK_STATUS_EMOJI[state]}
        return payload if await self._post("/reactions.add", payload) else None
    async def _post(self, path: str, payload: dict[str, str]) -> bool:
        resp = await self._channel._get_client().post(path, json=payload)
        if resp.status_code == 403: self._disable("missing_oauth_scope"); return False
        resp.raise_for_status(); data = resp.json()
        if data.get("ok"): return True
        if data.get("error") in {"missing_scope", "not_allowed_token_type"}: self._disable("missing_oauth_scope"); return False
        raise RuntimeError(f"Slack API error: {data.get('error')}")

class FeishuStatusReactor(_BaseStatusReactor):
    def __init__(self, channel: Any, logger: Any) -> None:
        super().__init__("feishu", logger); self._channel = channel
    async def _remove(self, token: tuple[str, str]) -> None:
        message_id, reaction_id = token
        resp = await self._channel._get_client().delete(f"/im/v1/messages/{message_id}/reactions/{reaction_id}", headers=await self._channel._auth_headers())
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg')}")
    async def _add(self, message: IncomingMessage, state: str) -> tuple[str, str] | None:
        message_id = message.metadata.get("message_id")
        if not isinstance(message_id, str) or not message_id: return None
        emoji_type = FEISHU_STATUS_EMOJI[state]
        resp = await self._channel._get_client().post(f"/im/v1/messages/{message_id}/reactions", json={"reaction_type": {"emoji_type": emoji_type}}, headers=await self._channel._auth_headers())
        if resp.status_code == 403: self._disable("missing_oauth_scope"); return None
        resp.raise_for_status(); data = resp.json()
        if data.get("code") != 0:
            reason = "missing_oauth_scope" if "scope" in str(data.get("msg", "")).lower() else "invalid_emoji_type"
            self._disable(reason); return None
        reaction_id = data.get("data", {}).get("reaction_id")
        if not isinstance(reaction_id, str) or not reaction_id: self._disable("missing_reaction_id"); return None
        return (message_id, reaction_id)

NULL_STATUS_REACTOR = NullStatusReactor()
