"""MetaSkill Agent terminal-error contracts with synthetic local inputs."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opensquilla.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent, ToolResultEvent
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.executors.agent import run_step_with_skill_stream
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.types import SkillLayer, SkillSpec


class AgentTerminalErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill agent terminal ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        environment = patch.dict(os.environ, {"OPENSQUILLA_STATE_DIR": str(self.directory / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        specification = SkillSpec("synthetic-skill", "Synthetic local instructions.",
                                  SkillLayer.WORKSPACE, False, [], "Synthetic instructions.",
                                  path=self.directory / "SKILL.md")
        self.loader = SimpleNamespace(get_by_name=lambda _name: specification)
        self.step = MetaStep(id="synthetic-step", skill="synthetic-skill")

    def execute(self, events, *, drained=None):
        async def runner(_system_prompt, _user_message):
            for event in events:
                yield event
            if drained is not None:
                drained.append(True)

        return run_step_with_skill_stream(self.step, "synthetic-skill", {}, {},
                                          agent_runner=runner, skill_loader=self.loader)

    async def test_terminal_error_followed_by_partial_usage_done_is_not_success(self):
        drained = []
        forwarded = []
        terminal = ErrorEvent(message="Synthetic provider request failed", code="transport")
        with self.assertRaisesRegex(RuntimeError, "sub-agent failed"):
            async for event in self.execute([
                TextDeltaEvent(text="An unfinished action plan."), terminal,
                DoneEvent(text="An unfinished action plan.", input_tokens=11, output_tokens=7),
            ], drained=drained):
                forwarded.append(event)
        self.assertEqual(drained, [True], "Consume native accounting events before failing the step")
        self.assertIn(terminal, forwarded)
        self.assertFalse(any(isinstance(event, _StepDone) for event in forwarded))

    async def test_terminal_error_fails_with_or_without_partial_text_and_done(self):
        for prefix in ([], [TextDeltaEvent(text="Incomplete output")]):
            with self.subTest(partial=bool(prefix)):
                with self.assertRaisesRegex(RuntimeError, "Synthetic terminal error"):
                    async for _ in self.execute(prefix + [ErrorEvent(message="Synthetic terminal error")]):
                        pass

    async def test_successful_done_keeps_its_authoritative_snapshot(self):
        events = [event async for event in self.execute([
            TextDeltaEvent(text="Intermediate progress."),
            DoneEvent(text="Completed synthetic result.", text_snapshot="Completed synthetic result."),
        ])]
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], _StepDone)
        self.assertEqual(events[0].text, "Completed synthetic result.")
        self.assertEqual(events[0].status, "ok")

    async def test_recovered_tool_error_does_not_fail_a_completed_agent(self):
        events = [event async for event in self.execute([
            ToolResultEvent(tool_name="read_file", result="Synthetic missing file", is_error=True),
            DoneEvent(text="Recovered and completed."),
        ])]
        self.assertIsInstance(events[-1], _StepDone)
        self.assertEqual(events[-1].text, "Recovered and completed.")

    async def test_terminal_error_message_is_redacted_before_persistence(self):
        with self.assertRaises(RuntimeError) as failure:
            async for _ in self.execute([
                ErrorEvent(message="Synthetic error Bearer local-secret and sk-synthetic-secret"),
                DoneEvent(text="Partial text", input_tokens=11),
            ]):
                pass
        self.assertNotIn("local-secret", str(failure.exception))
        self.assertNotIn("sk-synthetic-secret", str(failure.exception))


class PartialStreamServer:
    """A real SSE tool turn, then a visible partial stream plus terminal error."""

    def __init__(self):
        self.requests = []
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer local-test-key":
                        raise AssertionError("Expected the explicit loopback deployment")
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    owner.requests.append(payload)
                    index = len(owner.requests)
                    if index == 1:
                        delta = {"content": "Synthetic progress before writing. ", "tool_calls": [{
                            "index": 0, "id": "synthetic-write", "type": "function",
                            "function": {"name": "write_file", "arguments": json.dumps({
                                "path": "synthetic-partial.txt", "content": "Synthetic retained file",
                            })},
                        }]}
                        chunks = [
                            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
                        ]
                    elif index == 2:
                        chunks = [
                            {"choices": [{"index": 0, "delta": {"content": "An unfinished synthetic action plan."},
                                          "finish_reason": None}]},
                            {"error": {"message": "Synthetic request error", "code": "transport_error"}},
                        ]
                    else:
                        raise AssertionError("The visible failed stream must not be replayed")
                    body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                            + "data: [DONE]\n\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except Exception as error:
                    owner.errors.append(str(error))
                    self.send_error(400, str(error))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class NativeAgentPartialStreamTests(unittest.TestCase):
    def test_failed_agent_keeps_native_usage_and_partial_files_without_completing_dependents(self):
        with tempfile.TemporaryDirectory(prefix="metaskill native error 中文 ") as directory:
            root = Path(directory)
            skills = root / "skills"
            workspace = root / "workspace"
            trace = workspace / "trace.json"
            child = skills / "synthetic-agent"
            meta = skills / "synthetic-meta"
            child.mkdir(parents=True)
            meta.mkdir()
            (child / "SKILL.md").write_text(
                "---\nname: synthetic-agent\ndescription: A synthetic writer.\n---\n"
                "Write synthetic-partial.txt and finish the task.\n", encoding="utf-8")
            (meta / "SKILL.md").write_text(
                "---\nname: synthetic-meta\ndescription: A synthetic error test.\nkind: meta\n"
                'final_text_mode: "raw"\ncomposition:\n  steps:\n'
                "    - id: work\n      kind: agent\n      skill: synthetic-agent\n"
                '      with:\n        task: "{{ inputs.user_message }}"\n'
                "    - id: dependent\n      kind: tool_call\n      tool: write_file\n"
                "      depends_on: [work]\n      tool_args:\n        path: dependent.txt\n"
                "        content: Should not run after a terminal error.\n---\n", encoding="utf-8")
            environment = {key: value for key, value in os.environ.items()
                           if not key.upper().endswith(("_API_KEY", "_TOKEN"))
                           and "PROXY" not in key.upper() and not key.upper().startswith("OPENSQUILLA_")}
            environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="*",
                               METASKILL_LOCAL_TEST_KEY="local-test-key")
            with PartialStreamServer() as server:
                config = root / "loopback.toml"
                config.write_text(
                    '[model]\nprovider="openrouter"\nname="synthetic-agent-model"\n'
                    f'base_url="http://127.0.0.1:{server.server.server_port}/v1"\n'
                    'api_key_env="METASKILL_LOCAL_TEST_KEY"\n[runtime]\nmax_iterations=4\n'
                    'max_tokens=256\ntimeout_seconds=30\nrequest_timeout=15\n', encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/run_metaskill.py"), "--skills-dir", str(skills),
                     "--name", "synthetic-meta", "--workspace", str(workspace), "--config", str(config),
                     "--run-mode", "full", "--trace-json", str(trace)],
                    cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8",
                    timeout=60, check=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(server.errors, [])
                self.assertEqual(len(server.requests), 2)
            self.assertIn("MetaSkill failed at work", result.stderr)
            self.assertNotIn("local-test-key", result.stdout + result.stderr)
            self.assertEqual((workspace / "synthetic-partial.txt").read_text(encoding="utf-8"), "Synthetic retained file")
            self.assertFalse((workspace / "dependent.txt").exists())
            payload = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            work = next(step for step in payload["steps"] if step["step_id"] == "work")
            self.assertEqual(work["status"], "failed")
            self.assertIn("sub-agent failed", work["error"])
            usage = work["provider_usage"]
            self.assertEqual(usage["call_count"], 2)
            self.assertEqual(usage["known_usage_calls"], 1)
            self.assertEqual(usage["unknown_usage_calls"], 1)
            self.assertEqual(usage["known_token_subtotals"]["input_tokens"], 11)
            self.assertEqual(usage["known_token_subtotals"]["output_tokens"], 7)
            self.assertEqual(usage["coverage"], "partial")
            self.assertIsNone(usage["total_tokens"])
            self.assertEqual(len(list((workspace / ".metaskill-state").glob("usage-*.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()
