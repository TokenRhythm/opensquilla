from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import opensquilla.memory.profile_import.files as profile_import_files
import opensquilla.memory.profile_import.transaction as profile_import_transaction
import opensquilla.profile_import_io as profile_import_io
from opensquilla.memory.profile_import import (
    ModelIdentity,
    ProfileImportInputTooLargeError,
    ProfileImportInvalidOutputError,
    ProfileImportModelError,
    ProfileImportPaths,
    ProfileImportPreviewRequest,
    ProfileImportQuotas,
    ProfileImportService,
    ProfileImportStalePreviewError,
    ProfileImportWriteError,
    lookup_job_agent,
    lookup_preview_agent,
    lookup_receipt_agent,
)
from opensquilla.memory.profile_import.jobs import ProfileImportJobRunner
from opensquilla.memory.profile_import.models import DecisionTarget, FusionModelRequest
from opensquilla.memory.profile_import.prompts import (
    FUSION_SYSTEM_PROMPT,
    UNDO_SYSTEM_PROMPT,
)


def _ids() -> Iterator[str]:
    for value in range(1, 100):
        yield f"{value:032x}"


def _fusion_json(
    *,
    user: str,
    memory: str,
    imported: str | None = None,
    source_excerpt: str = "likes tea",
    decision_target: str = "USER",
    import_source_excerpt: str = "",
) -> str:
    decisions = (
        [
            {
                "outcome": "applied",
                "target": decision_target,
                "source_excerpt": source_excerpt,
                "candidate_excerpt": source_excerpt,
                "date": "unknown",
                "model_confidence": "high",
                "reason": "Explicitly stated",
            }
        ]
        if source_excerpt
        else []
    )
    if import_source_excerpt:
        decisions.append(
            {
                "outcome": "applied",
                "target": "IMPORT",
                "source_excerpt": import_source_excerpt,
                "candidate_excerpt": import_source_excerpt,
                "date": "unknown",
                "model_confidence": "high",
                "reason": "Explicitly stated",
            }
        )
    return json.dumps(
        {
            "schema_version": 1,
            "candidate": {
                "user_md": user,
                "memory_md": memory,
                "import_md": imported,
            },
            "decisions": decisions,
            "summary": ["Imported profile details"],
        }
    )


def _paths(tmp_path: Path) -> ProfileImportPaths:
    return ProfileImportPaths(
        agent_id="main",
        agent_workspace_dir=tmp_path / "workspace",
        memory_workspace_dir=tmp_path / "memory-state",
        state_dir=tmp_path / "state",
    )


def _service(
    tmp_path: Path,
    responses: list[str],
    calls: list[FusionModelRequest],
    *,
    quotas: ProfileImportQuotas | None = None,
) -> ProfileImportService:
    identifiers = _ids()

    async def complete(request: FusionModelRequest) -> str:
        calls.append(request)
        return responses.pop(0)

    return ProfileImportService(
        _paths(tmp_path),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        complete,
        quotas=quotas,
        now=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        id_factory=lambda: next(identifiers),
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )


def _prepare_baseline(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.agent_workspace_dir.mkdir(parents=True)
    paths.memory_workspace_dir.mkdir(parents=True)
    paths.user_path.write_text("# User\nAlice\n", encoding="utf-8")
    paths.memory_path.write_text("# Memory\nBe concise.\n", encoding="utf-8")


def test_fusion_and_undo_prompts_preserve_each_candidate_target_language() -> None:
    fusion = " ".join(FUSION_SYSTEM_PROMPT.split())
    undo = " ".join(UNDO_SYSTEM_PROMPT.split())
    assert "Prompt version: profile-fusion-v3" in fusion
    assert "target's existing dominant language" in fusion
    assert "only when that target is empty" in fusion
    assert "Use the requested UI locale for summary" in fusion
    assert '"Imported from: <name>" line is provenance metadata only' in fusion
    assert "any applied decision targets IMPORT" in fusion
    assert "candidate.import_md must be a non-empty" in fusion
    assert "If candidate.import_md is non-empty" in fusion
    assert "at least one applied decision must target IMPORT" in fusion
    assert "never for candidate content" in undo


@pytest.mark.asyncio
async def test_preview_apply_and_exact_undo_are_private_idempotent_and_recoverable(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
                imported="# Project\n[unknown] - Built a tea tracker.\n",
                import_source_excerpt="built a tea tracker",
            )
        ],
        calls,
    )
    request = ProfileImportPreviewRequest(
        raw_text=(
            "The user likes tea and built a tea tracker.\n"
            "Imported from: Codex"
        ),
        ui_locale="en",
        client_request_id="request-1",
    )

    preview = await service.preview(request)

    assert len(calls) == 1
    assert calls[0].identity.provider == "configured-provider"
    assert calls[0].identity.model == "configured-model"
    assert "schema_version" in calls[0].response_schema["properties"]
    assert preview.no_changes is False
    assert {item.target.value for item in preview.files} == {"USER", "IMPORT"}
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    raw_path = (
        _paths(tmp_path).agent_import_state_dir / preview.batch_id / "raw.txt"
    )
    assert raw_path.read_text(encoding="utf-8") == request.raw_text
    assert lookup_preview_agent(_paths(tmp_path).state_dir, preview.preview_id) == "main"

    applied = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "apply-key",
    )

    assert applied.status == "applied"
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nLikes tea.\n"
    )
    import_path = (
        _paths(tmp_path).imports_dir / f"{preview.batch_id}.md"
    )
    assert import_path.exists()
    assert not raw_path.exists()
    assert lookup_receipt_agent(_paths(tmp_path).state_dir, applied.receipt_id) == "main"
    receipt = service.store.load_receipt(applied.receipt_id)
    assert receipt.declared_source == "Codex"
    await service.set_index_status(applied.receipt_id, "ready")
    await service.recover()
    indexed_info = await service.info()
    assert indexed_info.recent_import is not None
    assert indexed_info.recent_import.index_status == "ready"
    assert indexed_info.recent_import.file_count == 2
    assert {target.value for target in indexed_info.recent_import.targets} == {
        "USER",
        "IMPORT",
    }

    repeated = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "apply-key",
    )
    assert repeated.status == "alreadyApplied"
    assert repeated.receipt_id == applied.receipt_id

    undone = await service.undo(applied.receipt_id, "undo-request")
    assert undone.status == "undone"
    undone_info = await service.info()
    assert undone_info.recent_import is not None
    assert undone_info.recent_import.index_status == "pending"
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert not import_path.exists()

    repeated_undo = await service.undo(applied.receipt_id, "undo-request")
    assert repeated_undo.status == "alreadyUndone"
    with pytest.raises(ProfileImportStalePreviewError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "apply-key",
        )


