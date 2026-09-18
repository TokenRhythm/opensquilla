from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from opensquilla.attachment_workspace import AttachmentWorkspaceMaterializer
from opensquilla.sandbox import filesystem_worker
from opensquilla.sandbox.operation_runtime import SandboxOperationResult
from opensquilla.tools.builtin import filesystem, media
from opensquilla.tools.document_readers import MAX_OUTPUT_CHARS, read_document
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context


def _docx(path: Path, paragraphs: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
            + "</w:body></w:document>",
        )


def _pdf(path: Path, count: int = 3) -> None:
    document = canvas.Canvas(str(path))
    for index in range(1, count + 1):
        if index != 2:
            document.drawString(50, 700, f"Document page {index}")
        document.showPage()
    document.save()


def test_materializer_preserves_conflicting_original(tmp_path: Path) -> None:
    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    args = {"name": "notes.txt", "mime": "text/plain", "session_id": "session-a"}
    first = materializer.materialize_bytes(b"original", **args)
    assert first.rel_path
    target = tmp_path / first.rel_path
    target.write_bytes(b"external edit")
    second = materializer.materialize_bytes(b"original", **args)
    assert not second.available
    assert "conflict" in (second.error or "")
    assert target.read_bytes() == b"external edit"


@pytest.mark.asyncio
async def test_working_file_survives_rematerialization_and_new_context(tmp_path: Path) -> None:
    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    args = {"name": "notes.txt", "mime": "text/plain", "session_id": "session-a"}
    original = materializer.materialize_bytes(b"alpha\n", **args)
    assert original.rel_path
    saved = []
    context = ToolContext(
        workspace_dir=str(tmp_path), artifact_session_id="session-a", run_mode="full"
    )

    async def persist() -> None:
        saved.append(json.loads(json.dumps(context.attachment_working_files)))

    context.persist_attachment_working_files = persist
    token = current_tool_context.set(context)
    try:
        await filesystem.edit_file(original.rel_path, "alpha", "beta")
        assert (tmp_path / original.rel_path).read_text() == "alpha\n"
        entry = context.attachment_working_files[original.rel_path]
        work = tmp_path / entry["path"]
        assert work.read_text() == "beta\n"
        materializer.materialize_bytes(b"alpha\n", **args)
        assert work.read_text() == "beta\n"
        assert "beta" in await filesystem.read_file(original.rel_path)
    finally:
        current_tool_context.reset(token)
    restored = ToolContext(
        workspace_dir=str(tmp_path),
        artifact_session_id="session-a",
        run_mode="full",
        attachment_working_files=saved[-1],
    )
    token = current_tool_context.set(restored)
    try:
        await filesystem.edit_file(original.rel_path, "beta", "gamma")
        assert work.read_text() == "gamma\n"
        work.unlink()
        with pytest.raises(SafeToolError, match="missing"):
            await filesystem.read_file(original.rel_path)
        assert (tmp_path / original.rel_path).read_text() == "alpha\n"
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("collaboration_mode", ["default", "plan"])
async def test_shared_dispatch_preserves_attachment_owner_across_storage_restart(
    tmp_path: Path, collaboration_mode: str,
) -> None:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import ToolCall
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry

    database = str(tmp_path / "attachment-dispatch.db")
    storage = SessionStorage(database)
    await storage.connect()
    source_bytes = b"immutable input\n"
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create("agent:main:attachment-dispatch")
        materializer = AttachmentWorkspaceMaterializer(
            media_root=tmp_path, workspace_dir=tmp_path,
        )
        original = materializer.materialize_bytes(
            source_bytes, name="notes.txt", mime="text/plain", session_id=session.session_id,
        )
        assert original.rel_path
        runner = TurnRunner(provider_selector=None, session_manager=manager)
        context = await runner._with_artifact_context(ToolContext(
            workspace_dir=str(tmp_path), run_mode="full", workspace_strict=True,
            collaboration_mode=collaboration_mode, allowed_tools={"read_file", "edit_file"},
            usage_root_turn_id="synthetic-shared-turn",
        ), session.session_key)
        assert context.usage_root_turn_id == "synthetic-shared-turn"
        result = await build_tool_handler(get_default_registry(), context)(ToolCall(
            tool_use_id="owned-edit", tool_name="edit_file",
            arguments={"path": original.rel_path, "old_text": "immutable", "new_text": "edited"},
        ))
        assert not result.is_error, result.content
        assert (tmp_path / original.rel_path).read_bytes() == source_bytes
        work_record = context.attachment_working_files[original.rel_path]
        work_path = tmp_path / work_record["path"]
        assert work_path.read_text() == "edited input\n"
        assert work_record["session_id"] == session.session_id
    finally:
        await storage.close()

    restored_storage = SessionStorage(database)
    await restored_storage.connect()
    try:
        restored_runner = TurnRunner(
            provider_selector=None, session_manager=SessionManager(restored_storage),
        )
        restored = await restored_runner._with_artifact_context(ToolContext(
            workspace_dir=str(tmp_path), run_mode="full", workspace_strict=True,
            collaboration_mode=collaboration_mode, allowed_tools={"read_file", "edit_file"},
        ), session.session_key)
        assert restored.attachment_working_files[original.rel_path] == work_record
        handler = build_tool_handler(get_default_registry(), restored)
        read = await handler(ToolCall(
            tool_use_id="restored-read", tool_name="read_file",
            arguments={"path": original.rel_path},
        ))
        assert not read.is_error, read.content
        assert "edited input" in read.content

        # Collaboration intent cannot override the current tool denylist.
        restored.denied_tools.add("edit_file")
        denied = await handler(ToolCall(
            tool_use_id="denied-edit", tool_name="edit_file",
            arguments={"path": original.rel_path, "old_text": "edited", "new_text": "denied"},
        ))
        assert denied.is_error
        assert json.loads(denied.content)["error_class"] == "PolicyDenied"
        assert work_path.read_text() == "edited input\n"
        assert (tmp_path / original.rel_path).read_bytes() == source_bytes
    finally:
        await restored_storage.close()


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "edit_source"])
@pytest.mark.parametrize("existing_workfile", [False, True])
@pytest.mark.parametrize("missing_binding", ["epoch", "persistence"])
async def test_managed_attachment_edit_requires_durable_owner_before_mutation(
    tmp_path: Path, tool_name: str, existing_workfile: bool, missing_binding: str,
) -> None:
    from unittest.mock import AsyncMock

    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    original = materializer.materialize_bytes(
        b"alpha\n", name="notes.txt", mime="text/plain", session_id="session-a",
    )
    assert original.rel_path
    source = tmp_path / original.rel_path
    work = source.parent / "working" / source.name
    context = ToolContext(
        workspace_dir=str(tmp_path), artifact_session_id="session-a", run_mode="full",
    )
    token = current_tool_context.set(context)
    try:
        if existing_workfile:
            # An embedded caller without a session manager keeps its existing
            # context-local editing support; it establishes the editable target.
            await filesystem.edit_file(original.rel_path, "alpha", "embedded")
            assert work.read_text() == "embedded\n"
        context.sandbox_session_manager = object()
        context.session_epoch = None if missing_binding == "epoch" else 0
        persist = AsyncMock()
        context.persist_attachment_working_files = (
            persist if missing_binding == "epoch" else None
        )
        before = {path.relative_to(tmp_path): path.read_bytes()
                  for path in tmp_path.rglob("*") if path.is_file()}
        with pytest.raises(SafeToolError, match="current durable session"):
            if tool_name == "write_file":
                await filesystem.write_file(original.rel_path, "replacement\n")
            elif tool_name == "edit_file":
                await filesystem.edit_file(original.rel_path, "alpha", "replacement")
            else:
                await filesystem.edit_source(original.rel_path, "synthetic-revision", [])
        assert {path.relative_to(tmp_path): path.read_bytes()
                for path in tmp_path.rglob("*") if path.is_file()} == before
        persist.assert_not_awaited()
    finally:
        current_tool_context.reset(token)


