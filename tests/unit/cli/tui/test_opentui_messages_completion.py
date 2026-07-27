from __future__ import annotations

import json

import pytest

from opensquilla.cli.tui.opentui.messages import (
    CompletionCandidate,
    HostCompletionRequest,
    HostToPythonMessageError,
    host_message_from_json,
    python_message_to_json,
)


def test_host_completion_request_parses_required_fields() -> None:
    assert host_message_from_json(
        '{"type":"completion.request","kind":"file","query":"src/cli","request_id":42}'
    ) == HostCompletionRequest(kind="file", query="src/cli", request_id=42)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"type":"completion.request","query":"x","request_id":1}', "completion.kind"),
        ('{"type":"completion.request","kind":"slash","request_id":1}', "completion.query"),
        ('{"type":"completion.request","kind":"slash","query":"x"}', "completion.request_id"),
    ],
)
def test_host_completion_request_rejects_missing_fields(raw: str, message: str) -> None:
    with pytest.raises(HostToPythonMessageError, match=message):
        host_message_from_json(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"type":"completion.request","kind":7,"query":"x","request_id":1}', "completion.kind"),
        (
            '{"type":"completion.request","kind":"slash","query":7,"request_id":1}',
            "completion.query",
        ),
        (
            '{"type":"completion.request","kind":"slash","query":"x","request_id":"1"}',
            "completion.request_id",
        ),
    ],
)
def test_host_completion_request_rejects_wrong_field_types(raw: str, message: str) -> None:
    with pytest.raises(HostToPythonMessageError, match=message):
        host_message_from_json(raw)


def test_completion_candidate_serializes_as_json_payload() -> None:
    raw = python_message_to_json(
        "completion.item",
        CompletionCandidate(
            label="/compact",
            description="Compact older context.",
            insert_text="/compact ",
            category="command",
        ),
    )

    assert json.loads(raw) == {
        "type": "completion.item",
        "label": "/compact",
        "description": "Compact older context.",
        "insert_text": "/compact ",
        "category": "command",
        "usage": "",
        "aliases": [],
        "argument_choices": [],
        "visible_by_default": True,
        "deprecated": False,
        "submit_behavior": "submit",
        "busy_policy": "immediate",
        "presentation": "notice",
    }