@pytest.mark.asyncio
async def test_private_state_permissions_do_not_change_the_configured_state_root(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Windows DACL semantics are covered by Windows integration tests")
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(mode=0o751)
    paths.state_dir.chmod(0o751)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="private-permissions",
        )
    )

    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o751
    private_root = paths.state_dir / "profile-imports"
    for directory in (
        private_root,
        paths.agent_import_state_dir,
        service.store.batch_dir(preview.batch_id),
        private_root / "_locators",
        paths.agent_import_state_dir / "_indexes",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file_path in private_root.rglob("*"):
        if file_path.is_file():
            assert stat.S_IMODE(file_path.stat().st_mode) == 0o600


def test_windows_private_acl_uses_current_sid_and_a_bound_handle() -> None:
    events: list[tuple[object, ...]] = []

    class _FakeNative:
        def current_user_sid(self) -> str:
            return "S-1-5-21-123"

        @contextlib.contextmanager
        def open_bound(
            self,
            path: Path,
            *,
            directory: bool,
            expected_device: int,
            expected_inode: int,
        ) -> Iterator[object]:
            events.append(("open", path, directory, expected_device, expected_inode))
            yield "bound-handle"
            events.append(("close", "bound-handle"))

        def set_protected_dacl(self, handle: object, sddl: str) -> None:
            events.append(("set", handle, sddl))

        def read_dacl_sddl(self, handle: object) -> str:
            events.append(("read", handle))
            return "O:S-1-5-21-123D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)"

        def create_directory(self, path: Path, sddl: str) -> None:
            events.append(("create", path, sddl))

    native = _FakeNative()
    profile_import_files._apply_windows_private_dacl(
        Path("private.json"),
        directory=False,
        expected_device=7,
        expected_inode=42,
        native=native,
    )
    profile_import_files._create_windows_private_directory(
        Path("profile-imports"),
        native=native,
    )

    assert events == [
        ("open", Path("private.json"), False, 7, 42),
        (
            "set",
            "bound-handle",
            "D:P(A;;FA;;;S-1-5-21-123)(A;;FA;;;SY)",
        ),
        ("read", "bound-handle"),
        ("close", "bound-handle"),
        (
            "create",
            Path("profile-imports"),
            "D:P(A;OICI;FA;;;S-1-5-21-123)(A;OICI;FA;;;SY)",
        ),
    ]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL APIs")
def test_windows_acl_canonicalizer_resolves_sddl_principal_aliases() -> None:
    from opensquilla import private_paths

    native = private_paths._CtypesWindowsPrivateAcl()

    assert native.canonical_sid("SY") == "S-1-5-18"
    assert native.canonical_sid("BA") == "S-1-5-32-544"
    local_administrator = native.canonical_sid("LA")
    assert local_administrator.startswith("S-1-5-21-")
    assert local_administrator.endswith("-500")


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL APIs")
def test_profile_import_directory_round_trips_native_private_acl(tmp_path: Path) -> None:
    from opensquilla import private_paths

    private_root = tmp_path / "profile-imports"
    private_root.mkdir()

    profile_import_files.restrict_private_path(private_root, directory=True)

    assert private_paths.windows_path_has_private_dacl(
        private_root,
        directory=True,
        require_protected=True,
    )


@pytest.mark.asyncio
async def test_invalid_json_is_retried_once_and_recovers_without_user_action(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            "not json",
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            ),
        ],
        calls,
    )

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="repair-request",
        )
    )

    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert preview.no_changes is False
    job = service.store.latest_draft_job()
    assert job is not None
    assert job.status.value == "ready"
    assert job.attempt_count == 1
    assert service.store.read_raw(job.batch_id) == "The user likes tea."


@pytest.mark.asyncio
async def test_full_json_code_fence_is_accepted(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    valid = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    service = _service(tmp_path, [f"```json\n{valid}\n```"], calls)

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="fenced-json-request",
        )
    )

    assert len(calls) == 1
    assert preview.no_changes is False
    assert {item.target.value for item in preview.files} == {"USER"}


@pytest.mark.asyncio
async def test_multiple_json_roots_are_rejected_then_retry_recovers(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    valid = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    service = _service(tmp_path, [f"{valid}\n{valid}", valid], calls)

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="multiple-roots-request",
        )
    )

    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert preview.no_changes is False


@pytest.mark.asyncio
async def test_applied_import_without_content_retries_and_recovers(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    source_excerpt = "built a tea tracker"
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported=None,
                source_excerpt=source_excerpt,
                decision_target="IMPORT",
            ),
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported="# Projects\n- Built a tea tracker.\n",
                source_excerpt=source_excerpt,
                decision_target="IMPORT",
            ),
        ],
        calls,
    )

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user built a tea tracker.",
            client_request_id="missing-import-content-recovers",
        )
    )

    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert {item.target.value for item in preview.files} == {"IMPORT"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "imported",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param(" \n\t", id="whitespace"),
    ],
)
async def test_applied_import_without_nonblank_content_fails_closed(
    tmp_path: Path,
    imported: str | None,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    source_excerpt = "built a tea tracker"
    inconsistent = _fusion_json(
        user="# User\nAlice\n",
        memory="# Memory\nBe concise.\n",
        imported=imported,
        source_excerpt=source_excerpt,
        decision_target="IMPORT",
    )
    service = _service(tmp_path, [inconsistent, inconsistent], calls)

    with pytest.raises(
        ProfileImportInvalidOutputError,
        match="applies IMPORT without non-empty IMPORT content",
    ):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user built a tea tracker.",
                client_request_id=f"missing-import-content-{imported!r}",
            )
        )

    assert len(calls) == 2
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )
    assert list(_paths(tmp_path).imports_dir.glob("*.md")) == []
    failed = service.store.latest_draft_job()
    assert failed is not None
    assert failed.status.value == "failed"
    assert failed.error_code == "MEMORY_IMPORT_INVALID_OUTPUT"
    assert service.store.read_raw(failed.batch_id) == "The user built a tea tracker."


@pytest.mark.asyncio
async def test_import_content_without_applied_import_decision_retries_and_recovers(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    inconsistent = _fusion_json(
        user="# User\nAlice\n",
        memory="# Memory\nBe concise.\n",
        imported="# Projects\n- Built a tea tracker.\n",
        source_excerpt="",
    )
    valid_no_change = _fusion_json(
        user="# User\nAlice\n",
        memory="# Memory\nBe concise.\n",
        imported=None,
        source_excerpt="",
    )
    service = _service(tmp_path, [inconsistent, valid_no_change], calls)

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user built a tea tracker.",
            client_request_id="unclaimed-import-content-recovers",
        )
    )

    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert preview.no_changes is True
    assert preview.files == []


@pytest.mark.asyncio
async def test_import_content_without_applied_import_decision_fails_closed(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    inconsistent = _fusion_json(
        user="# User\nAlice\n",
        memory="# Memory\nBe concise.\n",
        imported="# Projects\n- Built a tea tracker.\n",
        source_excerpt="",
    )
    service = _service(tmp_path, [inconsistent, inconsistent], calls)

    with pytest.raises(
        ProfileImportInvalidOutputError,
        match="contains IMPORT content without an applied IMPORT decision",
    ):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user built a tea tracker.",
                client_request_id="unclaimed-import-content-fails",
            )
        )

    assert len(calls) == 2
    assert list(_paths(tmp_path).imports_dir.glob("*.md")) == []


@pytest.mark.asyncio
async def test_nested_valid_json_object_in_invalid_wrapper_is_not_unwrapped(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    valid = json.loads(
        _fusion_json(
            user="# User\nAlice\nLikes tea.\n",
            memory="# Memory\nBe concise.\n",
        )
    )
    response = json.dumps({"result": valid})
    service = _service(tmp_path, [response, response], calls)

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id="nested-valid-object-request",
            )
        )

    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["array", "incomplete"])
