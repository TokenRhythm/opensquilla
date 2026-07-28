from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_memory_import import (
    _GatewayFusionCompletion,
    _handle_memory_import_apply,
    _handle_memory_import_cancel,
    _handle_memory_import_discard,
    _handle_memory_import_info,
    _handle_memory_import_preview,
    _handle_memory_import_retry,
    _handle_memory_import_start,
    _handle_memory_import_status,
    _handle_memory_import_undo,
    _profile_import_output_tokens,
    _profile_import_paths,
    run_profile_import_startup_maintenance,
    run_profile_import_startup_recovery,
)
from opensquilla.gateway.scopes import METHOD_SCOPES
from opensquilla.provider.types import DoneEvent, TextDeltaEvent


class _FakeProfileImportService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.model = SimpleNamespace(
            provider="synthetic",
            model="synthetic-model",
            is_loopback=False,
        )

    async def info(self) -> dict[str, Any]:
        self.calls.append(("info", None))
        return {
            "available": True,
            "provider": "synthetic",
            "model": "synthetic-model",
            "is_local": False,
            "max_input_bytes": 262_144,
            "prompt_version": "profile-fusion-v1",
            "recent_import": None,
        }

    async def preview(self, request: Any) -> dict[str, Any]:
        self.calls.append(("preview", request))
        return {
            "preview_id": "preview-1",
            "batch_id": "batch-1",
            "candidate_hash": "candidate-hash",
            "provider": "synthetic",
            "model": "synthetic-model",
            "summary": ["One change"],
            "decision_counts": {"applied": 1, "duplicate": 0, "unresolved": 0},
            "files": [
                {
                    "target": "USER",
                    "relative_path": "USER.md",
                    "added_lines": 1,
                    "removed_lines": 0,
                    "diff": "+Name: Synthetic",
                }
            ],
        }

    async def apply(
        self,
        preview_id: str,
        candidate_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.calls.append(("apply", (preview_id, candidate_hash, idempotency_key)))
        return {
            "status": "applied",
            "receipt_id": "receipt-1",
            "batch_id": "batch-1",
            "agent_id": "main",
            "index_status": "ready",
            "applied_at": "2026-07-28T00:00:00Z",
        }

    async def undo(self, receipt_id: str, client_request_id: str) -> dict[str, Any]:
        self.calls.append(("undo", (receipt_id, client_request_id)))
        return {
            "status": "undone",
            "receipt_id": receipt_id,
            "agent_id": "main",
        }

    async def discard(self, preview_id: str) -> dict[str, Any]:
        self.calls.append(("discard", preview_id))
        return {"status": "discarded", "preview_id": preview_id}


def _ctx(service: Any, **kwargs: Any) -> RpcContext:
    return RpcContext(conn_id="test", profile_import_service=service, **kwargs)


@pytest.mark.asyncio
async def test_background_job_rpc_contract_uses_durable_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_memory_import as rpc_memory_import

    service = _FakeProfileImportService()
    service.job_status = lambda job_id: {
        "job_id": job_id,
        "batch_id": "b" * 32,
        "status": "analyzing",
        "stage": "model",
        "provider": "synthetic",
        "model": "synthetic-model",
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:01Z",
        "expires_at": "2026-07-29T00:00:00Z",
        "attempt_count": 1,
        "can_retry": False,
    }
    runner_calls: list[tuple[str, Any]] = []

    class _Runner:
        async def start(self, _service: Any, request: Any) -> dict[str, Any]:
            runner_calls.append(("start", request))
            return {
                **service.job_status("a" * 32),
                "status": "queued",
                "stage": "reading",
                "attempt_count": 0,
            }

        async def retry(
            self,
            _service: Any,
            job_id: str,
            client_request_id: str,
        ) -> dict[str, Any]:
            runner_calls.append(("retry", (job_id, client_request_id)))
            return {
                **service.job_status(job_id),
                "status": "queued",
                "stage": "reading",
            }

        async def cancel(self, _service: Any, job_id: str) -> dict[str, Any]:
            runner_calls.append(("cancel", job_id))
            return {
                **service.job_status(job_id),
                "status": "cancelled",
                "can_retry": True,
            }

    monkeypatch.setattr(rpc_memory_import, "_job_runner", lambda: _Runner())
    context = _ctx(service)
    common = {
        "agentId": "main",
        "expectedProvider": "synthetic",
        "expectedModel": "synthetic-model",
        "expectedIsLocal": False,
    }

    started = await _handle_memory_import_start(
        {
            **common,
            "rawText": "Synthetic imported profile",
            "clientRequestId": "start-request",
        },
        context,
    )
    status = await _handle_memory_import_status(
        {"agentId": "main", "jobId": "a" * 32},
        context,
    )
    retried = await _handle_memory_import_retry(
        {
            **common,
            "jobId": "a" * 32,
            "clientRequestId": "retry-request",
        },
        context,
    )
    cancelled = await _handle_memory_import_cancel(
        {"agentId": "main", "jobId": "a" * 32},
        context,
    )

    assert started["status"] == "queued"
    assert started["agentId"] == "main"
    assert status["status"] == "analyzing"
    assert retried["status"] == "queued"
    assert cancelled["status"] == "cancelled"
    assert [name for name, _value in runner_calls] == [
        "start",
        "retry",
        "cancel",
    ]


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        (8_192, 8_192),
        (32_768, 32_768),
        (128_000, 128_000),
    ],
)
def test_profile_import_output_budget_uses_the_default_model_limit(
    resolved: int,
    expected: int,
) -> None:
    assert _profile_import_output_tokens(resolved) == expected