async def test_managed_lookup_failure_blocks_attachment_workfile_but_allows_ordinary_file(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from opensquilla.engine.runtime import TurnRunner

    source = tmp_path / ".opensquilla/attachments/session-a/original.txt"
    work = source.parent / "working" / source.name
    work.parent.mkdir(parents=True)
    source.write_text("original\n")
    work.write_text("previous edit\n")
    manager = MagicMock()
    manager.get_session = AsyncMock(side_effect=OSError("synthetic session lookup failure"))
    manager.update = AsyncMock()
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    context = await runner._with_artifact_context(
        ToolContext(workspace_dir=str(tmp_path), run_mode="full"), "agent:main:session-a",
    )
    assert context.session_epoch is None
    assert context.persist_attachment_working_files is None
    token = current_tool_context.set(context)
    try:
        for path in (source, work):
            with pytest.raises(SafeToolError, match="current durable session"):
                await filesystem.write_file(str(path), "replacement\n")
        await filesystem.write_file("ordinary.txt", "allowed\n")
    finally:
        current_tool_context.reset(token)
    assert source.read_text() == "original\n"
    assert work.read_text() == "previous edit\n"
    assert (tmp_path / "ordinary.txt").read_text() == "allowed\n"
    manager.update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("collaboration_mode", ["default", "plan"])
@pytest.mark.parametrize("denied", [False, True])
async def test_document_read_runs_inside_filesystem_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collaboration_mode: str,
    denied: bool,
) -> None:
    from opensquilla.engine.types import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry

    target = tmp_path / "sample.docx"
    _docx(target, ["first", "second", "third"])
    calls = []

    async def execute(operation, **kwargs):
        calls.append(operation.kind)
        payload = {"kind": operation.kind, **operation.request.to_payload()}
        return SandboxOperationResult.from_worker_stdout(
            json.dumps(filesystem_worker._run(payload))
        )

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", execute)
    context = ToolContext(
        workspace_dir=str(tmp_path), run_mode="full", collaboration_mode=collaboration_mode,
        denied_tools={"read_file"} if denied else set(),
    )
    output = await build_tool_handler(get_default_registry(), context)(ToolCall(
        tool_use_id="document-read", tool_name="read_file",
        arguments={"path": "sample.docx", "offset": 2, "limit": 1},
    ))
    result = json.loads(output.content)
    if denied:
        assert output.is_error
        assert result["error_class"] == "PolicyDenied"
        assert calls == []
        return
    assert not output.is_error
    assert calls == ["read_file"]
    assert result["unit"] == "paragraph"
    assert result["range"] == [2]
    assert result["units"][0]["text"] == "second"
    assert result["next_offset"] == 3


