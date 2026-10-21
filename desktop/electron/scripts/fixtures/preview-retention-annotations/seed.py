"""Seed a new isolated acceptance profile using the real Document registration path."""

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


async def main(config_path: Path) -> None:
    profile = config_path.resolve().parent
    config = GatewayConfig.load(config_path)
    database = Path(config.state_dir) / "sessions.db"
    if database.exists() or database.parent != profile / "state":
        raise ValueError("Fixture requires a new profile with empty profile/state")
    database.parent.mkdir(parents=True)
    manifest = []
    async with SessionStorage(database) as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=build_execution_workspace_factory(
                config, profile_home=profile
            ),
        )
        service = await ArtifactSessionService.from_session_storage(storage)
        for chat in ("A", "B"):
            key = f"agent:main:webchat:preview-acceptance-{chat.lower()}"
            session = await manager.create(
                key, display_name=f"Preview acceptance {chat}", last_channel="webchat"
            )
            root = Path(session.execution_workspace["root"])
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
            await manager.append_message(
                key,
                "user",
                "Synthetic preview and annotation acceptance fixtures. No provider calls.",
            )
            segments, paths = [], []
            for number in range(1, 5):
                label = f"{chat}{number}"
                folder = f"preview-{label.lower()}"
                (root / folder).mkdir()
                html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Preview {label}</title>
<style>body{{font:22px system-ui;padding:32px;background:#eef2ff;color:#172554}}
h1{{color:#4338ca}}button,input{{font:20px system-ui;padding:12px;margin:10px 10px 10px 0}}
.card{{background:white;padding:24px;border:2px solid #a5b4fc;border-radius:16px;margin:20px 0}}
</style></head><body><h1 id="heading">Preview {label}</h1>
<p id="description">Select this paragraph to annotate {label}.</p>
<div class="card"><button id="counter">Count 0</button>
<input id="draft" placeholder="Retained input {label}"></div>
<button id="hash">Change page section</button>
<button id="delayed-hash">Change section after two seconds</button>
<p id="footer">Independent retained page {label}</p>
<script>window.retainedCounter=0;
counter.onclick=()=>{{counter.textContent='Count '+(++window.retainedCounter)}};
hash.onclick=()=>location.hash='section-'+Date.now();
document.getElementById('delayed-hash').onclick=()=>{{
  document.getElementById('delayed-hash').textContent='Navigation scheduled';
  setTimeout(()=>location.hash='section-'+Date.now(),2000);
}};
</script></body></html>"""
                (root / folder / "index.html").write_text(html, encoding="utf-8")
                args = {
                    "path": f"{folder}/index.html",
                    "bundle": "directory",
                    "bundle_root": folder,
                }
                token = current_tool_context.set(context)
                try:
                    result = await open_workspace_preview(**args)
                finally:
                    current_tool_context.reset(token)
                segments.extend(
                    [
                        {
                            "type": "tool_use",
                            "tool_use_id": f"preview-{label}",
                            "name": "open_workspace_preview",
                            "input": args,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": f"preview-{label}",
                            "name": "open_workspace_preview",
                            "result": result,
                            "is_error": False,
                        },
                    ]
                )
                paths.append(f"`{folder}/index.html`")
                manifest.append(
                    {
                        "chat": chat,
                        "label": label,
                        "key": key,
                        "path": args["path"],
                        "workspace": str(root),
                        "result": result,
                    }
                )
            await manager.append_message(
                key,
                "assistant",
                "Open these retained preview fixtures:\n\n" + "\n\n".join(paths),
                tool_calls=segments,
            )
        await service.close()
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
