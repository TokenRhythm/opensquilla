"""Complete fixed materials, outbox compatibility and adapter lifetime."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import artifact_archives
from opensquilla.artifacts import ArtifactStore, collect_artifact_bundle
from opensquilla.channels import artifact_delivery
from opensquilla.channels.artifact_delivery import (
    artifact_fallback_lines,
    deliver_artifacts_as_channel_files,
)
from opensquilla.channels.contract import ChannelCapabilityProfile, ChannelSendResult
from opensquilla.channels.delivery_store import install_outbox
from opensquilla.channels.types import ChannelArtifactDeliveryRequest, IncomingMessage


def publication(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "page.html").write_text('<link rel="stylesheet" href="style.css">')
    (workspace / "style.css").write_text("body { color: red }")
    (workspace / "unrelated.txt").write_text("never export this")
    bundle = collect_artifact_bundle(workspace / "page.html", workspace_root=workspace)
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bundle(
        bundle, session_id="session", session_key="key", name="page.html",
        mime="text/html", source="test",
    )
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path / "media")))
    inbound = IncomingMessage(sender_id="sender", channel_id="chat", content="export page")
    request = ChannelArtifactDeliveryRequest(
        inbound=inbound, artifact_id=ref.id,
        file_path=str(store.resolve_for_download(ref.id, session_id="session")[1]),
        name=ref.name, mime_type=ref.mime, size=ref.size, session_id="session",
    )
    return store, ref, config, request


class Channel:
    capability_profile = ChannelCapabilityProfile(channel_type="test", artifact_delivery=True)
    _delivery_channel_name = "test-main"

    def __init__(self, store, outcome="sent"):
        self._delivery_store = store
        self.outcome = outcome
        self.requests = []
        self.payloads = []

    async def send(self, message):
        raise AssertionError("This test only sends artifact files")

    async def deliver_artifact(self, request):
        self.requests.append(request)
        self.payloads.append(Path(request.file_path).read_bytes())
        if self.outcome == "unknown":
            raise TimeoutError("receipt lost")
        if self.outcome == "failed":
            return ChannelSendResult.failed(capability="artifact_delivery", retryable=True)
        if self.outcome == "unconfirmed":
            return None
        return ChannelSendResult.sent(capability="artifact_delivery", provider_file_id="file")


async def send(channel, ref, config, request):
    return await deliver_artifacts_as_channel_files(
        channel, request.inbound, [ref.to_dict()], config, expected_session_id="session",
    )


async def test_complete_fixed_bundle_has_actual_zip_metadata_and_no_source_reread(
    tmp_path, channel_store,
):
    _, ref, config, request = publication(tmp_path)
    (tmp_path / "workspace/style.css").write_text("body { color: blue }")
    channel = Channel(await channel_store(tmp_path / "outbox.db"))
    install_outbox(channel)
    assert await send(channel, ref, config, request) == []
    actual = channel.requests[0]
    assert actual.name == Path(actual.file_path).name == "page.zip"
    assert actual.mime_type == "application/zip"
    assert actual.size == len(channel.payloads[0])
    with zipfile.ZipFile(io.BytesIO(channel.payloads[0])) as archive:
        assert set(archive.namelist()) == {"page.html", "style.css"}
        assert archive.read("style.css") == b"body { color: red }"
    assert not Path(actual.file_path).exists()
    assert await send(channel, ref, config, request) == []
    assert len(channel.requests) == 1
    with sqlite3.connect(tmp_path / "outbox.db") as conn:
        rows = conn.execute("SELECT message_json FROM channel_outbox").fetchall()
    metadata = json.loads(rows[0][0])["metadata"]
    assert metadata["artifact_name"] == "page.zip"
    assert metadata["artifact_mime_type"] == "application/zip"
    assert metadata["artifact_size"] == actual.size


@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed", "unconfirmed"])
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_old_new_outbox_bindings_cannot_reupload_or_claim_incompatible_receipt(
    tmp_path, channel_store, outcome, direction,
):
    _, ref, config, request = publication(tmp_path)
    path = tmp_path / "outbox.db"
    store = await channel_store(path)
    channel = Channel(store, outcome)
    install_outbox(channel)
    if direction == "upgrade":
        if outcome == "unknown":
            with pytest.raises(TimeoutError):
                await channel.deliver_artifact(request)
        else:
            await channel.deliver_artifact(request)
    else:
        await send(channel, ref, config, request)
    assert len(channel.requests) == 1
    await store.close()
    channel = Channel(await channel_store(path))
    install_outbox(channel)
    if direction == "upgrade":
        unsent = await send(channel, ref, config, request)
        assert len(unsent) == 1
        assert "no confirmed receipt" in " ".join(artifact_fallback_lines(unsent))
    else:
        result = await channel.deliver_artifact(request)
        assert not result.is_delivered()
        assert not result.retryable
    assert channel.requests == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 1


async def test_zip_retry_after_explicit_rejection_reuses_identical_bytes_and_id(
    tmp_path, channel_store,
):
    _, ref, config, request = publication(tmp_path)
    channel = Channel(await channel_store(tmp_path / "outbox.db"), "failed")
    install_outbox(channel)
    assert await send(channel, ref, config, request)
    channel.outcome = "sent"
    assert await send(channel, ref, config, request) == []
    assert len(channel.requests) == 2
    assert channel.requests[0].delivery_id == channel.requests[1].delivery_id
    assert channel.payloads[0] == channel.payloads[1]


@pytest.mark.parametrize("outcome", ["unconfirmed", "unknown"])
async def test_missing_receipt_stays_unconfirmed_on_first_send_and_replay(
    tmp_path, channel_store, outcome,
):
    _, ref, config, request = publication(tmp_path)
    channel = Channel(await channel_store(tmp_path / "outbox.db"), outcome)
    install_outbox(channel)
    delivered = set()
    for _ in range(2):
        unsent = await deliver_artifacts_as_channel_files(
            channel, request.inbound, [ref.to_dict()], config,
            expected_session_id="session", delivered_keys=delivered,
        )
        fallback = " ".join(artifact_fallback_lines(unsent))
        assert "awaiting confirmation" in fallback
        assert "failed" not in fallback
    assert len(channel.requests) == 1
    assert not delivered


async def test_missing_material_never_uploads_partial_html(tmp_path, channel_store):
    store, _, config, request = publication(tmp_path)
    ref = store.publish_bytes(
        b'<img src="missing.png">', name="broken.html", mime="text/html",
        session_id="session", session_key="key", source="legacy",
    )
    channel = Channel(await channel_store(tmp_path / "outbox.db"))
    unsent = await send(channel, ref, config, request)
    assert channel.requests == []
    assert "complete HTML bundle" in " ".join(artifact_fallback_lines(unsent))


async def test_awaited_adapter_holds_material_through_cancellation(tmp_path, channel_store):
    _, ref, config, request = publication(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    paths = []

    class SlowChannel(Channel):
        async def deliver_artifact(self, request):
            path = Path(request.file_path)
            paths.append(path)
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                # An adapter must settle its native reader before returning.
                assert path.exists()
                await finish.wait()
                assert path.read_bytes().startswith(b"PK")
                raise

    channel = SlowChannel(await channel_store(tmp_path / "outbox.db"))
    install_outbox(channel)
    task = asyncio.create_task(send(channel, ref, config, request))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert paths[0].exists()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].exists()
    diagnostics = await channel._delivery_store.diagnostics("test-main")
    assert diagnostics["outbox"]["unknown"]["count"] == 1


async def test_legacy_send_file_receives_real_zip_path(tmp_path):
    _, ref, config, request = publication(tmp_path)
    paths = []

    class Legacy:
        async def send_file(self, target, file_path):
            path = Path(file_path)
            assert target == "chat" and path.name == "page.zip"
            with zipfile.ZipFile(path) as archive:
                assert set(archive.namelist()) == {"page.html", "style.css"}
            paths.append(path)
            return ChannelSendResult.sent(capability="native_file_upload")

    assert await send(Legacy(), ref, config, request) == []
    assert not paths[0].exists()


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    root.mkdir()
    original = artifact_archives.tempfile.mkdtemp
    monkeypatch.setattr(
        artifact_archives.tempfile, "mkdtemp", lambda **kwargs: original(dir=root, **kwargs),
    )
    return root


async def test_self_contained_html_keeps_its_name_and_bytes(tmp_path, export_root):
    store, _, config, request = publication(tmp_path)
    payload = b"<h1>Self-contained page</h1>"
    ref = store.publish_bytes(
        payload, name="report.html", mime="text/html",
        session_id="session", session_key="key", source="test",
    )
    channel = Channel(None)
    assert await send(channel, ref, config, request) == []
    delivered = channel.requests[0]
    assert delivered.name == Path(delivered.file_path).name == "report.html"
    assert delivered.mime_type == "text/html" and delivered.size == len(payload)
    assert channel.payloads == [payload]
    assert list(export_root.iterdir()) == []


async def test_channel_slots_cover_capture_through_awaited_upload(
    tmp_path, monkeypatch, export_root,
):
    _, ref, config, request = publication(tmp_path)
    upload_started, release = asyncio.Event(), asyncio.Event()
    captures = []
    original = artifact_delivery.capture_saved_html

    def capture(*args, **kwargs):
        captures.append("capture")
        return original(*args, **kwargs)

    monkeypatch.setattr(artifact_delivery, "capture_saved_html", capture)

    class HeldChannel(Channel):
        async def deliver_artifact(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                upload_started.set()
            await release.wait()
            assert Path(request.file_path).exists()
            return ChannelSendResult.sent(capability="artifact_delivery")

    channel = HeldChannel(None)
    tasks = [asyncio.create_task(send(channel, ref, config, request)) for _ in range(2)]
    await asyncio.wait_for(upload_started.wait(), timeout=5)
    queued = asyncio.create_task(send(channel, ref, config, request))
    await asyncio.sleep(0.03)
    assert len(captures) == 2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    assert await asyncio.gather(*tasks) == [[], []]
    assert len(captures) == 2
    assert list(export_root.iterdir()) == []


async def test_channel_capture_cancel_settles_worker_before_releasing_material(
    tmp_path, monkeypatch, export_root,
):
    _, ref, config, request = publication(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = artifact_delivery.capture_saved_html

    def capture(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifact_delivery, "capture_saved_html", capture)
    channel = Channel(None)
    task = asyncio.create_task(send(channel, ref, config, request))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert channel.requests == []
    assert list(export_root.iterdir()) == []


async def test_adapter_reason_never_discloses_request_data(tmp_path):
    _, ref, config, request = publication(tmp_path)
    request.inbound.metadata["private_key"] = "secret-provider-token"

    class FailedChannel(Channel):
        async def deliver_artifact(self, request):
            return ChannelSendResult.failed(
                capability="artifact_delivery", reason=f"Failed request: {request!r}",
            )

    unsent = await send(FailedChannel(None), ref, config, request)
    fallback = " ".join(artifact_fallback_lines(unsent))
    assert "failed; no confirmed receipt" in fallback
    assert "secret-provider-token" not in fallback
    assert "file_path" not in fallback
    assert "opensquilla-html-export" not in fallback


async def test_authority_revoked_after_capture_cleans_material_without_url_fallback(
    tmp_path, monkeypatch, export_root,
):
    _, ref, config, request = publication(tmp_path)
    original_capture = artifact_delivery.capture_saved_html
    original_get_ref = ArtifactStore.get_ref
    revoked = False

    def capture(*args, **kwargs):
        nonlocal revoked
        result = original_capture(*args, **kwargs)
        revoked = True
        return result

    def get_ref(self, **kwargs):
        if revoked:
            raise PermissionError("revoked with private context")
        return original_get_ref(self, **kwargs)

    monkeypatch.setattr(artifact_delivery, "capture_saved_html", capture)
    monkeypatch.setattr(ArtifactStore, "get_ref", get_ref)
    channel = Channel(None)
    unsent = await send(channel, ref, config, request)
    assert channel.requests == []
    assert "denied" in " ".join(artifact_fallback_lines(unsent))
    assert "Generated file:" not in " ".join(artifact_fallback_lines(unsent))
    assert list(export_root.iterdir()) == []


async def test_cleanup_error_does_not_revoke_a_confirmed_send(
    tmp_path, monkeypatch, export_root,
):
    _, ref, config, request = publication(tmp_path)
    cleanup = artifact_archives.cleanup_archive

    async def noisy_cleanup(path):
        await cleanup(path)
        raise OSError("local cleanup report failed")

    monkeypatch.setattr(artifact_archives, "cleanup_archive", noisy_cleanup)
    channel = Channel(None)
    delivered = set()
    unsent = await deliver_artifacts_as_channel_files(
        channel, request.inbound, [ref.to_dict()], config,
        expected_session_id="session", delivered_keys=delivered,
    )
    assert unsent == []
    assert len(delivered) == len(channel.requests) == 1
    assert list(export_root.iterdir()) == []