async def test_nested_valid_json_object_in_non_object_root_is_not_unwrapped(
    tmp_path: Path,
    wrapper: str,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    valid = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    response = f"[{valid}]\n{{}}" if wrapper == "array" else f"[prefix {valid}"
    service = _service(tmp_path, [response, response], calls)

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id=f"nested-{wrapper}-object-request",
            )
        )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_schema_invalid_json_object_fails_without_writing_profile_files(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    response = json.dumps({"schema_version": 1})
    service = _service(tmp_path, [response, response], calls)

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id="schema-invalid-request",
            )
        )

    assert len(calls) == 2
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )
    failed = service.store.latest_draft_job()
    assert failed is not None
    assert failed.status.value == "failed"
    assert failed.error_code == "MEMORY_IMPORT_INVALID_OUTPUT"
    assert failed.attempt_count == 1
    assert service.store.read_raw(failed.batch_id) == "The user likes tea."


@pytest.mark.asyncio
async def test_invalid_evidence_is_retried_once_and_can_recover(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
                source_excerpt="a preference for tea",
            ),
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
                source_excerpt="likes tea",
            ),
        ],
        calls,
    )

    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="evidence-repair-request",
        )
    )

    assert len(calls) == 2
    assert preview.no_changes is False


@pytest.mark.asyncio
async def test_invalid_model_output_never_writes_profile_files(tmp_path: Path) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(tmp_path, ["bad", "still bad"], calls)

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id="invalid-request",
            )
        )

    assert len(calls) == 2
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )
    failed = service.store.latest_draft_job()
    assert failed is not None
    assert failed.status.value == "failed"
    assert failed.error_code == "MEMORY_IMPORT_INVALID_OUTPUT"
    assert failed.attempt_count == 1
    assert service.store.read_raw(failed.batch_id) == "The user likes tea."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError("synthetic timeout"), RuntimeError("synthetic 401")],
)
async def test_model_transport_failures_are_not_automatically_retried(
    tmp_path: Path,
    failure: Exception,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    identifiers = _ids()

    async def complete(request: FusionModelRequest) -> str:
        calls.append(request)
        raise failure

    service = ProfileImportService(
        _paths(tmp_path),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        complete,
        now=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        id_factory=lambda: next(identifiers),
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )

    with pytest.raises(ProfileImportModelError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id=f"transport-failure-{type(failure).__name__}",
            )
        )

    assert len(calls) == 1
    failed = service.store.latest_draft_job()
    assert failed is not None
    assert failed.status.value == "failed"
    assert failed.error_code == "MEMORY_IMPORT_MODEL_FAILED"
    assert failed.attempt_count == 1
    assert service.store.read_raw(failed.batch_id) == "The user likes tea."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_value", "expected_error"),
    [
        pytest.param(None, ProfileImportModelError, id="non-text"),
        pytest.param(
            "x" * (1024 * 1024 + 1),
            ProfileImportInvalidOutputError,
            id="oversized-text",
        ),
    ],
)
async def test_non_text_and_oversized_model_outputs_are_not_automatically_retried(
    tmp_path: Path,
    returned_value: object,
    expected_error: type[Exception],
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    identifiers = _ids()

    async def complete(request: FusionModelRequest) -> str:
        calls.append(request)
        return returned_value  # type: ignore[return-value]

    service = ProfileImportService(
        _paths(tmp_path),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        complete,
        now=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        id_factory=lambda: next(identifiers),
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )

    with pytest.raises(expected_error):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id=f"invalid-return-{type(returned_value).__name__}",
            )
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_background_job_returns_immediately_and_cancel_preserves_raw(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[FusionModelRequest] = []
    identifiers = _ids()

    async def complete(request: FusionModelRequest) -> str:
        calls.append(request)
        entered.set()
        await release.wait()
        return _fusion_json(
            user="# User\nAlice\nLikes tea.\n",
            memory="# Memory\nBe concise.\n",
        )

    service = ProfileImportService(
        _paths(tmp_path),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        complete,
        now=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        id_factory=lambda: next(identifiers),
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )
    runner = ProfileImportJobRunner()
    job = await runner.start(
        service,
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="background-start",
        ),
    )

    assert job.status.value == "queued"
    await entered.wait()
    assert (await service.job_status(job.job_id)).status.value == "analyzing"
    cancelled = await runner.cancel(service, job.job_id)
    assert cancelled.status.value == "cancelled"
    assert service.store.read_raw(job.batch_id) == "The user likes tea."
    assert len(calls) == 1
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"


@pytest.mark.asyncio
async def test_restart_marks_queued_job_interrupted_without_calling_model(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        calls,
    )
    job = await service.prepare_job(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="restart-job",
        )
    )

    assert await service.recover_jobs() == 1
    recovered = await service.job_status(job.job_id)
    assert recovered.status.value == "interrupted"
    assert recovered.can_retry is True
    assert calls == []


@pytest.mark.asyncio
async def test_runner_shutdown_marks_analyzing_job_interrupted(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    entered = asyncio.Event()

    async def complete(_request: FusionModelRequest) -> str:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = ProfileImportService(
        _paths(tmp_path),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        complete,
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )
    runner = ProfileImportJobRunner()
    job = await runner.start(
        service,
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="shutdown-job",
        ),
    )
    await entered.wait()

    await runner.shutdown()

    interrupted = await service.job_status(job.job_id)
    assert interrupted.status.value == "interrupted"
    assert interrupted.can_retry is True
    assert service.store.read_raw(job.batch_id) == "The user likes tea."


@pytest.mark.asyncio
async def test_preview_reuse_is_disk_backed_across_requests_and_service_instances(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    response = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    first = _service(tmp_path, [response], calls)
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="stable-request",
    )
    one = await first.preview(request)

    second = _service(tmp_path, [], calls)
    two = await second.preview(
        request.model_copy(update={"client_request_id": "different-request"})
    )

    assert one.preview_id == two.preview_id
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_client_request_id_cannot_be_reused_for_different_imported_text(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="request-bound-to-input",
        )
    )

    with pytest.raises(ProfileImportStalePreviewError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes coffee.",
                client_request_id="request-bound-to-input",
            )
        )


@pytest.mark.asyncio
async def test_same_raw_reanalyzes_after_an_unrelated_baseline_change(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            ),
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\nUse lists.\n",
            ),
        ],
        calls,
    )
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="first-baseline",
    )
    first = await service.preview(request)
    await service.apply(first.preview_id, first.candidate_hash, "first-baseline-apply")
    _paths(tmp_path).memory_path.write_text(
        "# Memory\nBe concise.\nUse lists.\n",
        encoding="utf-8",
    )

    second = await service.preview(
        request.model_copy(update={"client_request_id": "changed-baseline"})
    )

    assert second.preview_id != first.preview_id
    assert len(calls) == 2
    assert calls[1].identity == calls[0].identity


