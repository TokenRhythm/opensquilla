"""Reply to a Gateway questionnaire without admitting another chat turn."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from opensquilla.cli.chat.commands import is_exit_command
from opensquilla.cli.gateway_client import GatewayRPCError
from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface


class GatewayUserInput:
    """Collect one questionnaire through the existing terminal composer.

    The request identity comes from the server, including after reconnect. A
    failed submission is never promoted to ordinary chat or steering input.
    """

    def __init__(
        self,
        *,
        submit: Callable[..., Awaitable[dict[str, Any]]],
        write: Callable[[str], Awaitable[None]],
    ) -> None:
        self._submit = submit
        self._write = write
        self._session_key = ""
        self._requests: dict[str, dict[str, Any]] = {}
        self._answers: dict[str, dict[str, str]] = {}
        self._closed: dict[str, None] = {}
        self._presented: tuple[str, str] | None = None

    @property
    def pending(self) -> bool:
        return bool(self._requests)

    def reset(self, session_key: str, snapshot: dict[str, Any]) -> None:
        same_session = session_key == self._session_key
        previous_answers = self._answers if same_session else {}
        previous_request_ids = set(self._requests) if same_session else set()
        self._session_key = session_key
        self._requests.clear()
        self._answers = {}
        if not same_session:
            self._closed.clear()
        self._presented = None
        session = snapshot.get("session")
        if isinstance(session, dict):
            pending = session.get("pendingUserInputs", session.get("pending_user_inputs", []))
            if isinstance(pending, list):
                for request in pending:
                    self._accept(request)
        for request_id in self._requests:
            if request_id in previous_answers:
                self._answers[request_id] = previous_answers[request_id]
        # A same-session snapshot is authoritative: an accepted reply may
        # already be absent even if its RPC receipt or answered event was lost.
        for request_id in previous_request_ids - self._requests.keys():
            self._close(request_id)

    def _accept(self, request: Any) -> None:
        if not isinstance(request, dict) or request.get("kind") != "user_input":
            return
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in self._closed:
            return
        if request.get("status") != "input_required":
            self._close(request_id)
            return
        schema = request.get("clarify_schema")
        fields = schema.get("fields") if isinstance(schema, dict) else None
        if not isinstance(fields, list) or not fields:
            return
        if not all(isinstance(field, dict) and field.get("name") for field in fields):
            return
        self._requests[request_id] = request
        self._answers.setdefault(request_id, {})

    def _close(self, request_id: str) -> None:
        self._requests.pop(request_id, None)
        self._answers.pop(request_id, None)
        self._closed[request_id] = None
        if len(self._closed) > 128:
            self._closed.pop(next(iter(self._closed)))

    async def observe(self, event: dict[str, Any], *, session_key: str | None = None) -> None:
        if session_key is not None and session_key != self._session_key:
            return
        if event.get("event") == "session.event.tool_result":
            result = event.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (ValueError, TypeError):
                    return
            self._accept(result)
        elif event.get("event") in {
            "session.event.done", "session.event.error", "task.aborted", "task.failed",
            "task.timeout", "task.abandoned",
        }:
            task_id = event.get("turn_id") or event.get("run_id") or event.get("task_id")
            for request_id, request in tuple(self._requests.items()):
                if task_id and request.get("run_id") == task_id:
                    self._close(request_id)
        await self.present()

    async def present(self) -> None:
        if not self._requests:
            self._presented = None
            return
        request_id, request = next(iter(self._requests.items()))
        fields = request["clarify_schema"]["fields"]
        answers = self._answers[request_id]
        if len(answers) == len(fields):
            identity = (request_id, "__retry__")
            if self._presented != identity:
                self._presented = identity
                await self._write("Answer delivery is unconfirmed. Submit again to retry it.")
            return
        field = next((field for field in fields if field["name"] not in answers), fields[-1])
        identity = (request_id, field["name"])
        if identity == self._presented:
            return
        self._presented = identity
        lines = [f"Waiting for your answer ({fields.index(field) + 1}/{len(fields)}):"]
        lines.append(str(field.get("prompt") or field["name"]))
        for index, choice in enumerate(field.get("choices") or [], 1):
            lines.append(f"  {index}. {choice}")
        lines.append("Reply in the composer with your answer or an option number.")
        await self._write("\n".join(lines))

    async def answer(self, text: str) -> bool:
        if not self._requests:
            return False
        if is_exit_command(text):
            return False
        head = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if head.startswith("/") and DEFAULT_REGISTRY.find(head.lower(), Surface.CLI_GATEWAY):
            return False
        request_id, request = next(iter(self._requests.items()))
        session_key = self._session_key
        fields = request["clarify_schema"]["fields"]
        answers = self._answers[request_id]
        field = next((field for field in fields if field["name"] not in answers), fields[-1])
        value = text.strip()
        choices = field.get("choices") or []
        if value.isdecimal() and 1 <= int(value) <= len(choices):
            value = str(choices[int(value) - 1])
        if len(answers) < len(fields):
            answers[field["name"]] = value
        if len(answers) < len(fields):
            await self.present()
            return True
        try:
            await self._submit(session_key, request_id=request_id, fields=dict(answers))
        except GatewayRPCError as exc:
            if exc.code == "USER_INPUT_EXPIRED":
                self._close(request_id)
                await self._write("This question is no longer waiting for an answer.")
            else:
                if exc.accepted is False or exc.code == "INVALID_REQUEST":
                    # Validation may reject an earlier field, and the Gateway
                    # maps validation errors to INVALID_REQUEST without an
                    # accepted flag. Let every answer be corrected.
                    answers.clear()
                    self._presented = None
                    await self._write(f"Answer was rejected: {exc}. Please answer again.")
                else:
                    # Keep the exact answer only when delivery is uncertain.
                    await self._write(f"Answer was not confirmed: {exc}. Submit again to retry.")
                await self.present()
            return True
        except (ConnectionError, TimeoutError) as exc:
            await self._write(f"Answer was not confirmed: {exc}. Submit again to retry.")
            return True
        self._close(request_id)
        await self._write("Answer submitted. Continuing the current task.")
        await self.present()
        return True