def test_docx_large_paragraph_has_exact_character_continuation(tmp_path: Path) -> None:
    path = tmp_path / "large.docx"
    content = "x" * (MAX_OUTPUT_CHARS + 7)
    _docx(path, [content, "next"])
    first = read_document(path, limit=1)
    assert first is not None
    assert first["next_offset"] == 1
    assert first["next_character_offset"] == MAX_OUTPUT_CHARS
    second = read_document(
        path, offset=first["next_offset"], character_offset=first["next_character_offset"], limit=1
    )
    assert second is not None
    assert first["units"][0]["text"] + second["units"][0]["text"] == content
    assert second["next_offset"] == 2


def test_pptx_uses_presentation_order_and_skips_unrequested_corrupt_slide(tmp_path: Path) -> None:
    path = tmp_path / "slides.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/presentation.xml",
            "<p:presentation "
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldIdLst><p:sldId r:id="r2"/><p:sldId r:id="r1"/></p:sldIdLst>'
            "</p:presentation>",
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            "<Relationships>"
            '<Relationship Id="r1" Target="slides/slide1.xml"/>'
            '<Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>',
        )
        archive.writestr("ppt/slides/slide2.xml", "invalid xml")
        archive.writestr(
            "ppt/slides/slide1.xml",
            "<a:root "
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:p><a:r><a:t>Second in presentation</a:t></a:r></a:p></a:root>",
        )
    result = read_document(path, offset=2, limit=1)
    assert result and result["units"][0]["text"] == "Second in presentation"


def test_email_message_range_and_headers(tmp_path: Path) -> None:
    path = tmp_path / "mail.mbox"
    path.write_bytes(
        b"From sender@example.test Mon Jan 1 00:00:00 2024\nSubject: First\n\none\n"
        b"From sender@example.test Tue Jan 2 00:00:00 2024\nSubject: Second\n\ntwo\n"
    )
    result = read_document(path, offset=2, limit=1)
    assert result and result["range"] == [2]
    assert "Subject: Second" in result["units"][0]["text"]
    assert "First" not in result["units"][0]["text"]