@pytest.mark.asyncio
async def test_no_change_receipt_does_not_skip_reanalysis_after_baseline_change(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
            ),
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\nUse lists.\n",
            ),
        ],
        calls,
    )
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="no-change-baseline",
    )
    first = await service.preview(request)
    assert first.no_changes is True
    applied = await service.apply(
        first.preview_id,
        first.candidate_hash,
        "no-change-baseline-apply",
    )
    assert applied.status == "noChanges"
    assert applied.index_status == "ready"
    _paths(tmp_path).memory_path.write_text(
        "# Memory\nBe concise.\nUse lists.\n",
        encoding="utf-8",
    )

    second = await service.preview(
        request.model_copy(update={"client_request_id": "no-change-baseline-updated"})
    )

    assert second.preview_id != first.preview_id
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_no_change_apply_recovers_the_same_receipt_after_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                source_excerpt="",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The imported profile is already present.",
            client_request_id="no-change-crash-preview",
        )
    )
    original_update = service.store.update_preview
    update_calls = 0

    def fail_after_receipt(record: object) -> None:
        nonlocal update_calls
        update_calls += 1
        if update_calls == 2:
            raise ProfileImportWriteError("synthetic preview metadata failure")
        original_update(record)  # type: ignore[arg-type]

    monkeypatch.setattr(service.store, "update_preview", fail_after_receipt)
    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "no-change-crash-apply",
        )
    durable = service.store.load_receipt_by_batch(preview.batch_id)
    assert durable is not None

    monkeypatch.setattr(service.store, "update_preview", original_update)
    retried = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "no-change-crash-apply",
    )

    assert retried.status == "alreadyApplied"
    assert retried.receipt_id == durable.receipt_id
    assert service.store.load_receipt(retried.receipt_id).receipt_id == durable.receipt_id


@pytest.mark.asyncio
async def test_discard_finalizes_a_durable_no_change_receipt_after_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                source_excerpt="",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The imported profile is already present.",
            client_request_id="no-change-discard-recovery",
        )
    )
    original_update = service.store.update_preview
    update_calls = 0

    def fail_once_after_receipt(record: object) -> None:
        nonlocal update_calls
        update_calls += 1
        if update_calls == 2:
            raise ProfileImportWriteError("synthetic preview metadata failure")
        original_update(record)  # type: ignore[arg-type]

    monkeypatch.setattr(service.store, "update_preview", fail_once_after_receipt)
    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "no-change-discard-apply",
        )

    durable = service.store.load_receipt_by_batch(preview.batch_id)
    assert durable is not None
    await service.discard(preview.preview_id)

    finalized = service.store.load_preview(preview.preview_id)
    assert finalized.status.value == "applied"
    assert finalized.receipt_id == durable.receipt_id
    assert service.store.load_receipt(durable.receipt_id).receipt_id == durable.receipt_id
    assert not service.store.raw_path(preview.batch_id).exists()


@pytest.mark.asyncio
async def test_discard_forgets_the_entire_unapplied_private_draft(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="discard-private-draft",
    )
    preview = await service.preview(request)
    record = service.store.load_preview(preview.preview_id)
    job = service.store.find_job_by_preview(preview.preview_id)
    assert job is not None
    batch_dir = service.store.batch_dir(preview.batch_id)

    await service.discard(preview.preview_id)

    assert not batch_dir.exists()
    assert lookup_job_agent(_paths(tmp_path).state_dir, job.job_id) is None
    assert lookup_preview_agent(_paths(tmp_path).state_dir, preview.preview_id) is None
    assert (
        service.store.find_preview_by_request(
            f"import:{request.client_request_id}"
        )
        is None
    )
    assert service.store.find_preview_by_reuse_key(record.reuse_key) is None


@pytest.mark.asyncio
async def test_expired_preview_cleanup_forgets_candidates_and_indexes(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="expired-private-draft",
    )
    preview = await service.preview(request)
    record = service.store.load_preview(preview.preview_id)
    record.expires_at = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    service.store.update_preview(record)

    await service.info()

    assert not service.store.batch_dir(preview.batch_id).exists()
    assert lookup_preview_agent(_paths(tmp_path).state_dir, preview.preview_id) is None
    assert (
        service.store.find_preview_by_request(
            f"import:{request.client_request_id}"
        )
        is None
    )
    assert service.store.find_preview_by_reuse_key(record.reuse_key) is None


@pytest.mark.asyncio
async def test_expired_preview_cleanup_preserves_a_recoverable_failed_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="expired-recovery-draft",
        )
    )
    original_replace = profile_import_transaction._replace_target
    original_rollback = profile_import_transaction._rollback_plan

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise ProfileImportWriteError("synthetic publication failure")

    def fail_rollback(*_args: object, **_kwargs: object) -> None:
        raise ProfileImportWriteError("synthetic rollback failure")

    monkeypatch.setattr(profile_import_transaction, "_replace_target", fail_replace)
    monkeypatch.setattr(profile_import_transaction, "_rollback_plan", fail_rollback)
    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "expired-recovery-apply",
        )
    journal = service.store.load_journal(preview.batch_id)
    assert journal is not None
    assert journal.phase == "rollback_failed"
    record = service.store.load_preview(preview.preview_id)
    record.expires_at = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    service.store.update_preview(record)

    await service.info()

    assert service.store.batch_dir(preview.batch_id).exists()
    assert service.store.load_journal(preview.batch_id) is not None

    monkeypatch.setattr(profile_import_transaction, "_replace_target", original_replace)
    monkeypatch.setattr(profile_import_transaction, "_rollback_plan", original_rollback)
    await service.recover()


@pytest.mark.asyncio
async def test_reimport_after_undo_reanalyzes_instead_of_reusing_applied_preview(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    response = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    service = _service(tmp_path, [response, response], calls)
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea.",
        client_request_id="first-import",
    )
    first = await service.preview(request)
    applied = await service.apply(first.preview_id, first.candidate_hash, "first-apply")
    await service.undo(applied.receipt_id, "first-undo")

    second = await service.preview(
        request.model_copy(update={"client_request_id": "reimport-after-undo"})
    )

    assert second.preview_id != first.preview_id
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_apply_rejects_a_stale_preview_without_overwriting_user_edit(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="stale-request",
        )
    )
    _paths(tmp_path).user_path.write_text("# User\nAlice\nLater edit.\n", encoding="utf-8")

    with pytest.raises(ProfileImportStalePreviewError):
        await service.apply(preview.preview_id, preview.candidate_hash, "apply-stale")

    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nLater edit.\n"
    )


@pytest.mark.asyncio
async def test_old_incomplete_import_preview_loads_and_discards_but_cannot_apply(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="old-incomplete-import-preview",
        )
    )
    record = service.store.load_preview(preview.preview_id)
    record.prompt_version = "profile-fusion-v2"
    record.fusion_output = record.fusion_output.model_copy(
        update={
            "decisions": [
                record.fusion_output.decisions[0].model_copy(
                    update={"target": DecisionTarget.IMPORT}
                )
            ]
        }
    )
    service.store.update_preview(record)

    loaded = service.store.load_preview(preview.preview_id)
    assert loaded.prompt_version == "profile-fusion-v2"
    assert loaded.fusion_output.candidate.import_md is None
    assert loaded.fusion_output.decisions[0].target is DecisionTarget.IMPORT
    assert all(plan.target is not DecisionTarget.IMPORT for plan in loaded.files)

    with pytest.raises(
        ProfileImportStalePreviewError,
        match="preview is incomplete; generate a new preview",
    ):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "must-not-bind",
        )

    rejected = service.store.load_preview(preview.preview_id)
    assert rejected.idempotency_key_hash is None
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"

    await service.discard(preview.preview_id)

    assert lookup_preview_agent(_paths(tmp_path).state_dir, preview.preview_id) is None


