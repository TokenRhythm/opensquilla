"""Run the real Agent loop and DAG with native tools and an offline model stream."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_metaskill


class AgentToolTests(unittest.TestCase):
    def test_complete_skills_use_native_tools_and_pass_text_and_files_downstream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="metaskill agent 中文 ") as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            config = Path(directory) / "model.toml"
            config.write_text(
                '[model]\nname="offline-test-model"\n'
                'base_url="https://unused.invalid/v1"\n'
                'api_key_env="METASKILL_TEST_API_KEY"\n'
                '[runtime]\nmax_iterations=8\nmax_tokens=256\n'
                '[tools]\nrun_mode="full"\n', encoding="utf-8",
            )
            args = run_metaskill.arguments([
                "--name", "meta-writing-demo", "--config", str(config),
                "--workspace", str(workspace), "--input", "The study group meets tomorrow.",
            ])
            calls = []
            with run_metaskill.isolated_state(workspace):
                from opensquilla.provider.openai import OpenAIProvider
                from opensquilla.provider.types import (
                    ContentBlockToolResult, DoneEvent, TextDeltaEvent,
                    ToolUseEndEvent, ToolUseStartEvent,
                )

                async def local_stream(_provider, messages, *, tools=None, config=None):
                    calls.append((messages.copy(), config.system))
                    call = len(calls)
                    names = {tool.name for tool in tools}
                    self.assertTrue({"read_file", "write_file", "execute_code", "exec_command", "process"} <= names)
                    if call in (1, 4):
                        relative = (
                            "announcement-draft/templates/announcement.txt" if call == 1
                            else "announcement-edit/references/style-guide.txt"
                        )
                        path = ROOT / "examples" / "skills" / relative
                        self.assertIn(str(path.parent.parent), config.system)
                        self.assertNotIn("{baseDir}", config.system)
                        if call == 4:
                            self.assertIn("DRAFT FROM FIRST SKILL", str(messages))
                        name, arguments = "read_file", {"path": str(path)}
                    elif call == 2:
                        self.assert_tool_result(messages, ContentBlockToolResult)
                        name, arguments = "execute_code", {"code": (
                            "from pathlib import Path\n"
                            "Path('handoff.txt').write_text('shared artifact 中文', encoding='utf-8')\n"
                            "print('artifact ready')\n"
                        )}
                    elif call == 5:
                        self.assert_tool_result(messages, ContentBlockToolResult)
                        name, arguments = "read_file", {"path": "handoff.txt"}
                    elif call in (3, 6):
                        result = self.assert_tool_result(messages, ContentBlockToolResult)
                        self.assertIn("artifact ready" if call == 3 else "shared artifact 中文", result.content)
                        yield TextDeltaEvent(text="DRAFT FROM FIRST SKILL" if call == 3 else "FINAL EDITED ANNOUNCEMENT")
                        yield DoneEvent(stop_reason="stop", model="offline-test-model")
                        return
                    else:
                        self.fail(f"Unexpected extra provider call: {call}")
                    yield ToolUseStartEvent(tool_use_id=f"tool-{call}", tool_name=name)
                    yield ToolUseEndEvent(tool_use_id=f"tool-{call}", tool_name=name, arguments=arguments)
                    yield DoneEvent(stop_reason="tool_use", model="offline-test-model")

                output = io.StringIO()
                with patch.dict(os.environ, {"METASKILL_TEST_API_KEY": "test-key-only"}), \
                        patch.object(OpenAIProvider, "chat", local_stream), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(asyncio.run(run_metaskill.run(args)), 0)
                self.assertEqual(len(calls), 6)
                self.assertIn("FINAL EDITED ANNOUNCEMENT", output.getvalue())
                self.assertEqual((workspace / "handoff.txt").read_text(encoding="utf-8"), "shared artifact 中文")
                self.assertNotIn("test-key-only", output.getvalue())

    def assert_tool_result(self, messages, result_type):
        results = [
            block for message in messages if isinstance(message.content, list)
            for block in message.content if isinstance(block, result_type)
        ]
        self.assertTrue(results, "Agent must return native tool results to the provider")
        self.assertFalse(results[-1].is_error, str(results[-1]))
        self.assertTrue(results[-1].content)
        return results[-1]


if __name__ == "__main__":
    unittest.main()
