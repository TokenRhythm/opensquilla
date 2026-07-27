from __future__ import annotations

from types import SimpleNamespace

from opensquilla.gateway.boot import build_cron_result_payload


def test_cron_result_payload_carries_persisted_message_id_and_provenance() -> None:
    entry = SimpleNamespace(
        message_id="cron-message-123",
        created_at=1_721_234_567.0,
        provenance_kind="cron",
        provenance_source_tool="cron:daily-summary",
        provenance_source_session_key="cron:daily-summary:run:abc",
    )

    payload = build_cron_result_payload(
        "agent:main:webchat:demo",
        "Scheduled summary",
        entry,
    )

    assert payload == {
        "sessionKey": "agent:main:webchat:demo",
        "message": {
            "role": "assistant",
            "text": "Scheduled summary",
            "timestamp": 1_721_234_567.0,
            "messageId": "cron-message-123",
            "provenanceKind": "cron",
            "provenanceSourceTool": "cron:daily-summary",
            "provenanceSourceSessionKey": "cron:daily-summary:run:abc",
        },
    }
