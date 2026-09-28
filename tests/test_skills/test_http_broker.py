from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, UnixConnector, web

from opensquilla.skills.http_broker import (
    HTTPBrokerError,
    HTTPRoute,
    HTTPServiceGrant,
    SkillHTTPBroker,
)
from opensquilla.skills.script_runtime import SkillScriptGrant, SkillScriptRunner, private_inventory

KEY = "synthetic-broker-token"


@pytest.fixture
async def service() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []

    async def reply(request: web.Request) -> web.Response:
        calls.append(
            {
                "path": request.path_qs,
                "body": await request.read(),
                "authorization": request.headers.get("Authorization"),
            }
        )
        if request.path == "/redirect":
            raise web.HTTPFound("https://untrusted.invalid/secret")
        if request.path == "/large":
            return web.Response(body=b"x" * 8192)
        if request.path == "/slow":
            await asyncio.sleep(0.2)
        if request.path == "/secret":
            return web.Response(text=KEY)
        if request.path == "/error":
            return web.Response(
                status=409, body=b'{"code":"STALE_REVISION"}', content_type="application/json"
            )
        if request.path == "/image":
            return web.Response(body=b"raw image fixture", content_type="image/png")
        return web.json_response({"echo": calls[-1]["body"].decode()})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert isinstance(site._server, asyncio.Server)
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        await runner.cleanup()


def broker_at(tmp_path: Path, origin: str, **kwargs: Any) -> SkillHTTPBroker:
    sockets, receipts = tmp_path.parent / f"s-{uuid.uuid4().hex[:8]}", tmp_path / "receipts"
    sockets.mkdir()
    receipts.mkdir()
    return SkillHTTPBroker(
        services=(
            HTTPServiceGrant(
                name="source",
                origin=origin,
                api_key=KEY,
                timeout_seconds=0.08,
                max_request_bytes=1024,
                max_response_bytes=1024,
                routes=(
                    HTTPRoute("POST", r"/(read|redirect|large|slow|secret|error)"),
                    HTTPRoute("GET", r"/image", frozenset({"fileId"})),
                ),
            ),
        ),
        socket_directory=sockets,
        receipt_directory=receipts,
        execution_id="execution-1",
        caller_binding="task-owner",
        **kwargs,
    )


def payload(path: str = "/read", **changes: Any) -> dict[str, Any]:
    return {
        "service": "source",
        "method": "POST",
        "path": path,
        "bodyBase64": base64.b64encode(b'{"query":"fixture"}').decode(),
        **changes,
    }


async def post(socket: Path, value: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with ClientSession(connector=UnixConnector(path=str(socket))) as client:
        async with client.post("http://broker/call", json=value) as response:
            return response.status, await response.json()


async def test_real_unix_broker_preserves_bodies_and_host_receipt(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, calls = service
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    broker = broker_at(tmp_path, origin)
    async with broker.serve() as socket:
        status, response = await post(socket, payload())
        assert status == 200 and response["status"] == 200
        assert calls[0]["authorization"] == f"Bearer {KEY}"
        assert calls[0]["body"] == b'{"query":"fixture"}'
        stored = (broker.receipt_directory / f"{response['receiptId']}.json").read_bytes()
        assert hashlib.sha256(stored).hexdigest() == response["receiptId"]
        receipt = json.loads(stored)
        assert receipt["executionId"] == "execution-1"
        assert receipt["callerBinding"] == "task-owner"
        assert receipt["request"]["bodyBase64"] == payload()["bodyBase64"]
        assert receipt["response"]["bodyBase64"] == response["bodyBase64"]
        assert (
            receipt["response"]["bodySha256"]
            == hashlib.sha256(base64.b64decode(response["bodyBase64"])).hexdigest()
        )
        assert receipt["error"] is None and receipt["finishedAt"] >= receipt["startedAt"]
        assert KEY not in stored.decode() and KEY not in json.dumps(response)
    assert not socket.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"service": "other"},
        {"method": "DELETE"},
        {"path": "https://evil.invalid/read"},
        {"path": "//evil.invalid/read"},
        {"path": "/../read"},
        {"path": "/%2e%2e/read"},
        {"path": "/read?secret=1"},
        {"path": "/read#fragment"},
        {"path": "/write"},
        {"headers": {"Authorization": "model"}},
        {"url": "http://other.invalid"},
        {"bodyBase64": "not base64"},
        {"bodyBase64": base64.b64encode(b"x" * 1025).decode()},
    ],
)
async def test_out_of_scope_wire_does_not_reach_service(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
    changes: dict[str, Any],
) -> None:
    origin, calls = service
    broker = broker_at(tmp_path, origin)
    async with broker.serve() as socket:
        status, _ = await post(socket, payload(**changes))
    assert status == 400
    assert not calls


