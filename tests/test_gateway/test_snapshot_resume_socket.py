"""Lost resume replies retain independent replay progress over a real socket."""

import asyncio
import json
import socket
from types import SimpleNamespace

import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect

from opensquilla.gateway import rpc_sessions, rpc_transport, session_streams, websocket
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.transport_flow import SESSION_FLOW_V2_CAPABILITY, get_transport_budget
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

assert rpc_sessions and rpc_transport


async def test_two_session_pending_resume_retries_over_real_socket(tmp_path, monkeypatch):
    registry = websocket.ConnectionRegistry()
    streams = session_streams.SessionStreamRegistry(stream_generation="resume-retry-socket")
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    initial_bytes = get_transport_budget().used
    subscriptions = websocket.SubscriptionManager()
    storage = await SessionStorage.open(str(tmp_path / "resume.sqlite"))
    keys = ["agent:main:webchat:parent", "agent:main:webchat:other"]
    for key in keys:
        await storage.upsert_session(SessionNode(
            session_key=key, session_id=key.rsplit(":", 1)[-1], agent_id="main",
        ))
    config = GatewayConfig(
        auth=AuthConfig(mode="none", token=None), state_dir=str(tmp_path / "state"),
        config_path=str(tmp_path / "config.toml"), ws_writer_queue_enabled=True,
        client_ws_keepalive_timeout_s=0,
    )

    async def endpoint(ws):
        await websocket.handle_ws_connection(
            ws, config, dispatcher=get_dispatcher(), subscription_manager=subscriptions,
            session_manager=SimpleNamespace(storage=storage),
        )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[WebSocketRoute("/ws", endpoint)]), host="127.0.0.1", port=port,
        ws="websockets", lifespan="off", ws_max_size=MAX_PAYLOAD_BYTES,
        timeout_graceful_shutdown=2, log_level="error", access_log=False,
    ))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.001)
        async with connect(f"ws://127.0.0.1:{port}/ws", max_size=MAX_PAYLOAD_BYTES) as client:
            frames = []
            request_id = 0

            async def receive(**match):
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await client.recv())
                        frames.append(frame)
                        if all(frame.get(k) == v for k, v in match.items()):
                            return frame

            async def rpc(method, params):
                nonlocal request_id
                request_id += 1
                value = str(request_id)
                await client.send(json.dumps({
                    "type": "req", "id": value, "method": method, "params": params,
                }))
                frame = await receive(type="res", id=value)
                assert frame["ok"], frame.get("error")
                return frame["payload"]

            def replay_frames(key):
                return [frame for frame in frames if (frame.get("meta") or {}).get("replayed")
                        and frame["payload"].get("session_key") == key]

            await receive(event="connect.challenge")
            await client.send(json.dumps({"type": "req", "id": "connect", "method": "connect",
                "params": {"minProtocol": 4, "maxProtocol": 4, "caps": [
                    "transport.flow.v1", "transport.recovery.v1", SESSION_FLOW_V2_CAPABILITY,
                ]}}))
            await receive(type="hello-ok")
            receipts = {}
            for key in keys:
                await rpc("sessions.messages.subscribe", {"key": key, "fast_ack": True})
                part = await rpc("sessions.messages.snapshot.read", {
                    "key": key, "sync_revision": f"attempt-{key}",
                })
                assert part["segment_count"] == 1
                await rpc("transport.flow.update", {
                    "delivery_epoch": part["delivery"]["delivery_epoch"], "ack_delivery_id": 0,
                    "staged_delivery_ids": [part["delivery"]["delivery_id"]],
                })
                receipts[key] = {name: part[name] for name in (
                    "key", "sync_revision", "snapshot_id", "stream_generation",
                )} | {"stream_seq": part["current_stream_seq"]}
                for index in range(17):
                    streams.record(key, "session.event.text_delta", {"text": str(index)})

            proofs = {key: await rpc("sessions.messages.resume", receipts[key]) for key in keys}
            for expected_count in (8, 16, 17):
                for key in keys:
                    current = replay_frames(key)
                    assert len(current) == expected_count
                    # The first reply may be lost to an observer even though
                    # the socket delivered its first replay batch. Do not ACK
                    # until the exact resume has been repeated successfully.
                    repeated = await rpc("sessions.messages.resume", receipts[key])
                    assert repeated == proofs[key]
                    assert len(replay_frames(key)) == expected_count
                    lane = current[-1]["meta"]["session_flow_v2"]
                    await rpc("transport.sessionFlow.update.v2", {
                        "connection_epoch": lane["connection_epoch"],
                        "consumed": [{"subscription_epoch": lane["subscription_epoch"],
                                      "through_delivery_id": lane["delivery_id"]}],
                        "staged_recovery": [], "discarded_lanes": [],
                    })
            for key in keys:
                assert len(replay_frames(key)) == 17
                assert len({frame["payload"]["stream_seq"] for frame in replay_frames(key)}) == 17
                assert await rpc("sessions.messages.resume", receipts[key]) == proofs[key]
                await rpc("sessions.messages.snapshot.release", {
                    name: receipts[key][name] for name in ("key", "sync_revision", "snapshot_id")
                })
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        listener.close()
        await storage.close()
    assert get_transport_budget().used == initial_bytes