def test_pdf_extracts_only_requested_pages_and_reports_textless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pdfplumber.page

    target = tmp_path / "pages.pdf"
    _pdf(target, 12)
    extracted = []
    original = pdfplumber.page.Page.extract_text

    def extract(page, *args, **kwargs):
        extracted.append(page.page_number)
        return original(page, *args, **kwargs)

    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", extract)
    result = read_document(target, offset=2, limit=2)
    assert result and result["range"] == [2, 3]
    assert result["textless_pages"] == [2]
    assert result["next_offset"] == 4
    assert extracted == [2, 3]


@pytest.mark.asyncio
async def test_pdf_render_returns_selected_pages_to_current_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "pages.pdf"
    _pdf(target)
    rendered = []

    async def no_executor(*args, **kwargs):
        return None

    def render(path, page):
        from tests.helpers.image_bytes import image_bytes

        rendered.append(page)
        return image_bytes()

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", no_executor)
    monkeypatch.setattr(media, "_render_pdf_page_png", render)
    context = ToolContext(workspace_dir=str(tmp_path))
    token = current_tool_context.set(context)
    try:
        receipt = json.loads(
            await media.pdf("pages.pdf", pages="2-3", render=True, _tool_use_id="pdf1")
        )
    finally:
        current_tool_context.reset(token)
    assert rendered == [2, 3]
    assert receipt["vision_status"] == "loaded"
    assert len(context.tool_result_media["pdf1"]) == 2
    assert receipt["textless_pages"] == [2]