@pytest.mark.asyncio
async def test_old_incomplete_applied_preview_remains_idempotently_queryable(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="old-incomplete-applied-preview",
        )
    )
    first = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "old-incomplete-applied-key",
    )
    record = service.store.load_preview(preview.preview_id)
    record.prompt_version = "profile-fusion-v2"
    record.fusion_output = record.fusion_output.model_copy(
        update={
            "decisions": [
                record.fusion_output.decisions[0].model_copy(
                    update={"target": DecisionTarget.IMPORT}
                )
            ]
        }
    )
    service.store.update_preview(record)

    repeated = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "old-incomplete-applied-key",
    )

    assert repeated.status == "alreadyApplied"
    assert repeated.receipt_id == first.receipt_id


@pytest.mark.asyncio
async def test_old_import_preview_with_unclaimed_import_plan_cannot_apply(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported="# Projects\n- Built a tea tracker.\n",
                source_excerpt="built a tea tracker",
                decision_target="IMPORT",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user built a tea tracker.",
            client_request_id="old-unclaimed-import-plan",
        )
    )
    record = service.store.load_preview(preview.preview_id)
    record.prompt_version = "profile-fusion-v2"
    record.fusion_output = record.fusion_output.model_copy(update={"decisions": []})
    service.store.update_preview(record)

    with pytest.raises(
        ProfileImportStalePreviewError,
        match="preview is incomplete; generate a new preview",
    ):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "must-not-apply-unclaimed-import-plan",
        )

    assert list(_paths(tmp_path).imports_dir.glob("*.md")) == []


@pytest.mark.asyncio
async def test_stale_undo_returns_model_preview_and_preserves_later_changes(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            ),
            _fusion_json(
                user="# User\nAlice\nLater edit.\n",
                memory="# Memory\nBe concise.\n",
                source_excerpt="",
            ),
        ],
        calls,
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="import-for-stale-undo",
        )
    )
    applied = await service.apply(preview.preview_id, preview.candidate_hash, "apply-import")
    _paths(tmp_path).user_path.write_text(
        "# User\nAlice\nLikes tea.\nLater edit.\n",
        encoding="utf-8",
    )

    undo = await service.undo(applied.receipt_id, "stale-undo-request")

    assert undo.status == "reviewRequired"
    assert undo.preview is not None
    assert len(calls) == 2
    assert calls[1].identity == calls[0].identity
    assert calls[1].system_prompt != calls[0].system_prompt

    undo_applied = await service.apply(
        undo.preview.preview_id,
        undo.preview.candidate_hash,
        "apply-stale-undo",
    )
    assert undo_applied.status == "applied"
    repeated_undo_apply = await service.apply(
        undo.preview.preview_id,
        undo.preview.candidate_hash,
        "apply-stale-undo",
    )
    assert repeated_undo_apply.status == "alreadyApplied"
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nLater edit.\n"
    )


@pytest.mark.asyncio
async def test_stale_undo_allows_nonempty_import_candidate_with_no_decisions(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    original_import = "# Projects\n- Built a tea tracker.\n"
    later_import = "# Projects\n- Later independent note.\n"
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported=original_import,
                source_excerpt="built a tea tracker",
                decision_target="IMPORT",
            ),
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported=later_import,
                source_excerpt="",
            ),
        ],
        calls,
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user built a tea tracker.",
            client_request_id="import-before-nonempty-stale-undo",
        )
    )
    applied = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "apply-import-before-nonempty-stale-undo",
    )
    import_path = _paths(tmp_path).imports_dir / f"{preview.batch_id}.md"
    import_path.write_text(original_import + "- Later independent note.\n", encoding="utf-8")

    undo = await service.undo(applied.receipt_id, "nonempty-stale-undo")

    assert undo.status == "reviewRequired"
    assert undo.preview is not None
    assert len(calls) == 2
    assert {item.target.value for item in undo.preview.files} == {"IMPORT"}

    undo_applied = await service.apply(
        undo.preview.preview_id,
        undo.preview.candidate_hash,
        "apply-nonempty-stale-undo",
    )
    assert undo_applied.status == "applied"
    assert import_path.read_text(encoding="utf-8") == later_import


@pytest.mark.asyncio
async def test_stale_undo_invalid_output_is_not_automatically_retried(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    valid_import = _fusion_json(
        user="# User\nAlice\nLikes tea.\n",
        memory="# Memory\nBe concise.\n",
    )
    service = _service(tmp_path, [valid_import, "bad", valid_import], calls)
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="import-before-invalid-undo",
        )
    )
    applied = await service.apply(preview.preview_id, preview.candidate_hash, "apply-import")
    _paths(tmp_path).user_path.write_text(
        "# User\nAlice\nLikes tea.\nLater edit.\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.undo(applied.receipt_id, "invalid-stale-undo")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_raw_limit_and_symbolic_link_target_fail_closed(tmp_path: Path) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [],
        calls,
        quotas=ProfileImportQuotas(max_raw_bytes=8),
    )
    with pytest.raises(ProfileImportInputTooLargeError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="longer than eight bytes",
                client_request_id="too-large",
            )
        )
    assert calls == []

    if not hasattr(Path, "symlink_to"):
        return
    paths = _paths(tmp_path)
    paths.user_path.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        paths.user_path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    guarded = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    with pytest.raises(ProfileImportWriteError):
        await guarded.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id="symlink",
            )
        )
    assert outside.read_text(encoding="utf-8") == "outside"


def test_handle_bound_reads_preserve_mode_mtime_and_history_order(tmp_path: Path) -> None:
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    if os.name != "nt":
        paths.user_path.chmod(0o640)
    expected_mode = stat.S_IMODE(paths.user_path.stat().st_mode)

    exists, content, mode = profile_import_files.read_text_image(
        paths.agent_workspace_dir,
        paths.user_path,
    )

    assert exists is True
    assert content.encode("utf-8") == paths.user_path.read_bytes()
    assert mode == expected_mode

    paths.imports_dir.mkdir(parents=True)
    older = paths.imports_dir / "older.md"
    newer = paths.imports_dir / "newer.md"
    older.write_text("older", encoding="utf-8")
    newer.write_text("newer", encoding="utf-8")
    os.utime(older, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(newer, ns=(1_800_000_000_000_000_000,) * 2)

    history, digest = profile_import_files.history_snapshot(paths)

    assert history == [
        {"name": "newer.md", "content": "newer"},
        {"name": "older.md", "content": "older"},
    ]
    assert digest == profile_import_files.stable_hash(
        (
            "newer.md",
            profile_import_files.image_hash(exists=True, content="newer"),
            "older.md",
            profile_import_files.image_hash(exists=True, content="older"),
        )
    )


def test_profile_read_rejects_leaf_swap_between_inspection_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("POSIX openat/O_NOFOLLOW contract")
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    outside = tmp_path / "outside-user.md"
    outside.write_text("outside secret", encoding="utf-8")
    original_open = profile_import_io.os.open
    swapped = False

    def swap_leaf_before_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "USER.md" and dir_fd is not None and not swapped:
            swapped = True
            paths.user_path.unlink()
            paths.user_path.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profile_import_io.os, "open", swap_leaf_before_open)

    with pytest.raises(ProfileImportWriteError):
        profile_import_files.read_text_image(
            paths.agent_workspace_dir,
            paths.user_path,
        )

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "outside secret"


