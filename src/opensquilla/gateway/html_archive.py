"""HTTP lifetime adapter for shared bounded HTML ZIP exports."""

from __future__ import annotations

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from opensquilla.application.artifact_workbench import ContentMaterial
from opensquilla.artifact_archives import (
    HtmlZipExporter as HtmlZipExporter,
)
from opensquilla.artifact_archives import (
    capture_saved_html as capture_saved_html,
)


class TemporaryArchiveResponse(FileResponse):
    def __init__(self, material: ContentMaterial) -> None:
        assert material.path is not None and material.cleanup is not None
        self._cleanup = material.cleanup
        super().__init__(
            material.path,
            media_type=material.media_type,
            filename=material.filename,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()
