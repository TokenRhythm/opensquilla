"""Real ingress and Office execution preparation without a model/provider call."""

from __future__ import annotations

import html
import io
import json
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
)
from opensquilla.artifact_session.working_files import get_working_files
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.gateway.routing import tool_context_from_envelope
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.working_versions import with_working_versions
from opensquilla.office_format import OFFICE_MIMES
from tests.test_gateway.test_turn_ingress_rpc import SESSION_KEY, _open_real_stack


def _office_fixture(format: str) -> tuple[bytes, dict]:
    """Use actual Office packages with the quoted paragraph, shape or cells."""
    output = io.BytesIO()
    text = "批注发送测试"
    if format == "docx":
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph(text)
        document.save(output)
        selection = {
            "format": format, "kind": "text", "text": text, "pageIndex": 0,
            "source": {"story": "body", "path": [0]},
        }
    elif format == "pptx":
        pptx = pytest.importorskip("pptx")
        presentation = pptx.Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        shape = slide.shapes.add_textbox(914400, 914400, 3657600, 914400)
        shape.text = text
        presentation.save(output)
        selection = {
            "format": format, "kind": "element", "text": text, "slideIndex": 0,
            "shapeId": str(shape.shape_id), "name": shape.name, "origin": "slide",
        }
    else:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "目标"
        sheet.append([text, 12])
        workbook.save(output)
        selection = {
            "format": format, "kind": "range", "text": text,
            "sheetIndex": 0, "sheetName": "目标", "range": "A1:B1",
        }
    return output.getvalue(), selection


@pytest.mark.parametrize("format", ["docx", "pptx", "xlsx"])
async def test_office_annotation_only_send_accepts_prepares_and_replays_once(tmp_path, format):
    original, selection = _office_fixture(format)
    async with _open_real_stack(tmp_path / f"{format}-send.db") as stack:
        store = ArtifactStore(tmp_path / "media")
        ref = store.publish_bytes(
            original, session_id=stack.session_id, session_key=SESSION_KEY,
            name=f"annotation.{format}", mime=OFFICE_MIMES[format], source="test",
        )
        service = await ArtifactSessionService.from_session_storage(stack.storage)
        try:
            head = await service.create_document(
                session_key=SESSION_KEY, session_id=stack.session_id, name=ref.name,
                kind={"docx": ArtifactKind.DOCUMENT, "pptx": ArtifactKind.PRESENTATION,
                      "xlsx": ArtifactKind.SPREADSHEET}[format],
                initial_artifact=ArtifactBlobRef(
                    artifact_id=ref.id, sha256=ref.sha256, filename=ref.name,
                    media_type=ref.mime, byte_size=ref.size,
                ),
                actor=Actor(ActorKind.USER, "annotation-send-test"),
            )
            events = []

            async def controlled_handler(run):
                stack.received_runs.append(run)

                async def controlled_response():
                    # This starts only after the production execution wrapper
                    # verifies the pin and materializes the Office candidate.
                    stack.handler_started.set()
                    await stack.release_handler.wait()
                    yield DoneEvent(text="Verified annotation context")

                tools = tool_context_from_envelope(run.envelope, is_owner=True)
                try:
                    async for event in with_working_versions(
                        controlled_response(), config=stack.context.config,
                        session_manager=stack.manager, session_key=run.session_key,
                        session_id=run.envelope.session_id, workspace=tools.workspace_dir,
                        actor_id=run.task_id,
                        office_candidate=run.envelope.metadata.get("office_candidate"),
                        annotation_selection=run.envelope.metadata.get("annotation_selection"),
                    ):
                        events.append(event)
                finally:
                    stack.handler_started.set()

            stack.runtime._turn_handler = controlled_handler
            # This is the normal left-composer annotation-only fallback text;
            # there is no second sending endpoint or model invocation here.
            params = {
                "sessionKey": SESSION_KEY,
                "clientRequestId": f"{format}-annotation-only",
                "message": "Apply the attached page annotations.",
                "queueMode": "followup",
                "pageContext": {
                    "resourceId": f"document:{head.document.document_id}",
                    "revisionId": head.revision.revision_id,
                    "sha256": head.revision.artifact_sha256,
                    "annotations": [{
                        "text": "请将选中内容改为更具体的说明。",
                        "selectionText": selection["text"], "selection": selection,
                    }],
                },
            }
            accepted = await get_dispatcher().dispatch(
                "office-send", "chat.send", params, stack.context,
            )
            assert accepted.error is None, accepted.error
            task_id = accepted.payload["task_id"]
            await stack.wait_until_running(task_id)
            assert not any(isinstance(event, ErrorEvent) for event in events), events
            run = stack.received_runs[0]
            assert run.queue_mode == "followup"
            context = json.loads(html.unescape(
                run.message.split("<page_context>", 1)[1].split("</page_context>", 1)[0],
            ))
            assert context["annotations"] == params["pageContext"]["annotations"]
            assert context["versionId"] == head.revision.revision_id
            assert context["revisionId"] == head.revision.revision_id
            assert context["sha256"] == ref.sha256
            assert "_officeCandidate" not in context
            candidate = run.envelope.metadata["office_candidate"]
            assert candidate["documentId"] == head.document.document_id
            assert candidate["revisionId"] == head.revision.revision_id
            assert candidate["sha256"] == ref.sha256
            candidate_path = Path(context["workingFile"])
            assert candidate_path.is_relative_to(tmp_path / "workspace")
            assert candidate_path.read_bytes() == original
            binding = await get_working_files(service, head.document.document_id)
            assert candidate_path != binding.entry
            assert binding.entry.read_bytes() == original
            tools = tool_context_from_envelope(run.envelope, is_owner=True)
            assert tools.surfaced_tools is None and tools.allowed_tools is None
            assert "artifact_context" not in run.envelope.runtime_services

            async def assert_single_replay():
                replay = await get_dispatcher().dispatch(
                    "office-replay", "chat.send", params, stack.context,
                )
                assert replay.error is None, replay.error
                assert replay.payload["replayed"] is True
                assert replay.payload["task_id"] == task_id
                assert len(stack.received_runs) == 1
                assert set(stack.runtime._tasks) <= {task_id}
                async with stack.storage.conn.execute("SELECT COUNT(*) FROM agent_tasks") as cursor:
                    assert (await cursor.fetchone())[0] == 1
                entries = await stack.storage.get_canonical_transcript(stack.session_id)
                users = [entry for entry in entries if entry.role == "user"]
                assert len(users) == 1
                assert json.loads(users[0].content)["page_context"] == params["pageContext"]

            await assert_single_replay()
            stack.release_handler.set()
            terminal = await stack.wait_for_terminal(task_id, timeout=10)
            assert terminal.status == "succeeded"
            assert any(isinstance(event, DoneEvent) for event in events)
            assert not any(isinstance(event, ErrorEvent) for event in events), events
            assert not candidate_path.exists()
            assert binding.entry.read_bytes() == original
            assert len(await service.list_revisions(head.document.document_id)) == 1
            await assert_single_replay()
        finally:
            await service.close()
