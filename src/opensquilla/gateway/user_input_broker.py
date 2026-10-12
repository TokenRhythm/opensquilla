"""Runtime-owned pending interactions for structured tool input."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.session.keys import canonicalize_session_key

_MAX_COMPLETED_REQUESTS = 256
_MAX_STRING_CHARS = 2_000


class UserInputRequestError(ValueError):
    """Base class for safe pending-interaction errors."""


class UserInputRequestNotFoundError(UserInputRequestError):
    """The requested interaction is not pending in this runtime."""


class UserInputRequestConflictError(UserInputRequestError):
    """The request exists but does not belong to the supplied owner."""


class UserInputValidationError(UserInputRequestError):
    """Submitted fields do not satisfy the registered schema."""


@dataclass
class _PendingUserInput:
    request_id: str
    session_key: str
    task_id: str
    tool_use_id: str
    payload: dict[str, Any]
    created_at: int
    future: asyncio.Future[dict[str, Any]]


@dataclass(frozen=True)
class _CompletedUserInput:
    session_key: str
    payload: dict[str, Any]
    answers: dict[str, Any]


def _schema_fields(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    schema = payload.get("clarify_schema")
    if not isinstance(schema, Mapping):
        raise UserInputValidationError("pending user input has no clarify schema")
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise UserInputValidationError("pending user input has no fields")
    if not all(isinstance(field, Mapping) for field in fields):
        raise UserInputValidationError("pending user input fields are invalid")
    return list(fields)


def _required_text(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise UserInputValidationError(f"field {field_name!r} is required")
    if len(text) > _MAX_STRING_CHARS:
        raise UserInputValidationError(
            f"field {field_name!r} must be at most {_MAX_STRING_CHARS} characters"
        )
    return text


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1", "on", "是"}:
        return True
    if text in {"false", "no", "0", "off", "否"}:
        return False
    raise UserInputValidationError(f"field {field_name!r} must be a boolean")


def _coerce_field(field: Mapping[str, Any], value: Any) -> Any:
    name = str(field.get("name") or "").strip()
    field_type = str(field.get("type") or "string").strip().lower()
    # ``choice`` was emitted briefly by an older Plan implementation. Accept
    # it only at this boundary and canonicalize it to enum semantics.
    if field_type == "choice":
        field_type = "enum"
    if field_type == "bool":
        return _coerce_bool(value, field_name=name)
    if field_type == "int":
        if isinstance(value, bool):
            raise UserInputValidationError(f"field {name!r} must be an integer")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise UserInputValidationError(
                f"field {name!r} must be an integer"
            ) from exc
    text = _required_text(value, field_name=name)
    if field_type == "enum":
        choices = [str(choice) for choice in field.get("choices", [])]
        allow_other = field.get("allow_other") is True
        if text not in choices and not allow_other:
            raise UserInputValidationError(
                f"field {name!r} must be one of the offered choices"
            )
    elif field_type != "string":
        raise UserInputValidationError(
            f"field {name!r} has unsupported type {field_type!r}"
        )
    return text


def validate_user_input_fields(
    payload: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate untrusted surface fields against the registered request."""

    schema_fields = _schema_fields(payload)
    fields_by_name = {
        str(field.get("name") or "").strip(): field for field in schema_fields
    }
    unknown = sorted(set(map(str, fields)) - set(fields_by_name))
    if unknown:
        raise UserInputValidationError(f"unknown user-input field: {unknown[0]}")
    normalized: dict[str, Any] = {}
    for name, field in fields_by_name.items():
        if not name:
            raise UserInputValidationError("pending user input has an unnamed field")
        raw_value = fields.get(name)
        present = name in fields and raw_value is not None and raw_value != ""
        if not present:
            if field.get("required") is True:
                raise UserInputValidationError(f"field {name!r} is required")
            continue
        normalized[name] = _coerce_field(field, raw_value)
    return normalized


class StructuredUserInputBroker:
    """Own live pending interactions independently from ordinary steering."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingUserInput] = {}
        self._completed: OrderedDict[str, _CompletedUserInput] = OrderedDict()

    def open_request(
        self,
        *,
        session_key: str,
        task_id: str,
        tool_use_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session_key = canonicalize_session_key(session_key)
        if not task_id or not tool_use_id:
            raise UserInputRequestError(
                "pending user input requires task and tool identities"
            )
        _schema_fields(payload)
        if any(
            item.task_id == task_id and item.tool_use_id == tool_use_id
            for item in self._pending.values()
        ):
            raise UserInputRequestConflictError(
                "this tool call already has a pending user-input request"
            )
        request_id = str(uuid.uuid4())
        public_payload = copy.deepcopy(payload)
        public_payload.update(
            {
                "status": "input_required",
                "kind": "user_input",
                "paused": True,
                "request_id": request_id,
                "run_id": task_id,
            }
        )
        pending = _PendingUserInput(
            request_id=request_id,
            session_key=session_key,
            task_id=task_id,
            tool_use_id=tool_use_id,
            payload=public_payload,
            created_at=int(time.time() * 1000),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request_id] = pending
        return copy.deepcopy(public_payload)

    async def wait_for_response(self, request_id: str) -> dict[str, Any]:
        pending = self._pending.get(request_id)
        if pending is None:
            raise UserInputRequestNotFoundError(
                "pending user-input request no longer exists"
            )
        try:
            return await pending.future
        finally:
            self._pending.pop(request_id, None)

    def resolve(
        self,
        *,
        session_key: str,
        request_id: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        session_key = canonicalize_session_key(session_key)
        completed = self._completed.get(request_id)
        if completed is not None:
            if completed.session_key != session_key:
                raise UserInputRequestConflictError(
                    "pending user-input request belongs to another session"
                )
            normalized = validate_user_input_fields(completed.payload, fields)
            if normalized != completed.answers:
                raise UserInputRequestConflictError(
                    "pending user-input request was already answered differently"
                )
            return {
                "resolved": True,
                "replayed": True,
                "request_id": request_id,
            }
        pending = self._pending.get(request_id)
        if pending is None:
            raise UserInputRequestNotFoundError(
                "pending user-input request was not found"
            )
        if pending.session_key != session_key:
            raise UserInputRequestConflictError(
                "pending user-input request belongs to another session"
            )
        answers = validate_user_input_fields(pending.payload, fields)
        if pending.future.done():
            existing = pending.future.result()
            if existing != answers:
                raise UserInputRequestConflictError(
                    "pending user-input request was already answered differently"
                )
            return {
                "resolved": True,
                "replayed": True,
                "request_id": request_id,
            }
        pending.future.set_result(answers)
        self._completed[request_id] = _CompletedUserInput(
            session_key=session_key,
            payload=copy.deepcopy(pending.payload),
            answers=answers,
        )
        self._completed.move_to_end(request_id)
        while len(self._completed) > _MAX_COMPLETED_REQUESTS:
            self._completed.popitem(last=False)
        return {
            "resolved": True,
            "replayed": False,
            "request_id": request_id,
        }

    def pending_for_session(self, session_key: str) -> list[dict[str, Any]]:
        session_key = canonicalize_session_key(session_key)
        pending = sorted(
            (
                item
                for item in self._pending.values()
                if item.session_key == session_key and not item.future.done()
            ),
            key=lambda item: (item.created_at, item.request_id),
        )
        return [copy.deepcopy(item.payload) for item in pending]

    def cancel_request(self, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

    def cancel_task(self, task_id: str) -> None:
        request_ids = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.task_id == task_id
        ]
        for request_id in request_ids:
            self.cancel_request(request_id)
