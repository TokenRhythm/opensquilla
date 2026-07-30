"""Admin cron tool: strict structured-schedule contract.

Covers structured success path plus the four key field-named ``ToolError``
messages (flat string rejection, invalid cron expr, naive ISO ``at``, and
``every_seconds`` lower-bound).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import opensquilla.tools.builtin.admin as admin_mod
from opensquilla.scheduler.types import SessionTarget
from opensquilla.tools.builtin.admin import cron as cron_tool
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context


class _ToolFakeScheduler:
    def __init__(self) -> None:
        self.added_kwargs: dict[str, Any] | None = None
        self.added_calls: list[dict[str, Any]] = []

    async def add_job(self, **kwargs):
        self.added_kwargs = kwargs
        self.added_calls.append(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(
            id="job-strict",
            delivery=SimpleNamespace(ws_topic=""),
        )

    async def update_job(self, *_, **__):
        return None


@pytest.mark.asyncio
async def test_admin_cron_accepts_structured_cron_schedule() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    try:
        raw = await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "*/5 * * * *"},
            task="ping",
            job_kind="agent_turn",
            session_target="isolated",
        )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["schedule_value"] == "*/5 * * * *"
    assert fake.added_kwargs["creator_is_owner"] is True
    assert fake.added_kwargs["idempotency_key"] == ""
    assert json.loads(raw)["schedule_value"] == "*/5 * * * *"


@pytest.mark.asyncio
async def test_admin_cron_generates_stable_per_task_idempotency_key() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            task_id="task-123",
            session_key="agent:main:webchat:owner",
            sender_id="owner",
            run_mode="full",
        )
    )
    try:
        for _ in range(2):
            await cron_tool(
                action="add",
                schedule={"kind": "cron", "expr": "*/5 * * * *"},
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        current_tool_context.reset(token)
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    first_key = fake.added_calls[0]["idempotency_key"]
    assert first_key.startswith("cron-tool:")
    assert len(first_key) == len("cron-tool:") + 64
    assert fake.added_calls[1]["idempotency_key"] == first_key
    assert fake.added_calls[0]["run_mode"] == "full"


@pytest.mark.asyncio
async def test_admin_cron_normalizes_schedule_for_idempotency() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            task_id="task-123",
            session_key="agent:main:webchat:owner",
            sender_id="owner",
        )
    )
    try:
        for expression in ("0   9 * * *", "0 9 * * *"):
            await cron_tool(
                action="add",
                schedule={"kind": "cron", "expr": expression},
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        current_tool_context.reset(token)
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_calls[0]["idempotency_key"] == fake.added_calls[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_admin_cron_scopes_idempotency_to_task_id() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    try:
        for task_id in ("task-123", "task-456"):
            token = current_tool_context.set(
                ToolContext(
                    is_owner=True,
                    task_id=task_id,
                    session_key="agent:main:webchat:owner",
                    sender_id="owner",
                )
            )
            try:
                await cron_tool(
                    action="add",
                    schedule={"kind": "cron", "expr": "0 9 * * *"},
                    task="ping",
                    job_kind="agent_turn",
                    session_target="isolated",
                )
            finally:
                current_tool_context.reset(token)
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_calls[0]["idempotency_key"] != fake.added_calls[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_admin_cron_records_non_owner_creator_boundary() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    token = current_tool_context.set(
        ToolContext(
            is_owner=False,
            session_key="agent:main:channel:user",
            sender_id="channel-user",
            channel_kind="feishu",
            channel_id="chat-1",
        )
    )
    try:
        await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "*/5 * * * *"},
            task="ping",
            job_kind="agent_turn",
            session_target="isolated",
        )
    finally:
        current_tool_context.reset(token)
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["creator_is_owner"] is False


@pytest.mark.asyncio
async def test_admin_cron_current_target_binds_caller_session() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            session_key="agent:main:webchat:abc123",
            sender_id="owner",
        )
    )
    try:
        raw = await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "*/5 * * * *"},
            task="ping",
            job_kind="agent_turn",
            session_target="current",
        )
    finally:
        current_tool_context.reset(token)
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["session_target"] == SessionTarget.CURRENT
    assert fake.added_kwargs["session_key"] == "agent:main:webchat:abc123"
    assert fake.added_kwargs["origin_session_key"] == "agent:main:webchat:abc123"
    assert json.loads(raw)["session_target"] == "current"


@pytest.mark.asyncio
async def test_admin_cron_non_owner_current_target_stays_caller_scoped() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    token = current_tool_context.set(
        ToolContext(
            is_owner=False,
            session_key="agent:main:channel:user",
            sender_id="channel-user",
            channel_kind="feishu",
            channel_id="chat-1",
        )
    )
    try:
        await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "*/5 * * * *"},
            task="ping",
            job_kind="agent_turn",
            session_target="current",
        )
    finally:
        current_tool_context.reset(token)
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["creator_is_owner"] is False
    assert fake.added_kwargs["session_target"] == SessionTarget.CURRENT
    assert fake.added_kwargs["session_key"] == "agent:main:channel:user"
    assert fake.added_kwargs["origin_session_key"] == "agent:main:channel:user"


@pytest.mark.asyncio
async def test_admin_cron_applies_top_level_tz_to_structured_cron() -> None:
    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    try:
        await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "0 9 * * *"},
            task="ping",
            job_kind="agent_turn",
            session_target="isolated",
            tz="Asia/Shanghai",
        )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["schedule_tz"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_admin_cron_rejects_conflicting_schedule_and_top_level_tz() -> None:
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="schedule.tz conflicts with tz"):
            await cron_tool(
                action="add",
                schedule={
                    "kind": "cron",
                    "expr": "0 9 * * *",
                    "tz": "Asia/Shanghai",
                },
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
                tz="America/Los_Angeles",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_admin_cron_rejects_flat_string_schedule() -> None:
    """A bare 5-field cron string must NOT be accepted by the LLM tool."""
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="schedule must be an object"):
            await cron_tool(
                action="add",
                schedule="每5分钟",  # type: ignore[arg-type]
                task="ping",
                job_kind="system_event",
                session_target="main",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_admin_cron_rejects_invalid_cron_expr() -> None:
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="schedule.expr invalid"):
            await cron_tool(
                action="add",
                schedule={"kind": "cron", "expr": "not-a-cron"},
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_admin_cron_rejects_naive_at_timestamp() -> None:
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="must include a timezone"):
            await cron_tool(
                action="add",
                schedule={"kind": "at", "at": "2026-05-15T09:00:00"},
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_admin_cron_rejects_zero_every_seconds() -> None:
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="every_seconds"):
            await cron_tool(
                action="add",
                schedule={"kind": "every", "every_seconds": 0},
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]


def test_admin_cron_schema_does_not_advertise_every_anchor() -> None:
    registered = get_default_registry().get("cron")

    assert registered is not None
    schedule_props = registered.spec.parameters["schedule"]["properties"]
    assert "anchor_at" not in schedule_props


def test_admin_cron_schema_advertises_current_session_target() -> None:
    registered = get_default_registry().get("cron")

    assert registered is not None
    targets = registered.spec.parameters["session_target"]["enum"]
    assert "current" in targets


@pytest.mark.asyncio
async def test_admin_cron_rejects_every_anchor_until_supported() -> None:
    admin_mod.set_scheduler(_ToolFakeScheduler())
    try:
        with pytest.raises(ToolError, match="schedule.anchor_at is not supported"):
            await cron_tool(
                action="add",
                schedule={
                    "kind": "every",
                    "every_seconds": 300,
                    "anchor_at": "2026-05-18T09:00:00+08:00",
                },
                task="ping",
                job_kind="agent_turn",
                session_target="isolated",
            )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]
