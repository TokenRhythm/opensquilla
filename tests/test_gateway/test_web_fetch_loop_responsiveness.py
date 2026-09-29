"""Source-level WS handler integration; no network listener or Desktop process."""

from __future__ import annotations

import asyncio
import importlib
import socket
import threading

import httpx
import pytest

from opensquilla.gateway.rpc import get_dispatcher
from tests.test_gateway.test_websocket_connection_stability import _Socket, _start

fetch = importlib.import_module("opensquilla.tools.builtin.web_fetch")


async def _wait_thread_event(event: threading.Event) -> None:
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("finish", ["complete", "cancel"])
async def test_two_ws_connections_answer_probes_while_fetch_dns_waits(monkeypatch, finish):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    forced_release = threading.Event()
    requests: list[str] = []
    workers: list[int] = []
    loop_thread = threading.get_ident()

    def fail_safe() -> None:
        # This runs independently of asyncio: the old synchronous DNS path
        # must fail the assertion, rather than deadlocking the test runner.
        forced_release.set()
        release.set()

    watchdog = threading.Timer(1.5, fail_safe)
    watchdog.daemon = True

    def resolve(hostname, port):
        assert hostname == "public.example.test"
        workers.append(threading.get_ident())
        if not entered.is_set():
            watchdog.start()
            entered.set()
        try:
            release.wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        finally:
            ended.set()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            requests.append(url)
            return httpx.Response(
                200, text="synthetic content", headers={"content-type": "text/plain"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(fetch, "managed_network_httpx_kwargs", lambda: {})
    monkeypatch.setattr(fetch, "_web_fetch_httpx_client_kwargs", lambda *args: {})
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fetch._cache.clear()
    sockets = [_Socket(), _Socket()]
    handlers: list[asyncio.Task[None]] = []
    tool: asyncio.Task | None = None
    source = "https://public.example.test/article"
    try:
        for ws in sockets:
            handlers.append(await _start(ws, get_dispatcher()))
        tool = asyncio.create_task(fetch.run_web_fetch_payload(source))
        await _wait_thread_event(entered)
        for index, ws in enumerate(sockets):
            ws.incoming.put_nowait({"type": "ping", "nonce": f"blocked-dns-{index}"})
        replies = await asyncio.gather(*(
            ws.wait_frame(type="pong", nonce=f"blocked-dns-{index}")
            for index, ws in enumerate(sockets)
        ))
        assert len(replies) == 2
        assert not forced_release.is_set(), "Gateway probes waited for synchronous DNS"
        assert not release.is_set()
        assert not tool.done()
        assert workers and all(worker != loop_thread for worker in workers)
        assert requests == []
        assert all(not ws.close_codes for ws in sockets)
        if finish == "cancel":
            tool.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tool, 0.5)
            assert not ended.is_set()
        release.set()
        await _wait_thread_event(ended)
        if finish == "complete":
            assert (await tool)["status"] == 200
            assert requests == [source]
        else:
            # A completed but abandoned resolver must not resume the HTTP hop.
            await asyncio.sleep(0)
            assert requests == []
            assert len(fetch._cache) == 0
    finally:
        release.set()
        watchdog.cancel()
        if tool is not None:
            if not tool.done():
                tool.cancel()
            await asyncio.gather(tool, return_exceptions=True)
        if entered.is_set():
            await _wait_thread_event(ended)
        for ws in sockets:
            ws.incoming.put_nowait(None)
        await asyncio.wait_for(asyncio.gather(*handlers), 2)
        fetch._cache.clear()
