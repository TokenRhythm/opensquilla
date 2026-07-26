from __future__ import annotations

from opensquilla.gateway.app import _human_actionable_approvals


def test_human_approval_http_filter_hides_automatic_records() -> None:
    records = [
        {
            "id": "auto",
            "params": {"humanActionable": False, "reviewer": "auto_review"},
        },
        {
            "id": "human",
            "params": {"humanActionable": True, "reviewer": "user"},
        },
        {"id": "legacy", "params": {"toolName": "exec_command"}},
    ]

    assert [item["id"] for item in _human_actionable_approvals(records)] == [
        "human",
        "legacy",
    ]
