"""Fail-closed privacy checks for telemetry payloads.

Telemetry models intentionally expose only closed, typed fields.  This module
adds a second line of defence at model ingress and canonical serialization so
future nested models cannot accidentally carry user content under a familiar
sensitive field name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_TELEMETRY_NESTING_DEPTH = 16


class ForbiddenTelemetryFieldError(ValueError):
    """Raised when a telemetry payload contains a forbidden field name."""


_KEY_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# Compare normalized whole keys, not substrings.  In particular,
# ``analytics_user_id`` is an allowed purpose-specific pseudonymous identifier
# and must not be confused with the forbidden raw ``user_id`` field.
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "accountid",
        "acquisitiontoken",
        "agentconfig",
        "apikey",
        "argument",
        "arguments",
        "args",
        "channelid",
        "channelmaterial",
        "cookie",
        "content",
        "credential",
        "credentialid",
        "devicefingerprint",
        "errormessage",
        "exceptionmessage",
        "filecontent",
        "filename",
        "filepath",
        "fullstack",
        "fullstacktrace",
        "ip",
        "ipaddress",
        "mac",
        "macaddress",
        "message",
        "orderid",
        "orderinfo",
        "parameter",
        "parameters",
        "params",
        "path",
        "prompt",
        "prompttext",
        "payment",
        "paymentinfo",
        "rawerrormessage",
        "rawexceptionmessage",
        "rawprompt",
        "rawreply",
        "reply",
        "revocationtoken",
        "requestbody",
        "response",
        "secret",
        "stack",
        "stacktrace",
        "taskparameters",
        "taskargs",
        "taskinput",
        "taskparams",
        "token",
        "toolarguments",
        "toolargs",
        "toolinput",
        "toolparameters",
        "toolparams",
        "traceback",
        "userprompt",
        "userid",
        "utmcampaign",
        "utmcontent",
        "utmmedium",
        "utmsource",
        "utmterm",
    }
)


def normalize_field_name(value: str) -> str:
    """Normalize snake/camel/kebab variants for privacy-key comparison."""

    return _KEY_TOKEN_RE.sub("", value.casefold())


def is_forbidden_field_name(value: str) -> bool:
    """Return whether *value* is a field name telemetry must never accept."""

    return normalize_field_name(value) in _FORBIDDEN_KEY_TOKENS


def assert_no_forbidden_fields(value: Any) -> None:
    """Recursively reject forbidden mapping keys without inspecting values.

    The exception reports only the structural path.  It deliberately never
    includes a submitted value, which may itself contain private data.
    """

    def _scan(child: Any, *, path: tuple[str, ...], depth: int) -> None:
        if depth > MAX_TELEMETRY_NESTING_DEPTH:
            raise ForbiddenTelemetryFieldError("telemetry structure exceeds depth limit")

        if isinstance(child, Mapping):
            for raw_key, nested in child.items():
                if not isinstance(raw_key, str):
                    raise ForbiddenTelemetryFieldError(
                        "telemetry mapping field names must be strings"
                    )
                key = raw_key
                nested_path = (*path, key)
                if is_forbidden_field_name(key):
                    raise ForbiddenTelemetryFieldError(
                        f"forbidden telemetry field at {'.'.join(nested_path)}"
                    )
                _scan(nested, path=nested_path, depth=depth + 1)
            return

        if isinstance(child, Sequence) and not isinstance(
            child, (str, bytes, bytearray, memoryview)
        ):
            for index, nested in enumerate(child):
                _scan(nested, path=(*path, str(index)), depth=depth + 1)

    _scan(value, path=(), depth=0)


__all__ = [
    "ForbiddenTelemetryFieldError",
    "MAX_TELEMETRY_NESTING_DEPTH",
    "assert_no_forbidden_fields",
    "is_forbidden_field_name",
    "normalize_field_name",
]