@pytest.mark.asyncio
async def test_info_is_additively_unavailable_without_default_model() -> None:
    result = await _handle_memory_import_info(
        {},
        RpcContext(
            conn_id="test",
            config=SimpleNamespace(
                llm=SimpleNamespace(
                    provider="synthetic",
                    model="synthetic-model",
                    base_url="https://models.invalid/v1",
                )
            ),
        ),
    )

    assert result == {
        "schemaVersion": 1,
        "available": False,
        "provider": "synthetic",
        "model": "synthetic-model",
        "isLocal": False,
        "maxInputBytes": 262_144,
        "promptVersion": "profile-fusion-v2",
        "recentImport": None,
    }


@pytest.mark.asyncio
async def test_info_keeps_the_recent_receipt_when_default_model_is_unavailable(
    tmp_path: Path,
) -> None:
    from opensquilla.memory.profile_import import (
        ModelIdentity,
        ProfileImportPaths,
        ProfileImportPreviewRequest,
        ProfileImportService,
    )

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    memory_root = tmp_path / "memory"
    state.mkdir()
    workspace.mkdir()
    memory_root.mkdir()
    (workspace / "USER.md").write_text("# User\nAlice\n", encoding="utf-8")
    (memory_root / "MEMORY.md").write_text("# Memory\nBe concise.\n", encoding="utf-8")

    async def complete(_request: Any) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "candidate": {
                    "user_md": "# User\nAlice\n",
                    "memory_md": "# Memory\nBe concise.\n",
                    "import_md": None,
                },
                "decisions": [],
                "summary": ["Nothing new"],
            }
        )

    service = ProfileImportService(
        ProfileImportPaths(
            agent_id="main",
            agent_workspace_dir=workspace,
            memory_workspace_dir=memory_root,
            state_dir=state,
        ),
        ModelIdentity(provider="previous-provider", model="previous-model"),
        complete,
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The imported profile is already present.",
            client_request_id="recent-without-model",
        )
    )
    applied = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "recent-without-model-apply",
    )

    info = await _handle_memory_import_info(
        {"agentId": "main"},
        RpcContext(
            conn_id="test",
            config=SimpleNamespace(
                state_dir=str(state),
                workspace_dir=str(workspace),
                agents=[],
                llm=SimpleNamespace(
                    provider="previous-provider",
                    model="previous-model",
                    base_url="",
                ),
            ),
            memory_managers={
                "main": SimpleNamespace(
                    workspace_dir=memory_root,
                    memory_config=SimpleNamespace(
                        max_file_size_kb=1024,
                        max_total_size_kb=102400,
                        max_files=500,
                    ),
                )
            },
        ),
    )

    assert info["available"] is False
    assert info["recentImport"]["receiptId"] == applied.receipt_id
    assert info["recentImport"]["summary"] == ["Nothing new"]
    assert info["recentImport"]["targets"] == []


@pytest.mark.asyncio
async def test_unknown_agent_returns_stable_unavailable_before_provider_resolution() -> None:
    class _Selector:
        is_configured = True

        def clone(self) -> Any:
            raise AssertionError("an unknown agent must be rejected before provider resolution")

    with pytest.raises(RpcHandlerError) as captured:
        await _handle_memory_import_preview(
            {
                "agentId": "deleted-agent",
                "rawText": "Synthetic imported profile",
                "uiLocale": "en",
                "exportPromptVersion": "profile-export-v1",
                "clientRequestId": "unknown-agent-request",
                "expectedProvider": "synthetic",
                "expectedModel": "synthetic-model",
                "expectedIsLocal": False,
            },
            RpcContext(
                conn_id="test",
                provider_selector=_Selector(),
                memory_managers={},
            ),
        )

    assert captured.value.code == "MEMORY_IMPORT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_info_and_preview_return_camel_case_schema_v1() -> None:
    service = _FakeProfileImportService()
    ctx = _ctx(service)

    info = await _handle_memory_import_info({"agentId": "main"}, ctx)
    preview = await _handle_memory_import_preview(
        {
            "agentId": "main",
            "rawText": "Synthetic imported profile",
            "uiLocale": "en",
            "exportPromptVersion": "profile-export-v1",
            "clientRequestId": "client-1",
            "expectedProvider": "synthetic",
            "expectedModel": "synthetic-model",
            "expectedIsLocal": False,
        },
        ctx,
    )

    assert info == {
        "schemaVersion": 1,
        "available": True,
        "provider": "synthetic",
        "model": "synthetic-model",
        "isLocal": False,
        "maxInputBytes": 262_144,
        "promptVersion": "profile-fusion-v1",
        "recentImport": None,
    }
    assert preview["schemaVersion"] == 1
    assert preview["previewId"] == "preview-1"
    assert preview["decisionCounts"]["applied"] == 1
    assert preview["files"][0]["relativePath"] == "USER.md"
    request = service.calls[1][1]
    request_raw_text = (
        request["raw_text"] if isinstance(request, dict) else getattr(request, "raw_text")
    )
    assert request_raw_text == "Synthetic imported profile"


