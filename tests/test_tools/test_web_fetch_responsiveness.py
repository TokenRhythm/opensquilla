"""Offline fetch controls: blocked work must not stall unrelated loop work."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import socket
import threading

import httpx
import pytest

from opensquilla.tools import fetch_work
from opensquilla.tools.types import SSRFBlockedError

fetch = importlib.import_module("opensquilla.tools.builtin.web_fetch")


@pytest.fixture
def network(monkeypatch):
    requests = []
    responses = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            requests.append(url)
            return responses.get(url) or httpx.Response(
                200, text="readable content", headers={"content-type": "text/plain"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(fetch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(fetch, "managed_network_httpx_kwargs", lambda: {})
    monkeypatch.setattr(fetch, "_web_fetch_httpx_client_kwargs", lambda *args: {})
    monkeypatch.setattr(fetch, "_check_ssrf", lambda url: ["93.184.216.34"])
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fetch._cache.clear()
    yield requests, responses
    fetch._cache.clear()


async def _wait_entered(entered: threading.Event) -> None:
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial_dns", "redirect_dns", "parse", "fallback", "text"])
async def test_slow_fetch_stage_leaves_loop_responsive(monkeypatch, network, stage):
    requests, responses = network
    source, target = "https://public.test/start", "https://public.test/redirect"
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    worker_threads = []

    def block(result):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "test worker was not released"
        return result

    if stage in {"initial_dns", "redirect_dns"}:
        monkeypatch.setattr(
            fetch, "_check_ssrf",
            lambda url: block(["93.184.216.34"])
            if stage == "initial_dns" or url == target else ["93.184.216.34"],
        )
        if stage == "redirect_dns":
            responses[source] = httpx.Response(302, headers={"location": target})
    else:
        responses[source] = httpx.Response(
            200, text="<p>body</p>", headers={"content-type": "text/html"},
        )
        monkeypatch.setattr(fetch, "_try_readability", lambda html: (
            block(("title", "body", "readability")) if stage == "parse"
            else None if stage == "fallback" else ("title", "body", "readability")
        ))
        monkeypatch.setattr(fetch, "_try_html2text", lambda html: block(("", "body", "html2text")))
        monkeypatch.setattr(fetch, "_markdown_to_text", lambda markdown: block(markdown))

    task = asyncio.create_task(fetch.run_web_fetch_payload(
        source, extract_mode="text" if stage == "text" else "markdown",
    ))
    try:
        await _wait_entered(entered)
        # This timer stands for unrelated Gateway work. The worker is still
        # deliberately blocked; correctness does not depend on disk/DNS speed.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.5)
        assert not task.done()
        assert all(identity != loop_thread for identity in worker_threads)
        if stage == "initial_dns":
            assert requests == []
        elif stage == "redirect_dns":
            assert requests == [source]
    finally:
        release.set()
        result = await task
    assert result["status"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout"])
async def test_abandoned_dns_never_starts_request_or_populates_cache(monkeypatch, network, action):
    requests, _ = network
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def resolve(url):
        entered.set()
        try:
            assert release.wait(3)
            return ["93.184.216.34"]
        finally:
            ended.set()

    monkeypatch.setattr(fetch, "_check_ssrf", resolve)
    if action == "timeout":
        monkeypatch.setattr(fetch, "_DNS_TIMEOUT_SECONDS", 0.05)
    task = asyncio.create_task(fetch.run_web_fetch_payload("https://public.test/cancel"))
    try:
        await _wait_entered(entered)
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
        else:
            result = await asyncio.wait_for(task, 0.5)
            assert result["error"] == "timed_out"
        assert not ended.is_set()
        assert requests == []
    finally:
        release.set()
        await _wait_entered(ended)
    await asyncio.sleep(0.01)
    assert requests == []
    assert len(fetch._cache) == 0


@pytest.mark.asyncio
async def test_cancelled_workers_keep_capacity_until_real_completion(monkeypatch):
    entered = [threading.Event() for _ in range(fetch_work._BLOCKING_WORKERS)]
    releases = [threading.Event() for _ in entered]
    ended = [threading.Event() for _ in entered]
    submitted = []
    real_submit = fetch_work._blocking_executor.submit

    def submit(*args, **kwargs):
        submitted.append(args)
        return real_submit(*args, **kwargs)

    def work(index):
        entered[index].set()
        try:
            assert releases[index].wait(3)
        finally:
            ended[index].set()

    monkeypatch.setattr(fetch_work._blocking_executor, "submit", submit)
    jobs = [asyncio.create_task(fetch._run_blocking(work, i)) for i in range(len(entered))]
    queued = None
    try:
        for event in entered:
            await _wait_entered(event)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        queued = asyncio.create_task(fetch._run_blocking(lambda: "next"))
        await asyncio.sleep(0.03)
        assert len(submitted) == fetch_work._BLOCKING_WORKERS
        assert not queued.done()
        releases[0].set()
        assert await asyncio.wait_for(queued, 1) == "next"
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        if queued is not None:
            await queued
        for event in ended:
            await _wait_entered(event)


@pytest.mark.asyncio
async def test_async_guard_still_blocks_private_redirect_before_connect(monkeypatch, network):
    requests, responses = network
    source, target = "https://public.test/start", "http://private.test/secret"
    responses[source] = httpx.Response(302, headers={"location": target})
    checked = []

    def resolve(hostname, port):
        checked.append(hostname)
        ip = "10.0.0.8" if hostname == "private.test" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch, "_check_ssrf", fetch.validate_http_url_for_fetch)
    with pytest.raises(SSRFBlockedError):
        await fetch.run_web_fetch_payload(source)
    assert checked == ["public.test", "public.test", "private.test"]
    assert requests == [source]
    assert len(fetch._cache) == 0


@pytest.mark.asyncio
async def test_blocking_worker_preserves_context():
    marker = contextvars.ContextVar("web_fetch_test_marker", default="missing")
    token = marker.set("current-tool")
    try:
        assert await fetch._run_blocking(marker.get) == "current-tool"
    finally:
        marker.reset(token)


@pytest.mark.asyncio
async def test_cancelled_parser_discards_late_failure_and_releases_capacity(monkeypatch, network):
    _, responses = network
    source = "https://public.test/article"
    responses[source] = httpx.Response(
        200, text="<p>body</p>", headers={"content-type": "text/html"},
    )
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    def parse(_html):
        entered.set()
        try:
            assert release.wait(3)
            raise ValueError("late parser failure")
        finally:
            ended.set()

    monkeypatch.setattr(fetch, "_try_readability", parse)
    task = asyncio.create_task(fetch.run_web_fetch_payload(source))
    try:
        await _wait_entered(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        release.set()
        await _wait_entered(ended)
        assert await fetch._run_blocking(lambda: "recovered") == "recovered"
        await asyncio.sleep(0.01)
        assert len(fetch._cache) == 0
        assert unhandled == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await _wait_entered(ended)
        loop.set_exception_handler(previous_handler)


def test_worker_can_finish_after_caller_loop_closes():
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        try:
            assert release.wait(3)
        finally:
            ended.set()

    async def abandon():
        task = asyncio.create_task(fetch._run_blocking(work))
        await _wait_entered(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(abandon())
    finally:
        loop.close()
        release.set()
    assert ended.wait(2)
    assert asyncio.run(fetch._run_blocking(lambda: "new loop")) == "new loop"


def test_blocking_workers_support_sequential_event_loops():
    async def run():
        # Contention binds an asyncio semaphore to its loop.
        results = await asyncio.gather(*[
            fetch._run_blocking(lambda: 1) for _ in range(fetch_work._BLOCKING_WORKERS + 2)
        ])
        assert sum(results) == fetch_work._BLOCKING_WORKERS + 2

    asyncio.run(run())
    asyncio.run(run())
