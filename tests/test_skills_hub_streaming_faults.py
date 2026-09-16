"""Transport and staging failure boundaries for streamed Skill installs."""

from __future__ import annotations

import asyncio
import errno
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from opensquilla.skills.hub.github import GitHubSource, _download_file
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillSourceFetchError
from tests.test_skills_hub_streaming import MANIFEST, Response, StreamingClient


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_size", [None, 1])
async def test_download_budget_bounds_concurrent_writes_with_inaccurate_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared_size: int | None,
) -> None:
    from opensquilla.skills.hub import github

    class Client(StreamingClient):
        count = 4

        async def get(self, url, **kwargs):
            response = await super().get(url, **kwargs)
            if "/git/trees/" in url and declared_size is not None:
                for item in response.payload["tree"]:
                    item["size"] = declared_size
            return response

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            def chunks():
                if url.endswith("/SKILL.md"):
                    yield MANIFEST
                else:
                    yield from (b"data" for _ in range(50))

            yield Response(chunks=chunks)

    limit = len(MANIFEST) + 24
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        github, "DEFAULT_ARCHIVE_LIMITS",
        replace(github.DEFAULT_ARCHIVE_LIMITS, max_expanded_bytes=limit),
    )
    source = GitHubSource()
    resolution = await source.resolve("https://github.com/acme/demo")
    destination = tmp_path / "tree"
    with pytest.raises(SkillSourceFetchError) as raised:
        await source.fetch_resolved_into(resolution, destination)
    assert raised.value.diagnostics[0].code == "FETCH_SIZE_LIMIT"
    assert sum(p.stat().st_size for p in destination.rglob("*") if p.is_file()) <= limit


@pytest.mark.asyncio
async def test_manifest_size_rejected_before_reading_contents(tmp_path, monkeypatch):
    from opensquilla.skills.manifest import MAX_SKILL_FILE_BYTES

    class Client(StreamingClient):
        count = 0

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            yield Response(chunks=lambda: iter([b"x" * (MAX_SKILL_FILE_BYTES + 1)]))

    original_open = Path.open
    manifest_reads = []

    def observe_open(path, mode="r", *args, **kwargs):
        if path.name == "SKILL.md" and mode == "rb":
            manifest_reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(Path, "open", observe_open)
    source = GitHubSource()
    resolution = await source.resolve("https://github.com/acme/demo")
    with pytest.raises(SkillSourceFetchError) as raised:
        await source.fetch_resolved_into(resolution, tmp_path / "tree")
    assert raised.value.diagnostics[0].code == "MANIFEST_TOO_LARGE"
    assert not manifest_reads


@pytest.mark.asyncio
async def test_manifest_validation_uses_settled_worker(tmp_path, monkeypatch):
    from opensquilla.skills.hub import github

    monkeypatch.setattr(httpx, "AsyncClient", StreamingClient)
    monkeypatch.setattr(StreamingClient, "count", 0)
    original_prefix = github._manifest_prefix
    threads = []

    def observe_prefix(path):
        threads.append(threading.get_ident())
        return original_prefix(path)

    monkeypatch.setattr(github, "_manifest_prefix", observe_prefix)
    source = GitHubSource()
    resolution = await source.resolve("https://github.com/acme/demo")
    await source.fetch_resolved_into(resolution, tmp_path / "tree")
    assert threads and threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_retry_releases_only_truncated_download_bytes(tmp_path):
    from opensquilla.skills.hub.github import _DownloadBudget

    class InterruptedResponse(Response):
        async def aiter_bytes(self, *args):
            yield b"part"
            raise httpx.ReadError("synthetic interrupted download")

    class Client:
        calls = 0

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            self.calls += 1
            yield (
                InterruptedResponse() if self.calls == 1
                else Response(chunks=lambda: iter([b"complete"]))
            )

    # Another file has already consumed four bytes of the common budget.
    budget = _DownloadBudget(limit=12, used=4)
    target = tmp_path / "file"
    client = Client()
    await _download_file(client, "https://example.invalid/file", target, {}, budget=budget)
    assert target.read_bytes() == b"complete"
    assert budget.used == 12
    assert client.calls == 2


@pytest.mark.asyncio
async def test_manifest_cancellation_settles_read_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", StreamingClient)
    monkeypatch.setattr(StreamingClient, "count", 0)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_open = Path.open

    class SlowRead:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release manifest read")
            return self.handle.read(size)

    @contextmanager
    def slow_open(path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as handle:
            if path.name == "SKILL.md" and mode == "rb":
                try:
                    yield SlowRead(handle)
                finally:
                    finished.set()
            else:
                yield handle

    monkeypatch.setattr(Path, "open", slow_open)
    source = GitHubSource()
    resolution = await source.resolve("https://github.com/acme/demo")
    task = asyncio.create_task(source.fetch_resolved_into(resolution, tmp_path / "tree"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "worker must settle before staging can be removed"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert finished.is_set()


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