@pytest.mark.asyncio
async def test_preview_rejects_a_hot_model_change_before_calling_the_service() -> None:
    service = _FakeProfileImportService()
    service.model = SimpleNamespace(
        provider="new-provider",
        model="new-model",
        is_loopback=False,
    )

    with pytest.raises(RpcHandlerError) as captured:
        await _handle_memory_import_preview(
            {
                "agentId": "main",
                "rawText": "Sensitive synthetic imported profile",
                "uiLocale": "en",
                "exportPromptVersion": "profile-export-v1",
                "clientRequestId": "model-cas-request",
                "expectedProvider": "shown-provider",
                "expectedModel": "shown-model",
                "expectedIsLocal": False,
            },
            _ctx(service),
        )

    assert captured.value.code == "MEMORY_IMPORT_STALE_PREVIEW"
    assert service.calls == []


@pytest.mark.asyncio
async def test_preview_rejects_a_loopback_to_remote_change_before_model_use() -> None:
    service = _FakeProfileImportService()
    service.model = SimpleNamespace(
        provider="synthetic",
        model="synthetic-model",
        is_loopback=False,
    )

    with pytest.raises(RpcHandlerError) as captured:
        await _handle_memory_import_preview(
            {
                "agentId": "main",
                "rawText": "Sensitive synthetic imported profile",
                "uiLocale": "en",
                "exportPromptVersion": "profile-export-v1",
                "clientRequestId": "deployment-cas-request",
                "expectedProvider": "synthetic",
                "expectedModel": "synthetic-model",
                "expectedIsLocal": True,
            },
            _ctx(service),
        )

    assert captured.value.code == "MEMORY_IMPORT_STALE_PREVIEW"
    assert captured.value.details["actualIsLocal"] is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_undo_rejects_a_hot_model_change_before_stale_undo_analysis() -> None:
    class _StaleUndoService(_FakeProfileImportService):
        async def undo(self, receipt_id: str, client_request_id: str) -> dict[str, Any]:
            self.calls.append(("undo", (receipt_id, client_request_id)))
            return {
                "status": "reviewRequired",
                "receipt_id": receipt_id,
                "preview": None,
            }

    service = _StaleUndoService()
    service.model = SimpleNamespace(
        provider="new-provider",
        model="new-model",
        is_loopback=False,
    )

    with pytest.raises(RpcHandlerError) as captured:
        await _handle_memory_import_undo(
            {
                "receiptId": "receipt-1",
                "clientRequestId": "undo-model-cas",
                "expectedProvider": "shown-provider",
                "expectedModel": "shown-model",
                "expectedIsLocal": False,
            },
            _ctx(service),
        )

    assert captured.value.code == "MEMORY_IMPORT_STALE_PREVIEW"
    assert service.calls == [("undo", ("receipt-1", "undo-model-cas"))]


@pytest.mark.asyncio
async def test_exact_undo_remains_available_without_matching_model_identity() -> None:
    service = _FakeProfileImportService()
    service.model = SimpleNamespace(
        provider="unconfigured",
        model="unconfigured",
        is_loopback=False,
    )

    result = await _handle_memory_import_undo(
        {
            "receiptId": "receipt-1",
            "clientRequestId": "exact-undo-without-model",
            "expectedProvider": "previous-provider",
            "expectedModel": "previous-model",
            "expectedIsLocal": True,
        },
        _ctx(service),
    )

    assert result["status"] == "undone"
    assert service.calls == [
        ("undo", ("receipt-1", "exact-undo-without-model")),
    ]


