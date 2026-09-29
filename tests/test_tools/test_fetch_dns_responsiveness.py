"""All in-process URL fetchers must isolate the shared synchronous DNS guard."""

from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from opensquilla.provider import image_generation
from opensquilla.skills.hub import clawhub
from opensquilla.skills.hub.source import SkillSourceFetchError, SourceResolution
from opensquilla.tools import ssrf
from opensquilla.tools.builtin import media
from opensquilla.tools.types import ToolError


@pytest.fixture(params=["image", "generated_image", "skill_archive"])
def fetch_url(request, monkeypatch, tmp_path):
    timeout = 30.0

    async def fetch():
        url = "https://public.test/result.png"
        if request.param == "image":
            return await media._fetch_image_url(url)
        if request.param == "generated_image":
            return await image_generation._download_generated_image(
                url, timeout_seconds=timeout, provider_label="test",
            )
        resolution = SourceResolution(
            source_id="clawhub", requested_identifier="test", canonical_identifier="test",
            immutable=True, artifact_kind="archive", artifact_url=url,
        )
        return await clawhub.ClawHubSource()._fetch_into(
            resolution, tmp_path / "tree", tmp_path / "download.zip",
        )

    def set_timeout(value):
        nonlocal timeout
        timeout = value
        monkeypatch.setattr(media, "_IMAGE_FETCH_TIMEOUT_SECONDS", value)
        monkeypatch.setattr(clawhub, "_ARTIFACT_FETCH_TIMEOUT_SECONDS", value)

    error_type = {
        "image": ToolError,
        "generated_image": RuntimeError,
        "skill_archive": SkillSourceFetchError,
    }[request.param]
    return fetch, set_timeout, error_type


async def wait_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout"])
@pytest.mark.parametrize("stage", ["dns", "proxy", "transport"])
async def test_slow_fetch_dns_keeps_loop_responsive_and_abandons_request(
    fetch_url, monkeypatch, action, stage,
):
    fetch, set_timeout, error_type = fetch_url
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    threads = []
    requests = []

    def block(result):
        threads.append(threading.get_ident())
        entered.set()
        try:
            assert release.wait(3)
            return result
        finally:
            ended.set()

    def connect(**kwargs):
        requests.append(kwargs)
        raise AssertionError("abandoned DNS must not reach HTTP")

    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs:
        block(addresses) if stage == "dns" else addresses,
    )
    monkeypatch.setattr(
        ssrf, "environment_proxy_url", lambda _url: block(None) if stage == "proxy" else None,
    )
    monkeypatch.setattr(
        ssrf, "pinned_transport", lambda *_args, **_kwargs:
        block(None) if stage == "transport" else None,
    )
    for module in (media, image_generation, clawhub):
        monkeypatch.setattr(module, "_trust_env", lambda: True)
    monkeypatch.setattr(httpx, "AsyncClient", connect)
    if action == "timeout":
        set_timeout(0.05)
    task = asyncio.create_task(fetch())
    try:
        await wait_event(entered)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.5)
        assert not task.done()
        assert threads and all(thread != loop_thread for thread in threads)
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
        else:
            with pytest.raises(error_type) as failure:
                await asyncio.wait_for(task, timeout=0.5)
            assert not isinstance(failure.value, TimeoutError)
        assert not ended.is_set()
        assert requests == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await wait_event(ended)
    await asyncio.sleep(0.01)
    assert requests == []


@pytest.mark.asyncio
async def test_fetch_dns_still_rejects_private_destination_before_http(fetch_url, monkeypatch):
    fetch, _, error_type = fetch_url
    requests = []

    def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    def connect(**kwargs):
        requests.append(kwargs)
        raise AssertionError("private target must not reach HTTP")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpx, "AsyncClient", connect)
    with pytest.raises(error_type):
        await fetch()
    assert requests == []
