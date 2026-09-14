"""Regression tests for `opensquilla sessions list` filtering.

`_filter_sessions` is pure — rows in, rows out — so the channel filter can be
covered without a gateway, a network call, or any credential.
"""

from __future__ import annotations

from opensquilla.cli.sessions_cmd import _filter_sessions


def _rows() -> list[dict]:
    """Rows shaped as `sessions list --json` actually projects them.

    `channel` is null; the canonical fields are `source_kind`, `channel_kind`
    and `surface` (see `opensquilla.chat.source.chat_source_metadata`).
    """
    return [
        {
            "key": "agent:main:cron:daily",
            "agent_id": "main",
            "status": "idle",
            "source_kind": "cron",
            "channel_kind": "cron",
            "surface": "cron",
            "channel": None,
        },
        {
            "key": "agent:main:webchat:abc",
            "agent_id": "main",
            "status": "idle",
            "source_kind": "webui",
            "channel_kind": "webchat",
            "surface": "webchat",
            "channel": None,
        },
    ]


def _filter(channel: str | None) -> list[dict]:
    return _filter_sessions(_rows(), agent=None, status=None, channel=channel, since=None)


def test_channel_filter_matches_channel_kind():
    """`--channel cron` must find the row whose channel_kind is cron."""
    assert [row["key"] for row in _filter("cron")] == ["agent:main:cron:daily"]


def test_channel_filter_matches_channel_kind_for_webchat():
    """`--channel webchat` matches channel_kind/surface, which is what the row
    advertises — the `channel` key itself is null on these rows."""
    assert [row["key"] for row in _filter("webchat")] == ["agent:main:webchat:abc"]


def test_channel_filter_matches_source_kind():
    """`--channel webui` matches source_kind: the issue asks for the filter to
    accept the source projection too, and `webui` appears only there."""
    assert [row["key"] for row in _filter("webui")] == ["agent:main:webchat:abc"]


def test_channel_filter_still_rejects_a_channel_no_row_has():
    """Control: the filter must stay a filter — an unknown value matches nothing."""
    assert _filter("telegram") == []


def test_channel_filter_absent_returns_every_row():
    """Control: no --channel means no channel filtering."""
    assert len(_filter(None)) == 2


def test_channel_filter_still_matches_the_legacy_channel_field():
    """Control: rows that do carry an explicit `channel` keep working."""
    rows = [{"key": "k", "channel": "slack"}]
    assert _filter_sessions(rows, agent=None, status=None, channel="slack", since=None) == rows


def test_channel_filter_does_not_match_rows_with_only_null_channel_fields():
    """Control against the obvious wrong fix: collapsing null to "" must not make
    an empty-ish filter value match every row."""
    rows = [{"key": "k", "channel": None, "channel_kind": None, "surface": None}]
    assert _filter_sessions(rows, agent=None, status=None, channel="cron", since=None) == []