@pytest.mark.asyncio
async def test_apply_undo_and_discard_forward_only_server_side_identifiers() -> None:
    service = _FakeProfileImportService()
    invalidated: list[str] = []
    refreshed: list[str] = []
    indexed: list[tuple[str, bool]] = []
    dirtied: list[bool] = []

    async def _sync(*, reason: str, force: bool) -> None:
        indexed.append((reason, force))

    runner = SimpleNamespace(
        invalidate_profile_snapshot=lambda agent_id: invalidated.append(agent_id),
        refresh_memory_snapshot=lambda agent_id: refreshed.append(agent_id),
    )
    manager = SimpleNamespace(
        sync=_sync,
        sync_manager=SimpleNamespace(mark_dirty=lambda: dirtied.append(True)),
    )
    ctx = _ctx(service, turn_runner=runner, memory_managers={"main": manager})

    applied = await _handle_memory_import_apply(
        {
            "previewId": "preview-1",
            "candidateHash": "candidate-hash",
            "idempotencyKey": "apply-1",
            # A client-supplied candidate must be ignored by the RPC contract.
            "candidate": {"userMd": "malicious replacement"},
        },
        ctx,
    )
    undone = await _handle_memory_import_undo(
        {
            "receiptId": "receipt-1",
            "clientRequestId": "undo-1",
            "expectedProvider": "synthetic",
            "expectedModel": "synthetic-model",
            "expectedIsLocal": False,
        },
        ctx,
    )
    discarded = await _handle_memory_import_discard({"previewId": "preview-2"}, ctx)

    assert applied["status"] == "applied"
    assert applied["receiptId"] == "receipt-1"
    assert applied["indexStatus"] == "ready"
    assert undone["status"] == "undone"
    assert discarded == {
        "schemaVersion": 1,
        "status": "discarded",
        "previewId": "preview-2",
    }
    assert ("apply", ("preview-1", "candidate-hash", "apply-1")) in service.calls
    assert ("undo", ("receipt-1", "undo-1")) in service.calls
    assert invalidated == ["main", "main"]
    assert refreshed == ["main", "main"]
    assert indexed == [("profile_import", True), ("profile_import", True)]
    assert dirtied == [True, True]


@pytest.mark.asyncio
async def test_index_failure_marks_committed_apply_pending() -> None:
    async def _failed_sync(**_kwargs: Any) -> None:
        raise OSError("synthetic index failure")

    result = await _handle_memory_import_apply(
        {
            "previewId": "preview-1",
            "candidateHash": "candidate-hash",
            "idempotencyKey": "apply-1",
        },
        _ctx(
            _FakeProfileImportService(),
            memory_managers={
                "main": SimpleNamespace(
                    sync=_failed_sync,
                    sync_manager=SimpleNamespace(mark_dirty=lambda: None),
                )
            },
        ),
    )

    assert result["status"] == "applied"
    assert result["indexStatus"] == "pending"


@pytest.mark.asyncio
async def test_dirty_marker_failure_does_not_turn_a_committed_apply_into_an_rpc_error() -> None:
    async def _sync(**_kwargs: Any) -> None:
        return None

    def _failed_mark_dirty() -> None:
        raise OSError("synthetic dirty marker failure")

    result = await _handle_memory_import_apply(
        {
            "previewId": "preview-1",
            "candidateHash": "candidate-hash",
            "idempotencyKey": "apply-1",
        },
        _ctx(
            _FakeProfileImportService(),
            memory_managers={
                "main": SimpleNamespace(
                    sync=_sync,
                    sync_manager=SimpleNamespace(mark_dirty=_failed_mark_dirty),
                )
            },
        ),
    )

    assert result["status"] == "applied"
    assert result["indexStatus"] == "pending"


@pytest.mark.asyncio
async def test_preview_refreshes_runtime_and_index_after_finalizing_a_published_journal() -> None:
    index_statuses: list[tuple[str, str]] = []

    class _RecoveryStore:
        receipt = SimpleNamespace(receipt_id="receipt-recovered", files=[])

        def load_receipt_by_batch(self, batch_id: str) -> Any:
            assert batch_id == "batch-recovered"
            return self.receipt

        def load_receipt(self, receipt_id: str) -> Any:
            assert receipt_id == "receipt-recovered"
            return self.receipt

    class _RecoveryService(_FakeProfileImportService):
        def __init__(self) -> None:
            super().__init__()
            self.store = _RecoveryStore()

        async def recover(self) -> list[str]:
            return ["batch-recovered"]

        async def set_index_status(self, receipt_id: str, status: str) -> None:
            index_statuses.append((receipt_id, status))

    invalidated: list[str] = []
    refreshed: list[str] = []
    dirtied: list[bool] = []
    runner = SimpleNamespace(
        invalidate_profile_snapshot=lambda agent_id: invalidated.append(agent_id),
        refresh_memory_snapshot=lambda agent_id: refreshed.append(agent_id),
    )
    manager = SimpleNamespace(
        store=SimpleNamespace(
            index_file=lambda **_kwargs: None,
            remove_file=lambda _path: None,
        ),
        sync_manager=SimpleNamespace(mark_dirty=lambda: dirtied.append(True)),
    )
    service = _RecoveryService()

    preview = await _handle_memory_import_preview(
        {
            "agentId": "main",
            "rawText": "Synthetic imported profile",
            "clientRequestId": "after-recovery",
            "expectedProvider": "synthetic",
            "expectedModel": "synthetic-model",
            "expectedIsLocal": False,
        },
        _ctx(
            service,
            turn_runner=runner,
            memory_managers={"main": manager},
        ),
    )

    assert preview["previewId"] == "preview-1"
    assert invalidated == ["main"]
    assert refreshed == ["main"]
    assert dirtied == [True]
    assert index_statuses == [("receipt-recovered", "ready")]