def test_worker_denies_document_before_parser(tmp_path: Path) -> None:
    target = tmp_path / "secret.docx"
    _docx(target, ["not accessible"])
    # The worker's policy payload is exercised by the existing profile suite;
    # here use a denying access seam to assert ordering before format dispatch.
    from unittest.mock import patch

    with patch.object(filesystem_worker, "_enforce_candidate_access", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            filesystem_worker._run({"kind": "read_file", "path": str(target)})


def test_worker_probe_does_not_extract_document(tmp_path: Path) -> None:
    target = tmp_path / "bad.docx"
    target.write_bytes(b"not a zip")
    result = filesystem_worker._run({"kind": "probe_file", "path": str(target)})
    assert result["size"] == 9
    assert result["mtime_ns"] == target.stat().st_mtime_ns


@pytest.mark.asyncio
async def test_fork_copies_edited_bytes_without_sharing_working_file(tmp_path: Path) -> None:
    from opensquilla.attachment_working_files import fork_attachment_working_files

    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    original = materializer.materialize_bytes(
        b"initial",
        name="fork.txt",
        mime="text/plain",
        session_id="parent-session",
    )
    assert original.rel_path
    parent = ToolContext(
        workspace_dir=str(tmp_path),
        artifact_session_id="parent-session",
        run_mode="full",
    )
    token = current_tool_context.set(parent)
    try:
        await filesystem.edit_file(original.rel_path, "initial", "edited")
    finally:
        current_tool_context.reset(token)
    from opensquilla.attachment_fork import copy_fork_working_file

    child = ToolContext(
        workspace_dir=str(tmp_path), artifact_session_id="child-session", run_mode="full",
    )

    async def copy_file(source, target):
        await copy_fork_working_file(source, target, source_context=parent, target_context=child)

    child_map = await fork_attachment_working_files(
        parent.attachment_working_files,
        source_workspace=tmp_path,
        destination_workspace=tmp_path,
        source_session_id="parent-session",
        child_session_id="child-session",
        copy_file=copy_file,
    )
    child_entry = next(iter(child_map.values()))
    child_path = tmp_path / child_entry["path"]
    assert child_path.read_text() == "edited"
    child_path.write_text("child change")
    parent_entry = next(iter(parent.attachment_working_files.values()))
    assert (tmp_path / parent_entry["path"]).read_text() == "edited"
    assert child_entry["sha256"] == parent_entry["sha256"]


@pytest.mark.asyncio
async def test_denied_working_file_write_does_not_create_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    original = materializer.materialize_bytes(
        b"initial",
        name="denied.txt",
        mime="text/plain",
        session_id="session-a",
    )
    assert original.rel_path

    async def denied(*args, **kwargs):
        return {"status": "blocked", "reason": "read_only"}, False, ()

    monkeypatch.setattr(filesystem, "_gate_out_of_workspace_write", denied)
    context = ToolContext(workspace_dir=str(tmp_path), artifact_session_id="session-a")
    token = current_tool_context.set(context)
    try:
        result = json.loads(await filesystem.edit_file(original.rel_path, "initial", "modified"))
    finally:
        current_tool_context.reset(token)
    assert result["status"] == "blocked"
    assert not context.attachment_working_files
    assert not (tmp_path / original.rel_path).parent.joinpath("working").exists()


@pytest.mark.asyncio
async def test_attachment_copy_uses_executor_and_commits_metadata_before_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    original = materializer.materialize_bytes(
        b"initial",
        name="executor.txt",
        mime="text/plain",
        session_id="session-a",
    )
    assert original.rel_path
    calls = []

    async def execute(operation, **kwargs):
        calls.append(operation.kind)
        result = filesystem_worker._run({"kind": operation.kind, **operation.request.to_payload()})
        return SandboxOperationResult.from_worker_stdout(json.dumps(result))

    async def persist():
        calls.append("persist")

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", execute)
    context = ToolContext(
        workspace_dir=str(tmp_path),
        artifact_session_id="session-a",
        persist_attachment_working_files=persist,
    )
    token = current_tool_context.set(context)
    try:
        await filesystem.edit_file(original.rel_path, "initial", "modified")
    finally:
        current_tool_context.reset(token)
    assert calls == ["copy_attachment", "persist", "edit_text"]
    assert (tmp_path / original.rel_path).read_text() == "initial"


@pytest.mark.asyncio
async def test_apply_patch_refuses_immutable_original_even_in_full_mode(tmp_path: Path) -> None:
    from opensquilla.tools.builtin.patch import apply_patch

    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=tmp_path)
    original = materializer.materialize_bytes(
        b"initial\n",
        name="patch.txt",
        mime="text/plain",
        session_id="session-a",
    )
    assert original.rel_path
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path), run_mode="full"))
    try:
        with pytest.raises(SafeToolError, match="immutable attachment"):
            await apply_patch(
                f"*** Begin Patch\n*** Update File: {original.rel_path}\n"
                "@@ -1,1 +1,1 @@\n-initial\n+changed\n"
                "*** End Patch\n"
            )
    finally:
        current_tool_context.reset(token)
    assert (tmp_path / original.rel_path).read_text() == "initial\n"


def test_document_corruption_and_xml_entities_are_explicit_errors(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"not a zip")
    with pytest.raises(SafeToolError, match="corrupt Office"):
        read_document(corrupt)
    entity = tmp_path / "entities.docx"
    with zipfile.ZipFile(entity, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY y "text">]><x/>')
    with pytest.raises(SafeToolError, match="entity declarations"):
        read_document(entity)


def test_worker_copy_rechecks_source_read_permission(tmp_path: Path) -> None:
    import hashlib

    payload = b"test"
    source = tmp_path / f"{hashlib.sha256(payload).hexdigest()[:12]}-source.txt"
    source.write_bytes(payload)
    target = tmp_path / "copy.txt"
    with pytest.raises(PermissionError):
        filesystem_worker._run(
            {
                "kind": "copy_attachment",
                "sourcePath": str(source),
                "path": str(target),
                "permissions": {
                    "filesystem": {
                        "profile": {
                            "defaultAccess": "deny",
                            "deniedReadGlobs": [],
                            "entries": [{"path": str(target), "access": "write"}],
                        }
                    }
                },
            }
        )
    assert not target.exists()


def test_worker_probe_rejects_opened_file_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")
    real_open = os.open
    monkeypatch.setattr(filesystem_worker.os, "open", lambda path, flags: real_open(second, flags))
    with pytest.raises(ValueError, match="mapping changed"):
        filesystem_worker._run({"kind": "probe_file", "path": str(first)})


