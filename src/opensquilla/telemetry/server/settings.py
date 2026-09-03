"""Fail-closed settings for one scope-specific telemetry collector."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from opensquilla.telemetry.contracts.common import ConsentScope, EventSource
from opensquilla.telemetry.contracts.manifest import (
    MAX_GROWTH_BATCH_BYTES,
    MAX_RELIABILITY_BATCH_BYTES,
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
)
from opensquilla.telemetry.contracts.wire import TelemetryWireTarget
from opensquilla.telemetry.server.producer_auth import validate_producer_secrets

_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True, slots=True)
class CollectorSettings:
    """Configuration for exactly one isolated collector process and database."""

    scope: ConsentScope
    database_path: Path
    protocol_fingerprint: str = TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
    producer_secrets: Mapping[EventSource, bytes] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ConsentScope):
            raise TypeError("collector scope must be a ConsentScope")
        if not isinstance(self.database_path, Path):
            raise TypeError("collector database_path must be a Path")
        if not self.database_path.name:
            raise ValueError("collector database_path must name a file")
        if not _SHA256_RE.fullmatch(self.protocol_fingerprint):
            raise ValueError("collector protocol_fingerprint must be lowercase SHA-256")
        normalized_secrets = validate_producer_secrets(self.producer_secrets)
        if self.scope is ConsentScope.RELIABILITY and normalized_secrets:
            raise ValueError("reliability collector cannot configure growth producers")
        object.__setattr__(
            self,
            "producer_secrets",
            MappingProxyType(normalized_secrets),
        )

    @property
    def endpoint_path(self) -> str:
        if self.scope is ConsentScope.RELIABILITY:
            return "/v1/reliability/events"
        return "/v1/growth/events"

    @property
    def max_body_bytes(self) -> int:
        if self.scope is ConsentScope.RELIABILITY:
            return MAX_RELIABILITY_BATCH_BYTES
        return MAX_GROWTH_BATCH_BYTES

    @property
    def wire_target(self) -> TelemetryWireTarget:
        if self.scope is ConsentScope.RELIABILITY:
            return TelemetryWireTarget.RELIABILITY_BATCH
        return TelemetryWireTarget.GROWTH_BATCH


__all__ = ["CollectorSettings"]