@pytest.mark.asyncio
async def test_failed_recovery_forces_snapshot_invalidation_and_full_index_sync() -> None:
    class _PartiallyRecoveredService(_FakeProfileImportService):
        async def recover(self) -> list[str]:
            raise OSError("synthetic later journal failure")

    invalidated: list[str] = []
    refreshed: list[str] = []
    dirtied: list[bool] = []
    synced: list[tuple[str, bool]] = []

    async def _sync(*, reason: str, force: bool) -> None:
        synced.append((reason, force))

    response = await get_dispatcher().dispatch(
        "request-after-partial-recovery",
        "memory.import.preview",
        {
            "agentId": "main",
            "rawText": "Synthetic imported profile",
            "clientRequestId": "after-partial-recovery",
            "expectedProvider": "synthetic",
            "expectedModel": "synthetic-model",
            "expectedIsLocal": False,
        },
        _ctx(
            _PartiallyRecoveredService(),
            turn_runner=SimpleNamespace(
                invalidate_profile_snapshot=lambda agent_id: invalidated.append(agent_id),
                refresh_memory_snapshot=lambda agent_id: refreshed.append(agent_id),
            ),
            memory_managers={
                "main": SimpleNamespace(
                    sync=_sync,
                    sync_manager=SimpleNamespace(
                        mark_dirty=lambda: dirtied.append(True)
                    ),
                )
            },
        ),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "MEMORY_IMPORT_WRITE_FAILED"
    assert invalidated == ["main"]
    assert refreshed == ["main"]
    assert dirtied == [True]
    assert synced == [("profile_import_recovery", True)]


@pytest.mark.asyncio
async def test_stable_core_error_code_is_preserved() -> None:
    class _Error(RuntimeError):
        code = "MEMORY_IMPORT_STALE_PREVIEW"
        details = {"expected_hash": "before", "actual_hash": "after"}

    class _Service(_FakeProfileImportService):
        async def apply(self, *_args: Any) -> dict[str, Any]:
            raise _Error("The profile changed after preview.")

    response = await get_dispatcher().dispatch(
        "request-1",
        "memory.import.apply",
        {
            "previewId": "preview-1",
            "candidateHash": "candidate-hash",
            "idempotencyKey": "apply-1",
        },
        _ctx(_Service()),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "MEMORY_IMPORT_STALE_PREVIEW"
    assert response.error.details == {
        "expectedHash": "before",
        "actualHash": "after",
    }


@pytest.mark.asyncio
async def test_read_only_principal_can_only_call_info() -> None:
    service = _FakeProfileImportService()
    ctx = _ctx(
        service,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )
    dispatcher = get_dispatcher()

    info = await dispatcher.dispatch("info", "memory.import.info", {}, ctx)
    preview = await dispatcher.dispatch(
        "preview",
        "memory.import.preview",
        {
            "rawText": "Synthetic imported profile",
            "clientRequestId": "client-1",
        },
        ctx,
    )

    assert info.ok is True
    assert preview.ok is False
    assert preview.error is not None
    assert preview.error.code == "UNAUTHORIZED"


def test_profile_import_method_scope_contract() -> None:
    assert METHOD_SCOPES["memory.import.info"] == "operator.read"
    for method in (
        "memory.import.preview",
        "memory.import.start",
        "memory.import.status",
        "memory.import.cancel",
        "memory.import.retry",
        "memory.import.apply",
        "memory.import.undo",
        "memory.import.discard",
    ):
        assert METHOD_SCOPES[method] == "operator.admin"


def test_gateway_awaits_canonical_profile_recovery_before_turns_and_readiness() -> None:
    from opensquilla.gateway import boot

    source = inspect.getsource(boot.start_gateway_server)
    recovery = source.index("await run_profile_import_startup_recovery(")
    turn_runner = source.index("turn_runner = build_turn_runner_from_services(")
    server = source.index("task = create_background_task(server.serve())")
    ready = source.index("app.state.gateway_ready = True")

    assert recovery < turn_runner < server < ready


@pytest.mark.asyncio
async def test_startup_maintenance_sweeps_expired_isolated_raw_inputs(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    memory_root = tmp_path / "memory"
    batch_dir = state / "profile-imports" / "main" / ("a" * 32)
    state.mkdir()
    if os.name != "nt":
        state.chmod(0o751)
    batch_dir.mkdir(parents=True)
    workspace.mkdir()
    memory_root.mkdir()
    raw = batch_dir / "raw.txt"
    raw.write_text("Synthetic private input", encoding="utf-8")
    old = datetime.now(UTC) - timedelta(hours=25)
    os.utime(raw, (old.timestamp(), old.timestamp()))

    failures = await run_profile_import_startup_maintenance(
        config=SimpleNamespace(
            state_dir=str(state),
            workspace_dir=str(workspace),
            agents=[],
        ),
        memory_managers={
            "main": SimpleNamespace(
                workspace_dir=memory_root,
            )
        },
    )

    assert failures == {}
    assert not raw.exists()
    if os.name != "nt":
        assert stat.S_IMODE(state.stat().st_mode) == 0o751
        assert stat.S_IMODE((state / "profile-imports").stat().st_mode) == 0o700
        assert stat.S_IMODE(batch_dir.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_startup_recovery_processes_agents_serially_without_lock_self_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.memory.profile_import as profile_import

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_roots = {
        "alpha": tmp_path / "memory-alpha",
        "beta": tmp_path / "memory-beta",
    }
    for root in memory_roots.values():
        root.mkdir()
    active = 0
    max_active = 0
    order: list[str] = []

    class _SerialRecoveryService:
        def __init__(self, paths: Any, *_args: Any, **_kwargs: Any) -> None:
            self.agent_id = paths.agent_id

        async def recover(self) -> list[str]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append(f"start:{self.agent_id}")
            await asyncio.sleep(0)
            order.append(f"end:{self.agent_id}")
            active -= 1
            return [f"batch-{self.agent_id}"]

    monkeypatch.setattr(
        profile_import,
        "ProfileImportService",
        _SerialRecoveryService,
    )
    recovered = await run_profile_import_startup_recovery(
        config=SimpleNamespace(
            state_dir=str(state),
            workspace_dir=str(workspace),
            agents=[],
        ),
        memory_managers={
            agent_id: SimpleNamespace(workspace_dir=root)
            for agent_id, root in memory_roots.items()
        },
    )

    assert recovered == {
        "alpha": ["batch-alpha"],
        "beta": ["batch-beta"],
    }
    assert max_active == 1
    assert order == [
        "start:alpha",
        "end:alpha",
        "start:beta",
        "end:beta",
    ]


@pytest.mark.asyncio
async def test_startup_recovery_failure_stops_before_later_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.memory.profile_import as profile_import

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_roots = {
        "alpha": tmp_path / "memory-alpha",
        "beta": tmp_path / "memory-beta",
    }
    for root in memory_roots.values():
        root.mkdir()
    started: list[str] = []

    class _FailingRecoveryService:
        def __init__(self, paths: Any, *_args: Any, **_kwargs: Any) -> None:
            self.agent_id = paths.agent_id

        async def recover(self) -> list[str]:
            started.append(self.agent_id)
            raise OSError("synthetic recovery failure")

    monkeypatch.setattr(
        profile_import,
        "ProfileImportService",
        _FailingRecoveryService,
    )

    with pytest.raises(
        RuntimeError,
        match="startup recovery failed for agent 'alpha'",
    ):
        await run_profile_import_startup_recovery(
            config=SimpleNamespace(
                state_dir=str(state),
                workspace_dir=str(workspace),
                agents=[],
            ),
            memory_managers={
                agent_id: SimpleNamespace(workspace_dir=root)
                for agent_id, root in memory_roots.items()
            },
        )

    assert started == ["alpha"]


def test_profile_import_paths_resolve_state_symlink_before_lock_root_derivation(
    tmp_path: Path,
) -> None:
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks are unavailable")
    physical_state = tmp_path / "physical" / "state"
    physical_state.mkdir(parents=True)
    state_link = tmp_path / "state-link"
    try:
        state_link.symlink_to(physical_state, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    workspace = tmp_path / "workspace"
    memory_root = tmp_path / "memory"
    workspace.mkdir()
    memory_root.mkdir()
    ctx = RpcContext(
        conn_id="test",
        config=SimpleNamespace(
            state_dir=str(state_link),
            workspace_dir=str(workspace),
            agents=[],
        ),
        memory_managers={
            "main": SimpleNamespace(workspace_dir=memory_root),
        },
    )

    paths = _profile_import_paths(ctx, "main")

    assert paths.state_dir == physical_state.resolve()
    assert paths.operation_lock_root == physical_state.resolve().parent


@pytest.mark.asyncio
async def test_gateway_completion_reuses_one_resolved_primary_without_tools_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", raising=False)
    class _Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any]] = []

        async def chat(self, messages: Any, *, tools: Any, config: Any) -> Any:
            self.calls.append((tools, config))
            yield TextDeltaEvent(text='{"schema_version":1}')
            yield DoneEvent(model="synthetic-model", provider="synthetic")

    class _Selector:
        def __init__(self, provider: Any) -> None:
            self.provider = provider
            self.resolve_calls = 0
            self.fallback_calls = 0

        def resolve(self) -> Any:
            self.resolve_calls += 1
            return self.provider

        def next_fallback(self) -> Any:
            self.fallback_calls += 1
            raise AssertionError("profile import must never advance the fallback chain")

    provider = _Provider()
    selector = _Selector(provider)
    completion = _GatewayFusionCompletion(
        ctx=RpcContext(
            conn_id="test",
            config=SimpleNamespace(llm=SimpleNamespace(max_tokens=64)),
        ),
        selector=selector,
        provider_id="synthetic",
        model="synthetic-model",
        agent_id="main",
    )
    request = SimpleNamespace(
        system_prompt="Return JSON.",
        user_prompt="Synthetic profile",
        response_schema={"type": "object"},
    )

    first = await completion(request)
    second = await completion(request)

    assert first == second == '{"schema_version":1}'
    assert selector.resolve_calls == 1
    assert selector.fallback_calls == 0
    assert len(provider.calls) == 2
    assert all(tools is None for tools, _config in provider.calls)
    assert all(
        config.provider_request_correlation.call_kind == "auxiliary.profile_import"
        for _tools, config in provider.calls
    )


@pytest.mark.asyncio
async def test_gateway_completion_uses_the_configured_llm_request_timeout() -> None:
    class _Provider:
        async def chat(self, _messages: Any, *, tools: Any, config: Any) -> Any:
            assert tools is None
            assert config.output_json_schema is not None
            assert config.timeout == 0.001
            await asyncio.sleep(1)
            yield TextDeltaEvent(text="too late")

    selector = SimpleNamespace(resolve=lambda: _Provider())
    completion = _GatewayFusionCompletion(
        ctx=RpcContext(conn_id="test"),
        selector=selector,
        provider_id="synthetic",
        model="synthetic-model",
        agent_id="main",
        request_timeout=0.001,
    )

    with pytest.raises(TimeoutError):
        await completion(
            SimpleNamespace(
                system_prompt="Return JSON.",
                user_prompt="Synthetic profile",
                response_schema={"type": "object"},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["stalled", "failed", "cancelled"])
async def test_gateway_completion_bounds_and_suppresses_stream_cleanup(
    close_mode: str,
) -> None:
    class _Stream:
        def __init__(self) -> None:
            self.events = iter(
                [
                    TextDeltaEvent(text='{"schema_version":1}'),
                    DoneEvent(model="synthetic-model", provider="synthetic"),
                ]
            )

        def __aiter__(self) -> _Stream:
            return self

        async def __anext__(self) -> Any:
            try:
                return next(self.events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            if close_mode == "stalled":
                await asyncio.Event().wait()
            if close_mode == "failed":
                raise RuntimeError("synthetic close failure")
            if close_mode == "cancelled":
                raise asyncio.CancelledError

    class _Provider:
        accounts_physical_usage = True

        def chat(self, _messages: Any, *, tools: Any, config: Any) -> _Stream:
            assert tools is None
            return _Stream()

    completion = _GatewayFusionCompletion(
        ctx=RpcContext(conn_id="test"),
        selector=SimpleNamespace(resolve=lambda: _Provider()),
        provider_id="synthetic",
        model="synthetic-model",
        agent_id="main",
        request_timeout=0.01,
    )

    result = await asyncio.wait_for(
        completion(
            SimpleNamespace(
                system_prompt="Return JSON.",
                user_prompt="Synthetic profile",
                response_schema={"type": "object"},
            )
        ),
        timeout=0.25,
    )

    assert result == '{"schema_version":1}'


@pytest.mark.asyncio
async def test_gateway_completion_rejects_a_reported_model_change_without_fallback() -> None:
    class _Provider:
        async def chat(self, _messages: Any, *, tools: Any, config: Any) -> Any:
            assert tools is None
            yield TextDeltaEvent(text='{"schema_version":1}')
            yield DoneEvent(model="different-model", provider="synthetic")

    class _Selector:
        fallback_calls = 0

        def resolve(self) -> _Provider:
            return _Provider()

        def next_fallback(self) -> Any:
            self.fallback_calls += 1
            raise AssertionError("profile import must never use a fallback")

    selector = _Selector()
    completion = _GatewayFusionCompletion(
        ctx=RpcContext(conn_id="test"),
        selector=selector,
        provider_id="synthetic",
        model="synthetic-model",
        agent_id="main",
    )

    with pytest.raises(RuntimeError, match="did not match the configured model"):
        await completion(
            SimpleNamespace(
                system_prompt="Return JSON.",
                user_prompt="Synthetic profile",
                response_schema={"type": "object"},
            )
        )
    assert selector.fallback_calls == 0


@pytest.mark.asyncio
async def test_real_core_preview_and_apply_use_only_one_direct_model_call(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    (workspace / "USER.md").write_text("# USER.md\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("# MEMORY.md\n", encoding="utf-8")

    response = json.dumps(
        {
            "schema_version": 1,
            "candidate": {
                "user_md": "# USER.md\n\nName: Alice\n",
                "memory_md": "# MEMORY.md\n\n- Prefers concise answers.\n",
                "import_md": None,
            },
            "decisions": [
                {
                    "outcome": "applied",
                    "target": "USER",
                    "source_excerpt": "Name: Alice",
                    "candidate_excerpt": "Name: Alice",
                    "date": "unknown",
                    "model_confidence": "high",
                    "reason": "Explicitly supplied stable identity.",
                },
                {
                    "outcome": "applied",
                    "target": "MEMORY",
                    "source_excerpt": "Prefers concise answers",
                    "candidate_excerpt": "- Prefers concise answers.",
                    "date": "unknown",
                    "model_confidence": "high",
                    "reason": "Explicitly supplied durable preference.",
                }
            ],
            "summary": ["Added one profile field and one durable preference."],
        }
    )

    class _Provider:
        calls = 0

        async def chat(self, _messages: Any, *, tools: Any, config: Any) -> Any:
            assert tools is None
            assert config.output_json_schema is not None
            type(self).calls += 1
            yield TextDeltaEvent(text=response)
            yield DoneEvent(model="synthetic-model", provider="synthetic")

    class _Clone:
        def __init__(self) -> None:
            self.replay_disabled = False
            self.resolve_calls = 0

        def disable_provider_state_replay(self) -> None:
            self.replay_disabled = True

        def remaining_chain(self) -> list[Any]:
            return [
                SimpleNamespace(
                    provider="synthetic",
                    model="synthetic-model",
                    base_url="https://models.invalid/v1",
                )
            ]

        @property
        def active_provider_id(self) -> str:
            return "synthetic"

        def resolve(self) -> Any:
            self.resolve_calls += 1
            assert self.replay_disabled is True
            return _Provider()

        def next_fallback(self) -> Any:
            raise AssertionError("the profile import path must not use fallbacks")

    class _Selector:
        is_configured = True

        def __init__(self) -> None:
            self.clones: list[_Clone] = []

        def clone(self) -> _Clone:
            clone = _Clone()
            self.clones.append(clone)
            return clone

    fallback_syncs: list[tuple[str, bool]] = []
    indexed: list[tuple[str, str]] = []

    async def _sync(*, reason: str, force: bool) -> None:
        fallback_syncs.append((reason, force))

    class _IndexStore:
        async def index_file(self, *, path: str, content: str, source: Any) -> int:
            indexed.append((path, content))
            return 1

        async def remove_file(self, path: str) -> None:
            raise AssertionError(f"unexpected index removal: {path}")

    memory_config = SimpleNamespace(
        max_file_size_kb=1024,
        max_total_size_kb=102400,
        max_files=500,
    )
    manager = SimpleNamespace(
        workspace_dir=workspace,
        memory_dir=workspace / "memory",
        memory_config=memory_config,
        store=_IndexStore(),
        sync=_sync,
        sync_manager=SimpleNamespace(mark_dirty=lambda: None),
    )
    selector = _Selector()
    ctx = RpcContext(
        conn_id="test",
        config=SimpleNamespace(
            state_dir=str(state),
            workspace_dir=str(workspace),
            agents=[],
            memory=memory_config,
            llm=SimpleNamespace(
                provider="synthetic",
                model="synthetic-model",
                base_url="https://models.invalid/v1",
                max_tokens=1024,
            ),
        ),
        provider_selector=selector,
        memory_managers={"main": manager},
    )

    info = await _handle_memory_import_info({"agentId": "main"}, ctx)
    preview = await _handle_memory_import_preview(
        {
            "agentId": "main",
            "rawText": "Name: Alice\nPrefers concise answers",
            "uiLocale": "en",
            "exportPromptVersion": "profile-export-v1",
            "clientRequestId": "synthetic-request",
            "expectedProvider": "synthetic",
            "expectedModel": "synthetic-model",
            "expectedIsLocal": False,
        },
        ctx,
    )
    applied = await _handle_memory_import_apply(
        {
            "previewId": preview["previewId"],
            "candidateHash": preview["candidateHash"],
            "idempotencyKey": "synthetic-apply",
        },
        ctx,
    )
    refreshed_info = await _handle_memory_import_info({"agentId": "main"}, ctx)

    assert info["available"] is True
    assert info["maxInputBytes"] == 262_144
    assert preview["provider"] == "synthetic"
    assert preview["model"] == "synthetic-model"
    assert preview["files"][0]["relativePath"] == "USER.md"
    assert applied["status"] == "applied"
    assert applied["indexStatus"] == "ready"
    assert (workspace / "USER.md").read_text(encoding="utf-8").endswith(
        "Name: Alice\n"
    )
    assert _Provider.calls == 1
    assert sum(clone.resolve_calls for clone in selector.clones) == 1
    with (workspace / "MEMORY.md").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as memory_file:
        assert indexed == [("MEMORY.md", memory_file.read())]
    assert fallback_syncs == []
    assert refreshed_info["recentImport"]["indexStatus"] == "ready"
