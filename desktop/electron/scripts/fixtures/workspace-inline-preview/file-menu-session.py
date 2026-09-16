"""Seed a fresh, explicit test profile with real file registrations; no model calls.

Usage: PYTHONPATH=src python file-menu-session.py /path/to/new/profile/config.toml
Do not run against an existing profile. The normal Gateway serves the fixture.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.workspace_preview import open_workspace_preview
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


async def main(config_path: Path):
    profile = config_path.resolve().parent
    config = GatewayConfig.load(config_path)
    database = Path(config.state_dir) / "sessions.db"
    if database.exists() or database.parent != profile / "state":
        raise ValueError("Use an empty, dedicated fixture profile with profile/state")
    database.parent.mkdir(parents=True)
    key = "agent:main:webchat:file-menu-fixture"
    async with SessionStorage(database) as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=(
                build_execution_workspace_factory(config, profile_home=profile)
            ),
        )
        session = await manager.create(key, display_name="文件菜单固定样例", last_channel="webchat")
        root = Path(session.execution_workspace["root"])
        service = await ArtifactSessionService.from_session_storage(storage)
        adopter = GeneratedArtifactAdopter(
            service=service,
            store=ArtifactStore(profile / "media"),
            session_key=key,
            session_id=session.session_id,
            workspace=str(root),
        )
        context = ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key=key,
            session_id=session.session_id,
            workspace_dir=str(root),
            artifact_session_id=session.session_id,
            artifact_media_root=str(profile / "media"),
            workspace_preview_opener=adopter.open_workspace_preview,
        )
        await manager.append_message(key, "user", "固定文件菜单样例，无模型调用。")
        segments = []
        for site in ("one", "two"):
            (root / site).mkdir()
            (root / site / "style.css").write_text(
                "body{font:24px system-ui;padding:32px}h1{color:#3563ca}"
            )
            for page in ("index", "editorial", "dashboard", "minimal"):
                links = " · ".join(
                    f'<a href="{name}.html">{name}</a>'
                    for name in ("index", "editorial", "dashboard", "minimal")
                )
                (root / site / f"{page}.html").write_text(
                    "<!doctype html><html><head><meta charset=\"utf-8\">"
                    '<link rel="stylesheet" href="style.css"></head>'
                    f"<body><h1>{site}/{page}</h1>"
                    f"<p>当前子页的固定内容。UTF-8 北京。</p>{links}</body></html>"
                )
            args = {"path": f"{site}/index.html", "bundle": "directory", "bundle_root": site}
            segments.append(
                {
                    "type": "tool_use",
                    "tool_use_id": f"preview-{site}",
                    "name": "open_workspace_preview",
                    "input": args,
                }
            )
            token = current_tool_context.set(context)
            try:
                result = await open_workspace_preview(**args)
            finally:
                current_tool_context.reset(token)
            segments.append(
                {
                    "type": "tool_result",
                    "tool_use_id": f"preview-{site}",
                    "name": "open_workspace_preview",
                    "result": result,
                    "is_error": False,
                }
            )
        await manager.append_message(
            key,
            "assistant",
            "文件菜单固定样例：\n\n"
            "| 站点 | 首页 | 编辑风 | 仪表盘 | 极简 |\n|---|---|---|---|---|\n"
            "| one | `one/index.html` | `one/editorial.html` | `one/dashboard.html` "
            "| `one/minimal.html` |\n"
            "| two | `two/index.html` | `two/editorial.html` | `two/dashboard.html` "
            "| `two/minimal.html` |\n\n"
            "普通文字与 [外部网址](https://example.com) 保留原生右键。",
            tool_calls=segments,
        )
        await service.close()
    print(json.dumps({"sessionKey": key, "workspace": str(root)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