def test_materializer_records_full_original_hash_in_shared_map(tmp_path: Path) -> None:
    import hashlib

    records = {}
    result = AttachmentWorkspaceMaterializer(
        media_root=tmp_path,
        workspace_dir=tmp_path,
        working_files=records,
    ).materialize_bytes(b"original", name="note.txt", mime="text/plain", session_id="session-a")
    assert result.rel_path
    assert records[result.rel_path]["sha256"] == hashlib.sha256(b"original").hexdigest()
    assert "path" not in records[result.rel_path]


@pytest.mark.asyncio
async def test_pdf_reports_unavailable_vision_without_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "textless.pdf"
    _pdf(target)

    def forbidden(*args):
        pytest.fail("Do not render when the current model cannot inspect images")

    monkeypatch.setattr(media, "_render_pdf_page_png", forbidden)
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(tmp_path),
            run_mode="full",
            image_analysis_target=lambda: None,
        )
    )
    try:
        result = json.loads(
            await media.pdf(str(target), pages="2", render=True, _tool_use_id="pdf")
        )
    finally:
        current_tool_context.reset(token)
    assert result["vision_status"] == "unavailable"
    assert result["textless_pages"] == [2]


def test_outlook_missing_optional_dependency_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    path = tmp_path / "message.msg"
    path.write_bytes(b"dummy message")
    monkeypatch.setitem(sys.modules, "extract_msg", None)
    with pytest.raises(SafeToolError, match=r"opensquilla\[msg\]"):
        read_document(path)


def test_xlsx_reads_selected_sheet_rows_with_continuation(tmp_path: Path) -> None:
    import openpyxl

    path = tmp_path / "workbook.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "First"
    first.append(["unused"])
    second = workbook.create_sheet("Second")
    for index in range(5):
        second.append([index, f"value {index}"])
    workbook.save(path)
    result = read_document(path, sheet="Second", offset=2, limit=2)
    assert result and result["range"] == [2, 3]
    assert json.loads(result["units"][0]["text"]) == ["1", "value 1"]
    assert result["next_offset"] == 4


@pytest.mark.asyncio
async def test_working_file_refuses_workspace_rebinding(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    materializer = AttachmentWorkspaceMaterializer(media_root=tmp_path, workspace_dir=first)
    original = materializer.materialize_bytes(
        b"initial",
        name="binding.txt",
        mime="text/plain",
        session_id="session-a",
    )
    assert original.rel_path
    context = ToolContext(
        workspace_dir=str(first), artifact_session_id="session-a", run_mode="full"
    )
    token = current_tool_context.set(context)
    try:
        await filesystem.edit_file(original.rel_path, "initial", "modified")
        entry = context.attachment_working_files[original.rel_path]
        impostor = second / entry["path"]
        impostor.parent.mkdir(parents=True)
        impostor.write_text("unrelated project")
        context.workspace_dir = str(second)
        with pytest.raises(SafeToolError, match="binding changed"):
            await filesystem.read_file(original.rel_path)
        with pytest.raises(SafeToolError, match="binding changed"):
            await filesystem.read_file(entry["path"])
    finally:
        current_tool_context.reset(token)
    assert impostor.read_text() == "unrelated project"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("rows", [3, 201])
async def test_structured_read_preserves_complete_read_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: bool,
    rows: int,
) -> None:
    target = tmp_path / "records.csv"
    target.write_text("".join(f"{index},value {index}\n" for index in range(rows)))

    async def execute(operation, **kwargs):
        if not worker:
            return None
        result = filesystem_worker._run({"kind": operation.kind, **operation.request.to_payload()})
        return SandboxOperationResult.from_worker_stdout(json.dumps(result))

    monkeypatch.setattr(filesystem, "_run_sandbox_operation_if_required", execute)
    context = ToolContext(workspace_dir=str(tmp_path), file_edit_requires_fresh_read=True)
    token = current_tool_context.set(context)
    try:
        result = json.loads(await filesystem.read_file(target.name))
        assert result["truncated"] == (rows > 100)
        assert bool(context.workspace_file_read_state) == (rows <= 100)
        if rows <= 100:
            await filesystem.edit_file(target.name, "value 1", "changed 1")
            assert "changed 1" in target.read_text()
    finally:
        current_tool_context.reset(token)
