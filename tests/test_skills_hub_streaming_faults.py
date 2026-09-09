"""Transport and staging failure boundaries for streamed Skill installs."""

from __future__ import annotations

import asyncio
import errno
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest

from opensquilla.skills.hub.github import GitHubSource, _download_file
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillSourceFetchError
from tests.test_skills_hub_streaming import Response, StreamingClient


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "server"])
async def test_transient_file_download_retries_twice(tmp_path: Path, failure: str) -> None:
    class Client:
        calls = 0

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            self.calls += 1
            if self.calls < 3 and failure == "transport":
                raise httpx.ReadError("synthetic interrupted download")
            yield httpx.Response(
                503 if self.calls < 3 else 200,
                content=b"complete",
                request=httpx.Request(method, url),
            )

    client = Client()
    target = tmp_path / "artifact"
    await _download_file(client, "https://example.invalid/file", target, {})
    assert client.calls == 3
    assert target.read_bytes() == b"complete"


@pytest.mark.asyncio
async def test_rate_limit_returns_immediately_without_retry(tmp_path: Path) -> None:
    class Client:
        calls = 0

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            self.calls += 1
            yield httpx.Response(
                429, headers={"Retry-After": "30"}, request=httpx.Request(method, url),
            )

    client = Client()
    with pytest.raises(SkillSourceFetchError) as raised:
        await _download_file(client, "https://example.invalid/file", tmp_path / "file", {})
    assert client.calls == 1
    assert raised.value.diagnostics[0].code == "FETCH_RATE_LIMITED"


@pytest.mark.asyncio
async def test_disk_full_removes_entire_staging_reservation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", StreamingClient)
    monkeypatch.setattr(StreamingClient, "count", 4)
    original = Path.open

    class FullDisk:
        def write(self, data):
            raise OSError(errno.ENOSPC, "synthetic disk full")

    @contextmanager
    def open_file(path, *args, **kwargs):
        with original(path, *args, **kwargs) as handle:
            yield FullDisk() if path.name == "0.txt" and args == ("wb",) else handle

    monkeypatch.setattr(Path, "open", open_file)
    managed = tmp_path / "managed"
    service = SkillManagementService(
        router=SourceRouter([GitHubSource()]), managed_dir=managed,
        lockfile_path=tmp_path / "lock.json", journal_path=tmp_path / "journal.json",
    )
    result = await service.install("https://github.com/acme/demo", "github")
    assert not result.success
    assert not (managed / "demo").exists()
    assert not list((managed / ".opensquilla-staging").glob("*"))
    assert StreamingClient.active == 0


@pytest.mark.asyncio
async def test_download_cancel_joins_workers_before_staging_cleanup(tmp_path: Path, monkeypatch):
    started = asyncio.Event()

    class WaitingResponse(Response):
        async def aiter_bytes(self, *args):
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    class Client(StreamingClient):
        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            type(self).active += 1
            try:
                yield WaitingResponse()
            finally:
                type(self).active -= 1

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    managed = tmp_path / "managed"
    service = SkillManagementService(
        router=SourceRouter([GitHubSource()]), managed_dir=managed,
        lockfile_path=tmp_path / "lock.json", journal_path=tmp_path / "journal.json",
    )
    task = asyncio.create_task(service.install("https://github.com/acme/demo", "github"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert Client.active == 0
    assert not (managed / "demo").exists()
    assert not list((managed / ".opensquilla-staging").glob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_loader", [False, True], ids=["verified", "legacy"])
async def test_postflight_hash_reads_leave_gateway_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_loader: bool,
) -> None:
    from opensquilla.skills.hub import management
    from opensquilla.skills.loader import SkillLoader
    from tests.test_skills.test_hub_management_service import FakeImmutableSource

    managed = tmp_path / "managed"
    lockfile = tmp_path / "lock.json"
    loader = SkillLoader(managed_dir=managed, lockfile_path=lockfile)
    loader.reload(force=True, reason="test.initial")

    class LegacyLoader:
        reload_verified = None
        catalog_publication_barrier = None

        def __getattr__(self, name):
            return getattr(loader, name)

    main_thread = threading.get_ident()
    postflight_threads: list[int] = []
    original_hash = management.compute_tree_sha256

    def observed_hash(path: Path) -> str:
        if path == managed / "demo":
            postflight_threads.append(threading.get_ident())
        return original_hash(path)

    monkeypatch.setattr(management, "compute_tree_sha256", observed_hash)
    service = SkillManagementService(
        router=SourceRouter([FakeImmutableSource({
            "SKILL.md": "---\nname: demo\ndescription: Synthetic fixture.\n---\n# Demo\n",
        })]),
        managed_dir=managed, lockfile_path=lockfile,
        loader=LegacyLoader() if legacy_loader else loader,
        journal_path=tmp_path / "journal.json",
    )
    result = await service.install("demo", "fake")
    assert result.success, result.to_dict()
    repeated = await service.install("demo", "fake")
    assert repeated.success and repeated.unchanged, repeated.to_dict()
    assert postflight_threads
    assert main_thread not in postflight_threads
