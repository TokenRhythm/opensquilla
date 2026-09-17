"""Characterization tests for the sessions.preview application boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.adapters.session_preview import (
    preview_params_from_v4,
    preview_query_from_v4,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import _BOUNDED_INTERACTIVE_READS, SessionStorage


class BoundedStorage:
    def __init__(self) -> None:
        self.session = SimpleNamespace(
            session_key="agent:main:webchat:preview",
            session_id="preview-id",
            display_name=None,
            derived_title="Preview",
            updated_at=2000,
        )
        self.calls: list[tuple[str, Any]] = []

    async def get_session(self, key: str) -> Any:
        self.calls.append(("get", (_BOUNDED_INTERACTIVE_READS.get(), key)))
        return self.session if key == self.session.session_key else None

    async def list_sessions(self, *, limit: int) -> list[Any]:
        self.calls.append(("list", (_BOUNDED_INTERACTIVE_READS.get(), limit)))
        return [self.session]

    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> dict[str, str]:
        self.calls.append(
            ("preview", (_BOUNDED_INTERACTIVE_READS.get(), list(session_ids), max_chars))
        )
        return {"preview-id": "latest"}

    async def list_canonical_user_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        limit_per_session: int,
    ) -> dict[str, list[str]]:
        self.calls.append(
            ("titles", (_BOUNDED_INTERACTIVE_READS.get(), list(session_ids), limit_per_session))
        )
        return {"preview-id": ["整理示例图片"]}


def context(storage: BoundedStorage | SessionStorage) -> RpcContext:
    ctx = RpcContext(
        conn_id="preview-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(memory={}),
    )
    ctx.session_manager = SimpleNamespace(storage=storage)
    return ctx


@pytest.mark.asyncio
async def test_preview_adapter_keeps_wire_projection_and_bounded_scope() -> None:
    storage = BoundedStorage()

    payload = await rpc_sessions._handle_sessions_preview(None, context(storage))

    assert payload["previews"] == [
        {
            "key": "agent:main:webchat:preview",
            "title": "Preview",
            "lastMessage": "latest",
            "updatedAt": 2000,
        }
    ]
    assert [name for name, _ in storage.calls] == ["list", "preview"]
    assert all(details[0] is True for _, details in storage.calls)


@pytest.mark.asyncio
async def test_preview_adapter_preserves_key_selection_order() -> None:
    storage = BoundedStorage()

    payload = await rpc_sessions._handle_sessions_preview(
        {"keys": ["missing", storage.session.session_key]},
        context(storage),
    )

    assert [item["key"] for item in payload["previews"]] == [storage.session.session_key]
    assert storage.calls == [
        ("get", (True, "missing")),
        ("get", (True, storage.session.session_key)),
        ("preview", (True, ["preview-id"], 120)),
    ]


@pytest.mark.asyncio
async def test_preview_recovers_custom_named_channel_title_using_configured_type() -> None:
    storage = BoundedStorage()
    storage.session.session_key = "agent:main:sample-channel:direct:sample-user"
    storage.session.last_channel = "sample-channel"
    storage.session.derived_title = "I cannot assist with that request"
    ctx = context(storage)
    ctx.config = GatewayConfig(
        channels={
            "channels": [
                {
                    "type": "feishu",
                    "name": "sample-channel",
                    "app_id": "cli_dummy",
                    "app_secret": "dummy",
                }
            ]
        },
    )

    payload = await rpc_sessions._handle_sessions_preview(None, ctx)

    assert payload["previews"][0]["title"] == "整理示例图片"
    assert storage.calls == [
        ("list", (True, 50)),
        ("preview", (True, ["preview-id"], 120)),
        ("titles", (True, ["preview-id"], 3)),
    ]


@pytest.mark.parametrize("params", ["x", 1, True])
def test_preview_keeps_legacy_non_mapping_params_error_order(params: Any) -> None:
    """The old handler raised before checking manager/storage availability."""

    with pytest.raises(AttributeError):
        preview_params_from_v4(params)


@pytest.mark.parametrize("limit", [0, -1, None, "bad", True, 1.5])
def test_preview_query_keeps_raw_legacy_limit(limit: Any) -> None:
    query = preview_query_from_v4({"limit": limit})

    assert query.limit is limit or query.limit == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("params", ["x", 1, True])
async def test_preview_non_mapping_params_fail_before_unavailable_manager(
    params: Any,
) -> None:
    """Unavailable backends must not mask the legacy params-shape error."""

    ctx = RpcContext(
        conn_id="preview-no-manager",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(memory={}),
    )

    with pytest.raises(AttributeError):
        await rpc_sessions._handle_sessions_preview(params, ctx)


@pytest.mark.asyncio
async def test_preview_recovers_only_refused_chat_titles_in_one_batch_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = str(tmp_path / "preview-titles.db")
    refusal = "I'm unable to provide assistance with this request"
    rows = [
        SessionNode(
            session_key="agent:main:webchat:refused-full",
            session_id="refused-full-id",
            display_name="WebChat",
            derived_title=refusal,
        ),
        SessionNode(
            session_key="agent:main:webchat:refused-truncated",
            session_id="refused-truncated-id",
            derived_title="I'm unable to provide assistance with this reque",
        ),
        SessionNode(
            session_key="agent:main:webchat:no-visible-text",
            session_id="no-visible-text-id",
            derived_title=refusal,
        ),
        SessionNode(
            session_key="agent:main:webchat:manual",
            session_id="manual-id",
            display_name=refusal,
            derived_title=refusal,
        ),
        SessionNode(
            session_key="agent:main:webchat:valid-title",
            session_id="valid-title-id",
            derived_title="I cannot log in",
        ),
        SessionNode(
            session_key="agent:main:subagent:preview-task",
            session_id="task-id",
            derived_title=refusal,
        ),
        SessionNode(
            session_key="cron:preview-job:run:sample",
            session_id="cron-id",
            derived_title=refusal,
        ),
    ]
    storage = SessionStorage(db_path)
    await storage.connect()
    try:
        for row in rows:
            await storage.upsert_session(row)
        for content in (
            "[Tool result (sample-call): synthetic tool output]",
            json.dumps(
                {
                    "text": "[2026-01-05T12:00+00:00 Mon UTC]\n"
                    "Describe sample files and their folder layout"
                }
            ),
        ):
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_key=rows[0].session_key,
                    session_id=rows[0].session_id,
                    role="user",
                    content=content,
                )
            )
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_key=rows[1].session_key,
                session_id=rows[1].session_id,
                role="user",
                content="整理示例图片",
            )
        )
    finally:
        await storage.close()

    reopened = SessionStorage(db_path)
    await reopened.connect()
    try:
        before = [(await reopened.get_session(row.session_key)).model_dump() for row in rows]
        batch_calls: list[tuple[bool, list[str], int]] = []
        original_batch = reopened.list_canonical_user_transcript_content_batch

        async def capture_batch(
            session_ids: list[str],
            *,
            limit_per_session: int,
        ) -> dict[str, list[str]]:
            batch_calls.append(
                (_BOUNDED_INTERACTIVE_READS.get(), list(session_ids), limit_per_session)
            )
            return await original_batch(session_ids, limit_per_session=limit_per_session)

        monkeypatch.setattr(reopened, "list_canonical_user_transcript_content_batch", capture_batch)
        payload = await rpc_sessions._handle_sessions_preview(
            {"keys": [row.session_key for row in rows]},
            context(reopened),
        )

        assert [item["title"] for item in payload["previews"]] == [
            "Describe sample files and their...",
            "整理示例图片",
            "no-visib",
            refusal,
            "I cannot log in",
            refusal,
            refusal,
        ]
        assert batch_calls == [(True, [row.session_id for row in rows[:3]], 3)]
        after = [(await reopened.get_session(row.session_key)).model_dump() for row in rows]
        assert after == before
    finally:
        await reopened.close()
