from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opensquilla.telemetry.contracts import (
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    telemetry_protocol_manifest,
)


def test_checked_in_protocol_manifest_is_the_python_contract_golden() -> None:
    artifact = (
        Path(__file__).parents[2]
        / "src"
        / "opensquilla"
        / "telemetry"
        / "contracts"
        / "protocol-manifest.v1.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert payload == telemetry_protocol_manifest()
    assert hashlib.sha256(canonical).hexdigest() == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
