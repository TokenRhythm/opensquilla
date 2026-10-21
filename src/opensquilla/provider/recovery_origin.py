"""Content-free transport evidence for replaying an interrupted response."""

from __future__ import annotations

from typing import Literal

import httpx

type RecoveryOrigin = Literal["read_timeout", "transport_disconnect", "unknown"]


def transport_recovery_origin(error: BaseException) -> RecoveryOrigin:
    """Classify concrete transport failures without inspecting upstream prose.

    Generic timeouts, connection establishment, request writes, local protocol
    errors, and unknown SDK wrappers do not establish an interrupted read.
    Exception causes are deliberately not traversed: a local failure may carry
    an earlier transport exception without being safe to replay itself.
    """

    if isinstance(error, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(
        error,
        (
            httpx.ReadError,
            httpx.RemoteProtocolError,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    ):
        return "transport_disconnect"
    return "unknown"
