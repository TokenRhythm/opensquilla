"""Characterization tests for the sessions.preview application boundary."""

from __future__ import annotations

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
from opensquilla.session.storage import _BOUNDED_INTERACTIVE_READS


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


def context(storage: BoundedStorage) -> RpcContext:
    ctx = RpcContext(
        conn_id="preview-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(memory={"flush_enabled": False}),
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
        config=GatewayConfig(memory={"flush_enabled": False}),
    )

    with pytest.raises(AttributeError):
        await rpc_sessions._handle_sessions_preview(params, ctx)
