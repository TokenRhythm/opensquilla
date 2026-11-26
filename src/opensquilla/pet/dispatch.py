"""Directed dispatch: native queue → resolve guard → idempotent send → ack (PR-3).

Reuses the gateway's exact ``sessions.resolve`` / ``sessions.send`` admission
path through the injected :class:`~opensquilla.pet.ports.RpcInvoker` (no
bypass). The idempotency key is derived from ``dispatch_id`` so a re-poll or
retry of the same item carries the same ``client_message_id`` and the gateway
deduplicates it instead of creating a second message.

Failure semantics mirror the external bridge: a timeout/reconnect is
*uncertain* (the gateway may already have admitted the turn) and is reported
differently from a definite RPC rejection (session not in the live table).
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from opensquilla.pet.bridge_client import BridgeClient
from opensquilla.pet.ports import RpcInvoker

log = structlog.get_logger(__name__)

_UNCERTAIN_RE = re.compile(r"timeout|reconnect", re.IGNORECASE)
_SOURCE_NAME = "agentrmc-bridge"


def _is_uncertain(message: str) -> bool:
    return bool(_UNCERTAIN_RE.search(message))


def client_message_id(dispatch_id: str) -> str:
    """Stable idempotency key derived from the dispatch id (no dashes)."""
    return f"agentrmc-{dispatch_id.replace('-', '')}"


class DispatchProcessor:
    """Poll the native dispatch queue and inject accepted items into sessions."""

    def __init__(
        self,
        bridge: BridgeClient,
        rpc_invoke: RpcInvoker,
        *,
        exe: str,
    ) -> None:
        self._bridge = bridge
        self._rpc = rpc_invoke
        self._exe = exe
        self._busy = False

    async def process_once(self) -> int:
        """Fetch and process one batch; return the number of items handled."""
        if self._busy:
            return 0
        self._busy = True
        handled = 0
        try:
            items = await self._bridge.dispatch_pending(self._exe)
            for item in items:
                await self._process_item(item)
                handled += 1
        except Exception as exc:  # noqa: BLE001 - polling must stay quiet
            log.debug("pet.dispatch_poll_failed", error=str(exc))
        finally:
            self._busy = False
        return handled

    async def _process_item(self, item: dict[str, Any]) -> None:
        dispatch_id = str(item.get("dispatch_id") or "")
        session_id = str(item.get("session_id") or "")
        text = str(item.get("text") or "")
        error = ""

        if not dispatch_id or not session_id or not text:
            error = "取件缺字段（dispatch_id/session_id/text）"
        else:
            error = await self._inject(session_id, text, dispatch_id)

        ok = error == ""
        try:
            await self._bridge.dispatch_ack(dispatch_id, ok=ok, error=error)
        except Exception as exc:  # noqa: BLE001 - native cancels on its own timeout
            log.debug("pet.dispatch_ack_failed", error=str(exc))
        log.info(
            "pet.dispatch",
            dispatch_id=dispatch_id[:8],
            session_id=session_id,
            ok=ok,
            error=error or None,
        )

    async def _inject(self, session_id: str, text: str, dispatch_id: str) -> str:
        # Ownership guard: only inject when resolve hits the live session table.
        try:
            await self._rpc("sessions.resolve", {"key": session_id})
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            short = session_id[:24]
            if _is_uncertain(message):
                return f"会话 {short}… 归属未确认：{message}（未注入）"
            return f"会话 {short}… 不在活表（resolve 拒绝），未注入"

        cmid = client_message_id(dispatch_id)
        try:
            accepted = await self._rpc(
                "sessions.send",
                {
                    "key": session_id,
                    "message": text,
                    "attachments": [],
                    "client_message_id": cmid,
                    "_source": {
                        "caller_kind": "cli",
                        "channel_kind": "cli",
                        "channel_id": f"cli:{_SOURCE_NAME}",
                        "source_kind": "cli",
                        "source_name": _SOURCE_NAME,
                        "client_message_id": cmid,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if _is_uncertain(message):
                return f"送达未知：{message}（网关可能已受理，先去会话里确认再重派）"
            return f"sessions.send 被拒：{message}"

        if not isinstance(accepted, dict) or not (
            accepted.get("turn_id") or accepted.get("task_id")
        ):
            return "送达未知：网关受理应答缺 turn_id，未确认（可能已受理）"
        return ""


__all__ = ["DispatchProcessor", "client_message_id"]