@pytest.mark.parametrize("path", ["/redirect", "/large", "/slow", "/secret"])
async def test_failed_upstream_is_bounded_recorded_and_not_leaked(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
    path: str,
) -> None:
    origin, calls = service
    broker = broker_at(tmp_path, origin)
    async with broker.serve() as socket:
        status, response = await post(socket, payload(path))
    assert status == 502 and len(calls) == 1
    raw = (broker.receipt_directory / f"{response['receiptId']}.json").read_text()
    assert json.loads(raw)["error"] is not None
    assert KEY not in raw and KEY not in json.dumps(response)


async def test_service_error_response_retains_status_and_raw_details(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
) -> None:
    broker = broker_at(tmp_path, service[0])
    async with broker.serve() as socket:
        status, response = await post(socket, payload("/error"))
    assert status == 200 and response["status"] == 409
    assert json.loads(base64.b64decode(response["bodyBase64"])) == {"code": "STALE_REVISION"}


async def test_get_image_query_is_explicit_and_request_body_forbidden(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
) -> None:
    broker = broker_at(tmp_path, service[0])
    request = payload("/image?fileId=source", method="GET", bodyBase64=None)
    async with broker.serve() as socket:
        status, response = await post(socket, request)
        assert status == 200 and response["contentType"] == "image/png"
        assert base64.b64decode(response["bodyBase64"]) == b"raw image fixture"
        assert (await post(socket, {**request, "bodyBase64": ""}))[0] == 400
        assert (await post(socket, {**request, "path": "/image?fileId=a&fileId=b"}))[0] == 400
        assert (await post(socket, {**request, "path": "/image?%75rl=other"}))[0] == 400
        assert (await post(socket, {**request, "path": "/image?%66ileId=a&fileId=b"}))[0] == 400


async def test_lifetime_budget_applies_across_socket_restarts(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
) -> None:
    broker = broker_at(tmp_path, service[0], max_calls=1)
    async with broker.serve() as socket:
        assert (await post(socket, payload()))[0] == 200
    async with broker.serve() as socket:
        assert (await post(socket, payload()))[0] == 429
    assert len(service[1]) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.invalid",
        "https://user:pass@host.invalid",
        "https://host.invalid/path",
        "file:///tmp/data",
    ],
)
def test_untrusted_service_origins_are_rejected(origin: str) -> None:
    with pytest.raises(HTTPBrokerError):
        HTTPServiceGrant(name="source", origin=origin, routes=(HTTPRoute("POST", "/read"),))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_real_sandbox_can_only_use_granted_socket_and_host_records_private_state(
    tmp_path: Path,
    service: tuple[str, list[dict[str, Any]]],
) -> None:
    broker = broker_at(tmp_path, service[0])
    skill = tmp_path / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: test\n---\n")
    (skill / "scripts/main.py").write_text("""import http.client,json,socket,sys
from pathlib import Path
class Connection(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
  self.sock.connect('/host/broker.sock')
client=Connection('broker')
client.request('POST','/call',body=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'})
response=client.getresponse()
body=response.read()
Path('/private/state.json').write_bytes(body)
print(body.decode())
""")
    work, inputs, private = (tmp_path / name for name in ("work", "inputs", "private"))
    for directory in (work, inputs, private):
        directory.mkdir()
    grant = SkillScriptGrant.pin("demo", skill, frozenset({"scripts/main.py"}))
    runner = SkillScriptRunner(
        grants=(grant,),
        workspace=work,
        inputs=inputs,
        private_root=private,
        receipt_root=broker.receipt_directory,
        broker=broker,
        execution_id=broker.execution_id,
        caller_binding=broker.caller_binding,
    )
    request = json.dumps(payload()).encode()
    result = await runner.run("demo", "scripts/main.py", [], stdin=request)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert (broker.receipt_directory / f"{response['receiptId']}.json").is_file()
    receipts = list((broker.receipt_directory / "invocations").glob("*.json"))
    assert len(receipts) == 1
    proof = json.loads(receipts[0].read_text())
    assert proof["privateDigests"] == private_inventory(private)
    assert proof["stdinSha256"] == hashlib.sha256(request).hexdigest()
    assert proof["stdoutSha256"] == hashlib.sha256(result.stdout.encode()).hexdigest()
    assert proof["packageSha256"] == grant.digest
    assert proof["callerBinding"] == broker.caller_binding
    assert proof["returncode"] == 0
    readonly = await runner.run("demo", "scripts/main.py", [], readonly=True, stdin=request)
    assert readonly.returncode != 0
    assert len(service[1]) == 1
    assert list((broker.receipt_directory / "invocations").glob("*.json")) == receipts