def test_history_snapshot_rejects_leaf_swap_between_inspection_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("POSIX openat/O_NOFOLLOW contract")
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    paths.imports_dir.mkdir(parents=True)
    imported = paths.imports_dir / "first.md"
    imported.write_text("safe history", encoding="utf-8")
    outside = tmp_path / "outside-history.md"
    outside.write_text("outside secret", encoding="utf-8")
    original_open = profile_import_io.os.open
    swapped = False

    def swap_leaf_before_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "first.md" and dir_fd is not None and not swapped:
            swapped = True
            imported.unlink()
            imported.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profile_import_io.os, "open", swap_leaf_before_open)

    with pytest.raises(ProfileImportWriteError):
        profile_import_files.history_snapshot(paths)

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "outside secret"


def test_history_snapshot_rejects_intermediate_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("POSIX openat/O_NOFOLLOW contract")
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    paths.imports_dir.mkdir(parents=True)
    (paths.imports_dir / "safe.md").write_text("safe history", encoding="utf-8")
    outside = tmp_path / "outside-imports"
    outside.mkdir()
    (outside / "secret.md").write_text("outside secret", encoding="utf-8")
    parked = paths.imports_dir.with_name("imports-before-swap")
    original_open = profile_import_io.os.open
    swapped = False

    def swap_directory_before_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "imports" and dir_fd is not None and not swapped:
            swapped = True
            paths.imports_dir.rename(parked)
            paths.imports_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profile_import_io.os, "open", swap_directory_before_open)

    with pytest.raises(ProfileImportWriteError):
        profile_import_files.history_snapshot(paths)

    assert swapped is True
    assert (outside / "secret.md").read_text(encoding="utf-8") == "outside secret"


def test_profile_read_rejects_fixed_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("POSIX openat/O_NOFOLLOW contract")
    _prepare_baseline(tmp_path)
    paths = _paths(tmp_path)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "USER.md").write_text("outside secret", encoding="utf-8")
    parked = paths.agent_workspace_dir.with_name("workspace-before-swap")
    original_open = profile_import_io.os.open
    swapped = False

    def swap_root_before_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == paths.agent_workspace_dir.name and dir_fd is not None and not swapped:
            swapped = True
            paths.agent_workspace_dir.rename(parked)
            paths.agent_workspace_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(profile_import_io.os, "open", swap_root_before_open)

    with pytest.raises(ProfileImportWriteError):
        profile_import_files.read_text_image(
            paths.agent_workspace_dir,
            paths.user_path,
        )

    assert swapped is True
    assert (outside / "USER.md").read_text(encoding="utf-8") == "outside secret"


@pytest.mark.asyncio
async def test_quota_counts_all_existing_memory_markdown_recursively(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    dated = _paths(tmp_path).memory_workspace_dir / "memory" / "2026" / "07"
    dated.mkdir(parents=True)
    (dated / "28.md").write_text("existing dated memory", encoding="utf-8")
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\n",
                memory="# Memory\nBe concise.\n",
                imported="# Project\n[unknown] - A project.\n",
                import_source_excerpt="described a project",
            )
        ],
        calls,
        quotas=ProfileImportQuotas(max_files=2),
    )

    with pytest.raises(ProfileImportInvalidOutputError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea and described a project.",
                client_request_id="quota-all-memory-files",
            )
        )


@pytest.mark.asyncio
async def test_model_request_budget_rejects_complete_baseline_before_calling_model(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [],
        calls,
        quotas=ProfileImportQuotas(max_request_tokens=10),
    )

    with pytest.raises(ProfileImportInputTooLargeError):
        await service.preview(
            ProfileImportPreviewRequest(
                raw_text="The user likes tea.",
                client_request_id="context-window-too-small",
            )
        )

    assert calls == []
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )


@pytest.mark.asyncio
async def test_multi_file_publish_failure_rolls_back_and_same_apply_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\nUse bullets.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea and prefers bullets.",
            client_request_id="rollback-preview",
        )
    )
    original_replace = profile_import_transaction._replace_target
    replace_calls = 0

    def fail_second_replace(
        paths: ProfileImportPaths,
        plan: object,
        *,
        transaction_id: str = "",
        checkpoint_publication: Callable[[object], None] | None = None,
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise ProfileImportWriteError("synthetic second-file failure")
        original_replace(  # type: ignore[arg-type]
            paths,
            plan,
            transaction_id=transaction_id,
            checkpoint_publication=checkpoint_publication,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        profile_import_transaction,
        "_replace_target",
        fail_second_replace,
    )
    with pytest.raises(ProfileImportWriteError):
        await service.apply(preview.preview_id, preview.candidate_hash, "rollback-apply")

    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == "# User\nAlice\n"
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )

    monkeypatch.setattr(profile_import_transaction, "_replace_target", original_replace)
    retried = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "rollback-apply",
    )
    assert retried.status == "applied"
    assert "Likes tea." in _paths(tmp_path).user_path.read_text(encoding="utf-8")
    assert "Use bullets." in _paths(tmp_path).memory_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_never_overwrites_an_edit_after_the_initial_baseline_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="publication-cas-preview",
        )
    )
    original_replace = profile_import_transaction._replace_target
    edited = False

    def edit_before_publication(
        paths: ProfileImportPaths,
        plan: object,
        *,
        transaction_id: str = "",
        checkpoint_publication: Callable[[object], None] | None = None,
    ) -> None:
        nonlocal edited
        if not edited:
            edited = True
            paths.user_path.write_text(
                "# User\nAlice\nConcurrent local edit.\n",
                encoding="utf-8",
            )
        original_replace(  # type: ignore[arg-type]
            paths,
            plan,
            transaction_id=transaction_id,
            checkpoint_publication=checkpoint_publication,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        profile_import_transaction,
        "_replace_target",
        edit_before_publication,
    )

    with pytest.raises(ProfileImportWriteError):
        await service.apply(preview.preview_id, preview.candidate_hash, "publication-cas")

    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nConcurrent local edit.\n"
    )


@pytest.mark.asyncio
async def test_final_publication_window_parks_and_restores_a_concurrent_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="native-publication-cas-preview",
        )
    )
    original_move = profile_import_transaction.native_move_no_replace
    injected = False

    def edit_at_native_park(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal injected
        if (
            not injected
            and source == _paths(tmp_path).user_path
            and destination.name.endswith(".before")
        ):
            injected = True
            source.write_text(
                "# User\nAlice\nConcurrent edit in final CAS window.\n",
                encoding="utf-8",
            )
        original_move(source, destination, **kwargs)

    monkeypatch.setattr(
        profile_import_transaction,
        "native_move_no_replace",
        edit_at_native_park,
    )

    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "native-publication-cas",
        )

    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nConcurrent edit in final CAS window.\n"
    )


