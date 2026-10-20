"""Loopback-only Usage browser fixture backed by temporary production SQLite.

The HTTP envelope is test infrastructure. Only usage.query is dispatched, using
the production RPC registry, authorization, handler, ledger query and serializer.
No provider, installed Gateway, real profile or chat execution is involved.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace


def create_app(database: Path):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext, get_dispatcher
    from opensquilla.session.models import SessionNode, TranscriptEntry
    from opensquilla.session.storage import SessionStorage
    from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventStart

    @asynccontextmanager
    async def lifespan(app):
        storage = await SessionStorage.open(str(database))
        try:
            now = int(time.time() * 1000) - 60_000

            async def call(node, run, cents, *, parent=None):
                start = UsageEventStart(
                    event_id=f"event-{run}", execution_id=run, call_index=0,
                    session_id=node.session_id, session_epoch=node.epoch,
                    turn_id=run, parent_turn_id=parent, root_turn_id=parent or run,
                    started_at_ms=now, provider="fixture", model="fixture/text",
                    run_kind="subagent" if parent else "agent",
                )
                await storage.start_usage_event(start)
                await storage.finalize_usage_event(start.event_id, UsageEventCompletion(
                    completed_at_ms=now + 100, input_tokens=10, output_tokens=2, total_tokens=12,
                    cost_nanos=cents * 10_000_000, billed_cost_nanos=cents * 10_000_000,
                    cost_source="provider_billed", provider="fixture", model="fixture/text",
                ))

            root = SessionNode(
                session_key="agent:main:webchat:derived", session_id="derived",
                display_name="WebChat",
            )
            child = SessionNode(
                session_key="agent:main:subagent:child", session_id="child",
                parent_session_key=root.session_key, display_name="不应成为额外账单的子任务",
                origin={
                    "kind": "subagent", "parent_session_id": root.session_id,
                    "parent_session_epoch": root.epoch,
                },
            )
            transcript = SessionNode(
                session_key="agent:main:webchat:transcript", session_id="transcript",
                display_name="WebChat",
            )
            manual = SessionNode(
                session_key="agent:main:webchat:manual", session_id="manual",
                display_name="手动命名任务", derived_title="不应覆盖手动名称",
            )
            retired = SessionNode(
                session_key="agent:main:webchat:reused", session_id="reused",
                display_name="WebChat",
            )
            for node in (root, child, transcript, manual, retired):
                await storage.upsert_session(node)
            await call(root, "root", 10)
            await call(child, "child", 20, parent="root")
            await call(transcript, "transcript", 20)
            await call(manual, "manual", 30)
            await call(retired, "retired", 40)
            # Reproduce pre-fix attribution snapshots, then improve only the
            # exact live owner. The retired epoch must not borrow its replacement.
            await storage.conn.execute(
                "UPDATE usage_attribution_owners SET title="
                "CASE WHEN session_id='derived' THEN 'WebChat' ELSE '' END "
                "WHERE session_id IN ('derived', 'reused')",
            )
            await storage.conn.commit()
            root.derived_title = "制作我的世界复刻游戏"
            await storage.upsert_session(root)
            await storage.append_transcript_entry(TranscriptEntry(
                session_id=transcript.session_id, session_key=transcript.session_key,
                role="user", content="整理发布检查清单",
            ))
            replacement = retired.model_copy(update={
                "epoch": 1, "derived_title": "新一代独立任务",
            })
            await storage.upsert_session(replacement)
            await call(replacement, "replacement", 50)
            app.state.context = RpcContext(
                conn_id="usage-browser-fixture",
                principal=Principal(
                    role="operator", scopes=frozenset({"operator.read"}),
                    is_owner=False, authenticated=True,
                ),
                session_manager=SimpleNamespace(storage=storage),
            )
            yield
        finally:
            await storage.close()

    async def health(_request):
        return JSONResponse({"ready": True, "source": "temporary-sqlite"})

    async def rpc(request):
        value = await request.json()
        if not isinstance(value, dict) or value.get("method") != "usage.query":
            return JSONResponse({"error": "Only usage.query is available"}, status_code=400)
        result = await get_dispatcher().dispatch(
            str(value.get("id", "usage-browser")), "usage.query", value.get("params"),
            request.app.state.context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    return Starlette(
        routes=[Route("/health", health), Route("/rpc", rpc, methods=["POST"])],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19087)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    temporary_root = repo / ".tmp" / "usage-browser"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gateway-", dir=temporary_root) as directory:
        root = Path(directory)
        # Set every profile root before importing any production module.
        for name, suffix in (
            ("OPENSQUILLA_STATE_DIR", "state"),
            ("OPENSQUILLA_LOG_DIR", "logs"),
            ("OPENSQUILLA_USER_STATE_DIR", "profile"),
        ):
            os.environ[name] = str(root / suffix)
        os.environ["OPENSQUILLA_TURN_CALL_LOG"] = "0"
        sys.path.insert(0, str(repo / "src"))
        import uvicorn

        uvicorn.run(
            create_app(root / "usage.sqlite"), host="127.0.0.1", port=args.port,
            log_level="warning", access_log=False,
        )


if __name__ == "__main__":
    main()
