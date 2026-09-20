"""Provider-only relay projection for corpus tests, preserving web tool transports."""

from __future__ import annotations

import os

import httpx

from scripts.live_tokenrhythm_transport import RelayTarget, TransportRejectedError


def install_from_env():
    if os.environ.get("OPENSQUILLA_LIVE_TRANSPORT") != "1":
        raise TransportRejectedError("live_transport_disabled")
    target = RelayTarget(
        os.environ["OPENSQUILLA_LIVE_RELAY_URL"], os.environ["OPENSQUILLA_LIVE_RELAY_CLIENT_KEY"]
    )
    original_async = httpx.AsyncClient.send
    original_sync = httpx.Client.send

    def project(request):
        if request.url.host == "tokenrhythm.studio":
            return target.project(request)
        if target.client_key in str(request.url) or any(
            target.client_key in value for value in request.headers.values()
        ):
            raise TransportRejectedError("placeholder_outside_relay")
        return request

    async def async_send(self, request, *args, **kwargs):
        return await original_async(self, project(request), *args, **kwargs)

    def sync_send(self, request, *args, **kwargs):
        return original_sync(self, project(request), *args, **kwargs)

    httpx.AsyncClient.send = async_send
    httpx.Client.send = sync_send

    def restore():
        httpx.AsyncClient.send = original_async
        httpx.Client.send = original_sync

    return restore