@pytest.mark.asyncio
async def test_failed_publication_never_removes_a_same_content_external_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="same-content-publication-conflict",
        )
    )
    original_move = profile_import_transaction.native_move_no_replace
    external_inode = 0

    def recreate_same_candidate_before_publish(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        nonlocal external_inode
        if (
            source.name.endswith(".tmp")
            and destination == _paths(tmp_path).user_path
        ):
            destination.write_bytes(source.read_bytes())
            external_inode = int(destination.stat().st_ino)
        original_move(source, destination, **kwargs)

    monkeypatch.setattr(
        profile_import_transaction,
        "native_move_no_replace",
        recreate_same_candidate_before_publish,
    )

    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "same-content-publication-conflict",
        )

    assert external_inode
    assert int(_paths(tmp_path).user_path.stat().st_ino) == external_inode
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nLikes tea.\n"
    )


@pytest.mark.asyncio
async def test_rollback_restores_a_crash_gap_only_when_the_candidate_is_still_owned(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="parked-gap-preview",
        )
    )
    record = service.store.load_preview(preview.preview_id)
    plan = next(item for item in record.files if item.target.value == "USER")
    transaction_id = "f" * 32
    target = _paths(tmp_path).user_path
    backup = profile_import_transaction._transaction_backup(
        target,
        transaction_id,
    )
    temporary = profile_import_transaction._transaction_temporary(
        target,
        transaction_id,
    )
    temporary.write_bytes(plan.after_content.encode("utf-8"))
    profile_import_transaction.native_move_no_replace(target, backup)

    profile_import_transaction._rollback_plan(
        _paths(tmp_path),
        plan,
        transaction_id=transaction_id,
    )

    assert target.read_bytes() == plan.before_content.encode("utf-8")
    assert not backup.exists()
    assert not temporary.exists()

    external_delete_id = "e" * 32
    external_backup = profile_import_transaction._transaction_backup(
        target,
        external_delete_id,
    )
    profile_import_transaction._replace_target(
        _paths(tmp_path),
        plan,
        transaction_id=external_delete_id,
    )
    target.unlink()
    with pytest.raises(ProfileImportStalePreviewError):
        profile_import_transaction._rollback_plan(
            _paths(tmp_path),
            plan,
            transaction_id=external_delete_id,
        )
    assert not target.exists()
    profile_import_transaction.native_move_no_replace(external_backup, target)


@pytest.mark.asyncio
async def test_rollback_never_overwrites_an_edit_after_its_own_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\nUse lists.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea and prefers lists.",
            client_request_id="rollback-cas-preview",
        )
    )
    original_replace = profile_import_transaction._replace_target

    def fail_after_later_edit(
        paths: ProfileImportPaths,
        plan: object,
        *,
        transaction_id: str = "",
        checkpoint_publication: Callable[[object], None] | None = None,
    ) -> None:
        target = getattr(getattr(plan, "target", None), "value", "")
        if target == "MEMORY":
            raise ProfileImportWriteError("synthetic second-target failure")
        original_replace(  # type: ignore[arg-type]
            paths,
            plan,
            transaction_id=transaction_id,
            checkpoint_publication=checkpoint_publication,  # type: ignore[arg-type]
        )
        paths.user_path.write_text(
            "# User\nAlice\nConcurrent edit after publication.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        profile_import_transaction,
        "_replace_target",
        fail_after_later_edit,
    )

    with pytest.raises(ProfileImportWriteError):
        await service.apply(preview.preview_id, preview.candidate_hash, "rollback-cas")

    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nConcurrent edit after publication.\n"
    )
    assert _paths(tmp_path).memory_path.read_text(encoding="utf-8") == (
        "# Memory\nBe concise.\n"
    )


@pytest.mark.asyncio
async def test_rollback_uses_persisted_identity_for_same_content_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\nUse lists.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea and prefers lists.",
            client_request_id="same-content-recreation-preview",
        )
    )
    original_replace = profile_import_transaction._replace_target
    external_inode = 0

    def recreate_after_publication(
        paths: ProfileImportPaths,
        plan: object,
        *,
        transaction_id: str = "",
        checkpoint_publication: Callable[[object], None] | None = None,
    ) -> None:
        nonlocal external_inode
        target_name = getattr(getattr(plan, "target", None), "value", "")
        if target_name == "MEMORY":
            raise ProfileImportWriteError("synthetic second-target failure")
        original_replace(  # type: ignore[arg-type]
            paths,
            plan,
            transaction_id=transaction_id,
            checkpoint_publication=checkpoint_publication,  # type: ignore[arg-type]
        )
        content = paths.user_path.read_bytes()
        paths.user_path.unlink()
        paths.user_path.write_bytes(content)
        external_inode = int(paths.user_path.stat().st_ino)

    monkeypatch.setattr(
        profile_import_transaction,
        "_replace_target",
        recreate_after_publication,
    )

    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "same-content-recreation-apply",
        )

    assert external_inode
    assert int(_paths(tmp_path).user_path.stat().st_ino) == external_inode
    assert _paths(tmp_path).user_path.read_text(encoding="utf-8") == (
        "# User\nAlice\nLikes tea.\n"
    )


@pytest.mark.asyncio
async def test_published_files_finalize_from_journal_after_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="published-recovery-preview",
        )
    )
    raw_path = _paths(tmp_path).agent_import_state_dir / preview.batch_id / "raw.txt"
    original_update = service.store.update_preview
    update_calls = 0

    def fail_transaction_metadata(record: object) -> None:
        nonlocal update_calls
        update_calls += 1
        if update_calls == 2:
            raise ProfileImportWriteError("synthetic metadata failure")
        original_update(record)  # type: ignore[arg-type]

    monkeypatch.setattr(service.store, "update_preview", fail_transaction_metadata)
    with pytest.raises(ProfileImportWriteError):
        await service.apply(preview.preview_id, preview.candidate_hash, "published-apply")

    assert "Likes tea." in _paths(tmp_path).user_path.read_text(encoding="utf-8")
    assert raw_path.exists()

    monkeypatch.setattr(service.store, "update_preview", original_update)
    recovered = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "published-apply",
    )
    assert recovered.status == "alreadyApplied"
    assert not raw_path.exists()


