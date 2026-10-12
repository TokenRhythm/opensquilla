"""Exercise registered OpenSquilla tools on temporary, synthetic inputs."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_metaskill


def python_command(script: Path) -> str:
    """Quote a known Python executable and file for the native host shell."""
    if os.name == "nt":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        return f"& {quote(sys.executable)} {quote(script)}; exit $LASTEXITCODE"
    return shlex.join([sys.executable, str(script)])


class NativeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill tools 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace space"
        self.workspace.mkdir()
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.state = run_metaskill.isolated_state(self.workspace)
        self.state.__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        from metaskill_tools import build_tools, close_tools
        from opensquilla.tool_boundary import ToolCall

        self.ToolCall = ToolCall
        self.definitions, self.handler = build_tools(
            self.workspace, self.skills, config={"tools": {"run_mode": "full"}},
        )
        self.addCleanup(close_tools, self.handler)

    async def call(self, name: str, arguments: dict):
        return await self.handler(self.ToolCall(name, name, arguments))

    def test_registry_exposes_native_execution_and_document_tools(self) -> None:
        names = {tool.name for tool in self.definitions}
        self.assertTrue({
            "exec_command", "execute_code", "background_process", "process",
            "read_file", "write_file", "image", "pdf", "http_request", "web_fetch",
        } <= names, names)
        self.assertNotIn("run_python", names)

    async def test_write_and_numbered_read_use_workspace_relative_paths(self) -> None:
        text = 'First line 中文\nSecond "quoted" line\n'
        written = await self.call("write_file", {"path": "note.txt", "content": text})
        self.assertFalse(written.is_error, written.content)
        self.assertEqual((self.workspace / "note.txt").read_text(encoding="utf-8"), text)
        read = await self.call("read_file", {"path": "note.txt"})
        self.assertFalse(read.is_error, read.content)
        self.assertIn("First line 中文", read.content)
        self.assertIn('Second "quoted" line', read.content)

    async def test_execute_code_returns_native_json_and_artifact(self) -> None:
        result = await self.call("execute_code", {"code": (
            "from pathlib import Path\n"
            "Path('calculated.txt').write_text(str(6 * 7), encoding='utf-8')\n"
            "print('computed 中文')\n"
        )})
        self.assertFalse(result.is_error, result.content)
        payload = json.loads(result.content)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["stdout"].strip(), "computed 中文")
        self.assertFalse(payload["timed_out"])
        self.assertEqual((self.workspace / "calculated.txt").read_text(), "42")
        failed = await self.call("execute_code", {"code": "raise SystemExit(7)"})
        self.assertEqual(json.loads(failed.content)["exit_code"], 7)
        self.assertTrue(failed.is_error, failed.content)

    async def test_exec_command_preserves_stdin_cwd_and_nonzero_exit(self) -> None:
        script = self.workspace / "echo input 中文.py"
        script.write_text(
            "from pathlib import Path\nimport sys\n"
            "data = sys.stdin.read()\n"
            "Path('received.txt').write_text(data, encoding='utf-8')\n"
            "print(data)\nraise SystemExit(9)\n", encoding="utf-8",
        )
        text = 'Argument "quoted" 中文\nSecond line'
        result = await self.call("exec_command", {
            "command": python_command(script), "stdin": text,
            "workdir": str(self.workspace), "timeout": 15,
        })
        self.assertIn("exit_code=9", result.content)
        self.assertIn(text, result.content.replace("\r\n", "\n"))
        self.assertTrue(result.is_error, result.content)
        self.assertEqual((self.workspace / "received.txt").read_text(encoding="utf-8"), text)

    async def test_pdf_reads_only_requested_page_without_model(self) -> None:
        from reportlab.pdfgen.canvas import Canvas

        path = self.workspace / "synthetic pages.pdf"
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, "First synthetic page")
        canvas.showPage()
        canvas.drawString(72, 700, "Second synthetic page")
        canvas.save()
        result = await self.call("pdf", {"path": str(path), "pages": "2"})
        self.assertFalse(result.is_error, result.content)
        payload = json.loads(result.content)
        self.assertEqual(payload["total_pages"], 2)
        self.assertIn("Second synthetic page", payload["text"])
        self.assertNotIn("First synthetic page", payload["text"])

    async def test_image_decodes_local_resource_before_offline_vision_call(self) -> None:
        from PIL import Image

        path = self.workspace / "synthetic image.png"
        Image.new("RGB", (8, 8), "red").save(path)
        vision = AsyncMock(return_value="A red square.")
        with patch("opensquilla.tools.builtin.media._call_vision_provider", vision):
            result = await self.call("image", {"path": str(path), "prompt": "Describe the color."})
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(json.loads(result.content)["description"], "A red square.")
        vision.assert_awaited_once()
        encoded, media_type, prompt = vision.call_args.args
        self.assertTrue(encoded)
        self.assertEqual(media_type, "image/png")
        self.assertEqual(prompt, "Describe the color.")

    async def test_background_process_wait_and_log_keep_native_session_contract(self) -> None:
        script = self.workspace / "background task.py"
        script.write_text("import time\ntime.sleep(0.15)\nprint('background complete')\n", encoding="utf-8")
        started = await self.call("background_process", {
            "command": python_command(script), "workdir": str(self.workspace),
        })
        self.assertFalse(started.is_error, started.content)
        match = re.search(r"^session_id=(\S+)", started.content)
        self.assertIsNotNone(match, started.content)
        session_id = match.group(1)
        try:
            waited = await self.call("process", {
                "action": "wait", "session_id": session_id, "timeout": 15,
            })
            self.assertFalse(waited.is_error, waited.content)
            waited_payload = json.loads(waited.content)
            self.assertTrue(waited_payload["exited"])
            self.assertEqual(waited_payload["session"]["returncode"], 0)
            logged = await self.call("process", {"action": "log", "session_id": session_id})
            self.assertIn("background complete", logged.content)
        finally:
            await self.call("process", {"action": "kill", "session_id": session_id})
            await self.call("process", {"action": "remove", "session_id": session_id})


if __name__ == "__main__":
    unittest.main()