@pytest.mark.asyncio
async def test_committed_recovery_repairs_missing_applied_context_for_idempotent_reimport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_baseline(tmp_path)
    calls: list[FusionModelRequest] = []
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
                imported="# Project\n[unknown] - Built a tea tracker.\n",
                import_source_excerpt="built a tea tracker",
            )
        ],
        calls,
    )
    request = ProfileImportPreviewRequest(
        raw_text="The user likes tea and built a tea tracker.",
        client_request_id="committed-context-preview",
    )
    preview = await service.preview(request)
    original_save_receipt = service.store.save_receipt

    def fail_applied_context_write(receipt: object) -> None:
        if getattr(receipt, "applied_user_hash", ""):
            raise ProfileImportWriteError("synthetic applied-context metadata failure")
        original_save_receipt(receipt)  # type: ignore[arg-type]

    monkeypatch.setattr(
        service.store,
        "save_receipt",
        fail_applied_context_write,
    )
    with pytest.raises(ProfileImportWriteError):
        await service.apply(
            preview.preview_id,
            preview.candidate_hash,
            "committed-context-apply",
        )

    durable = service.store.load_receipt_by_batch(preview.batch_id)
    assert durable is not None
    assert durable.applied_user_hash == ""
    assert service.store.load_journal(preview.batch_id).phase == "committed"  # type: ignore[union-attr]

    monkeypatch.setattr(service.store, "save_receipt", original_save_receipt)
    await service.recover()

    repaired = service.store.load_receipt(durable.receipt_id)
    assert repaired.applied_user_hash
    assert repaired.applied_memory_hash
    assert repaired.applied_history_hash
    repeated_preview = await service.preview(
        request.model_copy(update={"client_request_id": "committed-context-reimport"})
    )
    assert repeated_preview.no_changes is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_applied_context_is_not_repaired_across_a_later_history_edit(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
                imported="# Project\n[unknown] - Built a tea tracker.\n",
                import_source_excerpt="built a tea tracker",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea and built a tea tracker.",
            client_request_id="missing-context-preview",
        )
    )
    applied = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "missing-context-apply",
    )
    receipt = service.store.load_receipt(applied.receipt_id)
    service.store.save_receipt(
        receipt.model_copy(
            update={
                "applied_user_hash": "",
                "applied_memory_hash": "",
                "applied_history_hash": "",
            }
        )
    )
    service.store.journal_path(preview.batch_id).unlink()
    later = _paths(tmp_path).imports_dir / "later.md"
    later.write_text("Later local history", encoding="utf-8")

    repeated = await service.apply(
        preview.preview_id,
        preview.candidate_hash,
        "missing-context-apply",
    )

    assert repeated.status == "alreadyApplied"
    unrepaired = service.store.load_receipt(applied.receipt_id)
    assert unrepaired.applied_user_hash == ""
    assert unrepaired.applied_memory_hash == ""
    assert unrepaired.applied_history_hash == ""


@pytest.mark.asyncio
async def test_apply_preserves_bom_crlf_trailing_newline_and_existing_mode(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    path = _paths(tmp_path).user_path
    path.write_bytes(b"\xef\xbb\xbf# User\r\nAlice")
    path.chmod(0o640)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="format-preservation-preview",
        )
    )
    await service.apply(preview.preview_id, preview.candidate_hash, "format-apply")

    assert path.read_bytes() == b"\xef\xbb\xbf# User\r\nAlice\r\nLikes tea."
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640


def test_replacement_uses_the_dominant_existing_newline_style() -> None:
    before = "# User\nAlice\r\nLikes tea.\nUses lists.\n"
    candidate = "# User\nAlice\nLikes tea.\nUses lists.\nNew fact.\n"

    result = profile_import_files.canonicalize_replacement(
        before,
        candidate,
        existed=True,
    )

    assert result == "# User\nAlice\nLikes tea.\nUses lists.\nNew fact.\n"


@pytest.mark.asyncio
async def test_apply_supports_storage_paths_longer_than_legacy_windows_limits(
    tmp_path: Path,
) -> None:
    deep_root = tmp_path.joinpath(
        *(f"profile-import-segment-{index:02d}" for index in range(14))
    )
    _prepare_baseline(deep_root)
    service = _service(
        deep_root,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="long-path-preview",
        )
    )
    await service.apply(preview.preview_id, preview.candidate_hash, "long-path-apply")

    assert "Likes tea." in _paths(deep_root).user_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preview_cannot_apply_to_different_identical_storage_roots(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    first = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await first.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="root-bound-preview",
        )
    )

    other_workspace = tmp_path / "other-workspace"
    other_memory = tmp_path / "other-memory"
    other_workspace.mkdir()
    other_memory.mkdir()
    (other_workspace / "USER.md").write_text("# User\nAlice\n", encoding="utf-8")
    (other_memory / "MEMORY.md").write_text(
        "# Memory\nBe concise.\n",
        encoding="utf-8",
    )
    rebound = ProfileImportService(
        ProfileImportPaths(
            agent_id="main",
            agent_workspace_dir=other_workspace,
            memory_workspace_dir=other_memory,
            state_dir=_paths(tmp_path).state_dir,
        ),
        ModelIdentity(provider="configured-provider", model="configured-model"),
        None,
        profile_lock_factory=lambda _path: contextlib.nullcontext(),
    )

    with pytest.raises(ProfileImportStalePreviewError):
        await rebound.apply(
            preview.preview_id,
            preview.candidate_hash,
            "root-bound-apply",
        )

    assert (other_workspace / "USER.md").read_text(encoding="utf-8") == (
        "# User\nAlice\n"
    )


@pytest.mark.asyncio
async def test_recovery_conflict_stays_fail_closed(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="recovery-conflict-preview",
        )
    )
    await service.apply(preview.preview_id, preview.candidate_hash, "conflict-apply")
    journal = service.store.load_journal(preview.batch_id)
    assert journal is not None
    journal.phase = "published"
    service.store.save_journal(journal)
    _paths(tmp_path).user_path.write_text(
        "# User\nAlice\nUnrelated later change.\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileImportWriteError):
        await service.recover()
    conflicted = service.store.load_journal(preview.batch_id)
    assert conflicted is not None
    assert conflicted.phase == "recovery_conflict"
    with pytest.raises(ProfileImportWriteError):
        await service.recover()


@pytest.mark.asyncio
async def test_maintenance_removes_expired_raw_and_committed_transaction_temporaries(
    tmp_path: Path,
) -> None:
    _prepare_baseline(tmp_path)
    service = _service(
        tmp_path,
        [
            _fusion_json(
                user="# User\nAlice\nLikes tea.\n",
                memory="# Memory\nBe concise.\n",
            )
        ],
        [],
    )
    preview = await service.preview(
        ProfileImportPreviewRequest(
            raw_text="The user likes tea.",
            client_request_id="temporary-cleanup-preview",
        )
    )
    await service.apply(preview.preview_id, preview.candidate_hash, "temporary-cleanup")
    journal = service.store.load_journal(preview.batch_id)
    assert journal is not None
    transaction_temporary = profile_import_transaction._transaction_temporary(
        _paths(tmp_path).user_path,
        journal.transaction_id,
    )
    transaction_temporary.write_text("orphaned candidate", encoding="utf-8")

    orphan_batch = f"{98:032x}"
    orphan_dir = service.store.batch_dir(orphan_batch)
    orphan_dir.mkdir(parents=True)
    raw_temporary = orphan_dir / ".raw.txt.profile-import-orphan.tmp"
    raw_temporary.write_text("sensitive orphan", encoding="utf-8")
    locator_temporary = (
        _paths(tmp_path).state_dir
        / "profile-imports"
        / "_locators"
        / "previews"
        / ".preview.json.profile-import-orphan.tmp"
    )
    locator_temporary.parent.mkdir(parents=True, exist_ok=True)
    locator_temporary.write_text("sensitive locator draft", encoding="utf-8")
    expired_timestamp = datetime(2026, 7, 27, 10, 0, tzinfo=UTC).timestamp()
    os.utime(raw_temporary, (expired_timestamp, expired_timestamp))
    os.utime(locator_temporary, (expired_timestamp, expired_timestamp))

    recovered = await service.recover()
    await service.info()

    assert recovered == []
    assert not transaction_temporary.exists()
    assert not raw_temporary.exists()
    assert not locator_temporary.exists()
