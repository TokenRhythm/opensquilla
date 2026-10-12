"""Offline benchmark-host contracts with synthetic data and loopback HTTP only."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from metaskill_trace import LocalUsageSink, _expanded_trace_inputs, open_trace, summarize_provider_usage
import run_metaskill


class ReceiptServer:
    """Exercise the real OpenRouter adapter, including native usage receipts."""

    def __init__(self, *, receipts=True, agent_tools=False, stream_error=False):
        self.requests = []
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != "/v1/chat/completions":
                        raise AssertionError(f"Unexpected endpoint: {self.path}")
                    if self.headers.get("Authorization") != "Bearer local-test-key":
                        raise AssertionError("Synthetic deployment key was not passed")
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if payload.get("model") != "synthetic-benchmark-model" or not payload.get("stream"):
                        raise AssertionError("Expected explicit model and streaming request")
                    owner.requests.append(payload)
                    index = len(owner.requests)
                    if agent_tools and index == 1:
                        delta = {"tool_calls": [{
                            "index": 0, "id": "synthetic-call-1", "type": "function",
                            "function": {"name": "write_file", "arguments": json.dumps({
                                "path": "synthetic.txt", "content": "synthetic artifact 中文",
                            }, ensure_ascii=False)},
                        }]}
                        finish = "tool_calls"
                    else:
                        delta = {"content": f"SYNTHETIC RESPONSE {index} 中文"}
                        finish = "stop"
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    ]
                    if stream_error:
                        chunks = chunks[:2] + [{"error": {
                            "message": "synthetic response failed", "code": "invalid_request_error",
                        }}]
                    elif receipts:
                        chunks.append({"choices": [], "usage": {
                            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                            "completion_tokens_details": {"reasoning_tokens": 2},
                            "prompt_tokens_details": {"cached_tokens": 3}, "cost": 0.0123,
                        }})
                    body = ("".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
                            + "data: [DONE]\n\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except Exception as exc:
                    owner.errors.append(str(exc))
                    self.send_error(400, str(exc))

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


class BenchmarkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="benchmark host 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.workspace = self.directory / "workspace 中文"
        self.trace = self.workspace / "trace.json"

    def cli(self, name="meta-text-demo", *, extra=(), config=None, skills=None):
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_API_KEY", "_TOKEN"))
            and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
            and not key.upper().startswith("OPENSQUILLA_")
        }
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="*",
                   METASKILL_LOCAL_TEST_KEY="local-test-key")
        command = [sys.executable, str(ROOT / "scripts" / "run_metaskill.py"),
                   "--skills-dir", str(skills or ROOT / "examples" / "skills"),
                   "--name", name, "--workspace", str(self.workspace), "--run-mode", "full",
                   "--trace-json", str(self.trace)]
        if config is not None:
            command.extend(["--config", str(config)])
        return subprocess.run(command + list(extra), cwd=self.directory, env=env, text=True,
                              encoding="utf-8", capture_output=True, timeout=90, check=False)

    def success(self, result, server=None):
        detail = result.stdout + result.stderr
        if server is not None:
            detail += "\n" + "\n".join(server.errors)
            self.assertFalse(server.errors, detail)
        self.assertEqual(result.returncode, 0, detail)
        self.assertNotIn("local-test-key", result.stdout + result.stderr)
        return json.loads(self.trace.read_text(encoding="utf-8"))

    @contextmanager
    def model(self, **options):
        with ReceiptServer(**options) as server:
            config = self.directory / "loopback.toml"
            config.write_text(
                '[model]\nprovider="openrouter"\nname="synthetic-benchmark-model"\n'
                f'base_url="http://127.0.0.1:{server.server.server_port}/v1"\n'
                'api_key_env="METASKILL_LOCAL_TEST_KEY"\n'
                '[runtime]\nmax_iterations=4\nmax_tokens=256\nrequest_timeout=15\n'
                'timeout_seconds=30\n[tools]\nrun_mode="full"\n', encoding="utf-8",
            )
            yield server, config

    def test_utf8_input_file_preserves_multiline_task_and_is_exclusive(self):
        task = 'A first line 中文\nAnother line with "quotes" and $literal\n'
        source = self.directory / "任务 input.txt"
        source.write_text(task, encoding="utf-8-sig")
        result = self.cli(extra=("--input-file", str(source)))
        payload = self.success(result)
        self.assertEqual(payload["inputs"]["user_message"], task)
        self.assertEqual(payload["final_text"], f"UPPER: {task.upper().rstrip()}\nlower: {task.lower().rstrip()}")
        self.assertEqual(payload["provider_usage"]["coverage"], "not_applicable")
        self.assertIsNone(payload["provider_usage"]["total_tokens"])
        self.assertEqual(payload["provider_calls"], [])
        rejected = self.cli(extra=("--input-file", str(source), "--input", "conflicting task"))
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("not allowed with argument --input-file", rejected.stderr)

    def test_explicit_trace_limit_preserves_large_rendered_inputs_and_outputs(self):
        text = "Large synthetic 中文 line\n" * 3500
        source = self.directory / "large.txt"
        source.write_text(text, encoding="utf-8")
        result = self.cli(extra=("--input-file", str(source), "--max-trace-field-bytes", "524288"))
        payload = self.success(result)
        steps = {step["step_id"]: step for step in payload["steps"]}
        self.assertEqual(steps["upper"]["rendered_inputs"]["text"], text)
        self.assertEqual(steps["merge"]["rendered_inputs"]["upper"], steps["upper"]["output_text"])
        self.assertEqual(steps["merge"]["rendered_inputs"]["lower"], steps["lower"]["output_text"])
        self.assertFalse(steps["merge"]["rendered_inputs_truncated"])
        self.assertGreater(len(steps["merge"]["output_text"].encode("utf-8")), 64 * 1024)
        self.assertEqual(payload["trace_limits"]["max_field_bytes"], 524288)
        self.assertLess(len(payload["inputs"]["user_message"].encode("utf-8")), 4200)

    def test_expanded_projection_redacts_secrets_and_marks_over_limit(self):
        projection, truncated = _expanded_trace_inputs({
            "task": "A" * 70000, "api_key": "synthetic-secret-value",
            "nested": {"authorization": "Bearer synthetic-value", "ordinary": "sk-synthetic-value"},
            "paid_submission_receipt_proofs": {"do_not_persist": True},
        }, 100000)
        self.assertFalse(truncated)
        self.assertEqual(len(projection["task"]), 70000)
        self.assertEqual(projection["api_key"], "[REDACTED]")
        self.assertEqual(projection["nested"], {"authorization": "[REDACTED]", "ordinary": "[REDACTED]"})
        self.assertNotIn("paid_submission_receipt_proofs", projection)
        clipped, truncated = _expanded_trace_inputs({"task": "中文" * 100}, 32)
        self.assertTrue(truncated)
        self.assertTrue(clipped["__trace_field_truncated__"])
        self.assertGreater(clipped["original_utf8_bytes"], 32)

    def test_trace_limit_validation_is_before_creating_state(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit):
                args = run_metaskill.arguments(["--workspace", str(self.workspace)])
                args.max_trace_field_bytes = limit
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    asyncio.run(run_metaskill.run(args))
                self.assertFalse(self.workspace.exists())
        args = run_metaskill.arguments(["--workspace", str(self.workspace), "--max-trace-field-bytes", "100"])
        with self.assertRaisesRegex(ValueError, "requires --trace-json"):
            asyncio.run(run_metaskill.run(args))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            with open_trace(self.workspace, self.trace, max_field_bytes=0):
                pass
        self.assertFalse(self.workspace.exists())

    def test_direct_model_calls_account_once_and_attach_to_their_dag_steps(self):
        with self.model() as (server, config):
            payload = self.success(self.cli("meta-chat-demo", config=config), server)
            self.assertEqual(len(server.requests), 2)
            self.assertTrue(all(request["stream_options"]["include_usage"] for request in server.requests))
        usage = payload["provider_usage"]
        self.assertEqual((usage["call_count"], usage["input_tokens"], usage["output_tokens"]), (2, 22, 14))
        self.assertEqual((usage["reasoning_tokens"], usage["cache_read_tokens"]), (4, 6))
        self.assertEqual(usage["coverage"], "complete")
        self.assertAlmostEqual(usage["cost_usd"], 0.0246)
        self.assertEqual(len({call["call"]["event_id"] for call in payload["provider_calls"]}), 2)
        for step in payload["steps"]:
            self.assertEqual(step["provider_usage"]["call_count"], 1)
            self.assertEqual(step["provider_usage"]["total_tokens"], 18)
            self.assertTrue(step["usage"])
        usage_files = list((self.workspace / ".metaskill-state").glob("usage-*.jsonl"))
        self.assertEqual(len(usage_files), 1)
        rows = [json.loads(line) for line in usage_files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 4)  # start + terminal per physical request
        self.assertTrue(all("messages" not in row for row in rows))

    def test_agent_tool_loop_accounts_each_physical_request_without_duplicate_envelopes(self):
        skills = self.directory / "synthetic skills"
        agent = skills / "synthetic-writer"
        meta = skills / "synthetic-meta"
        agent.mkdir(parents=True)
        meta.mkdir()
        (agent / "SKILL.md").write_text(
            '---\nname: synthetic-writer\ndescription: Write a synthetic file.\n---\n'
            'Use write_file for synthetic.txt and return a short message.\n', encoding="utf-8",
        )
        (meta / "SKILL.md").write_text(
            '---\nname: synthetic-meta\ndescription: Exercise one synthetic Agent.\nkind: meta\n'
            'final_text_mode: "step:write"\ncomposition:\n  steps:\n    - id: write\n'
            '      kind: agent\n      skill: synthetic-writer\n      with:\n'
            '        task: "{{ inputs.user_message }}"\n---\n', encoding="utf-8",
        )
        with self.model(agent_tools=True) as (server, config):
            payload = self.success(self.cli("synthetic-meta", skills=skills, config=config), server)
            self.assertEqual(len(server.requests), 2)
        self.assertEqual((self.workspace / "synthetic.txt").read_text(encoding="utf-8"), "synthetic artifact 中文")
        self.assertEqual(payload["provider_usage"]["call_count"], 2)
        self.assertAlmostEqual(payload["provider_usage"]["cost_usd"], 0.0246)
        self.assertEqual(payload["steps"][0]["provider_usage"]["call_count"], 2)
        self.assertEqual(payload["steps"][0]["provider_usage"]["total_tokens"], 36)
        self.assertTrue(payload["steps"][0]["usage"])

    def test_missing_provider_receipts_are_unknown_not_free_or_zero_token_success(self):
        with self.model(receipts=False) as (server, config):
            payload = self.success(self.cli("meta-chat-demo", config=config), server)
            self.assertEqual(len(server.requests), 2)
        usage = payload["provider_usage"]
        self.assertEqual(usage["call_count"], 2)
        self.assertEqual(usage["unknown_usage_calls"], 2)
        self.assertFalse(usage["usage_available"])
        self.assertFalse(usage["cost_available"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["cost_usd"])
        self.assertEqual(usage["coverage"], "partial")

    def test_failed_partial_stream_persists_unknown_usage_and_failed_trace(self):
        with self.model(stream_error=True) as (server, config):
            result = self.cli("meta-chat-demo", config=config)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(server.errors)
            self.assertEqual(len(server.requests), 1)
        payload = json.loads(self.trace.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["steps"][0]["status"], "failed")
        self.assertEqual(payload["provider_calls"][0]["status"], "unknown")
        self.assertEqual(payload["provider_usage"]["unknown_usage_calls"], 1)
        self.assertIsNone(payload["provider_usage"]["cost_usd"])
        self.assertIsNone(payload["provider_usage"]["total_tokens"])
        self.assertEqual(payload["steps"][0]["provider_usage"]["call_count"], 1)

    def test_usage_sink_is_idempotent_and_mixed_unavailable_cost_stays_unknown(self):
        from opensquilla.engine.usage import usage_scope
        from opensquilla.engine.usage_accounting import (
            UsageAccountingScope, UsageCallItem, UsageCallResult, UsageExecutionContext,
        )

        destination = self.directory / "local usage.jsonl"
        sink = LocalUsageSink(destination)
        scope = UsageAccountingScope(sink=sink, context=UsageExecutionContext(
            execution_id="synthetic-execution", agent_run_id="synthetic-run", turn_id=None,
            session_id=None, agent_id="synthetic", run_kind="metaskill_cli",
        ))
        call = scope.new_call(provider="openrouter", model="synthetic")
        item = UsageCallItem(ordinal=0, provider="synthetic", model="one", input_tokens=5,
                             output_tokens=3, reasoning_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                             billed_cost_nanos=0, estimated_cost_nanos=0, cost_source="unavailable",
                             estimate_basis=None, price_source=None)
        result = UsageCallResult(completed_at_ms=1, input_tokens=5, output_tokens=3, reasoning_tokens=0,
                                 cache_read_tokens=0, cache_write_tokens=0, billed_cost_nanos=100,
                                 estimated_cost_nanos=0, cost_source="mixed", estimate_basis=None,
                                 price_source=None, items=(item,))

        async def record():
            with usage_scope("synthetic-run:step"):
                await sink.start(call)
                await sink.finalize(call, result)
                await sink.finalize(call, result)
                await sink.mark_unknown(call, "synthetic-late-cancellation")

        asyncio.run(record())
        rows = sink.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "finalized")
        self.assertEqual(rows[0]["scope_key"], "synthetic-run:step")
        self.assertEqual(len(destination.read_text(encoding="utf-8").splitlines()), 2)
        summary = summarize_provider_usage(rows)
        self.assertEqual(summary["total_tokens"], 8)  # envelope, not envelope + item
        self.assertFalse(summary["cost_available"])
        self.assertIsNone(summary["cost_usd"])

    def test_cleanup_stops_only_owned_native_background_tree_and_its_child(self):
        from metaskill_tools import build_tools, aclose_tools
        from opensquilla.tool_boundary import ToolCall
        from opensquilla.tools.builtin.shell import get_bg_session
        from opensquilla.tools.types import current_tool_context

        def visible_session(handler, session_id):
            token = current_tool_context.set(handler._metaskill_context)
            try:
                return get_bg_session(session_id)
            finally:
                current_tool_context.reset(token)

        def python_command(script):
            if os.name == "nt":
                quote = lambda value: "'" + str(value).replace("'", "''") + "'"
                return f"& {quote(sys.executable)} {quote(script)}; exit $LASTEXITCODE"
            return shlex.join([sys.executable, str(script)])

        def pid_running(pid):
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                handle = kernel.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                try:
                    code = wintypes.DWORD()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    return code.value == 259
                finally:
                    kernel.CloseHandle(handle)
            try:
                os.kill(pid, 0)
                stat = Path(f"/proc/{pid}/stat")
                return not (stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"))
            except ProcessLookupError:
                return False

        async def check():
            skills = self.directory / "synthetic background skills"
            skills.mkdir()
            foreign_workspace = self.directory / "foreign handler workspace"
            foreign_workspace.mkdir()
            self.workspace.mkdir()
            handlers = []
            try:
                with run_metaskill.isolated_state(self.workspace):
                    _, own = build_tools(self.workspace, skills, config={"tools": {"run_mode": "full"}})
                    handlers.append(own)
                    _, foreign = build_tools(foreign_workspace, skills, config={"tools": {"run_mode": "full"}})
                    handlers.append(foreign)
                    sessions = []
                    for handler, workspace in ((own, self.workspace), (foreign, foreign_workspace)):
                        script = workspace / "synthetic parent.py"
                        script.write_text(
                            "import subprocess, sys, time\nfrom pathlib import Path\n"
                            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                            "Path('child.pid').write_text(str(child.pid), encoding='ascii')\n"
                            "time.sleep(60)\n", encoding="utf-8",
                        )
                        result = await handler(ToolCall("synthetic-start", "background_process", {
                            "command": python_command(script), "workdir": str(workspace), "timeout": 90,
                        }))
                        self.assertFalse(result.is_error, result.content)
                        match = re.search(r"^session_id=(\S+)", result.content)
                        self.assertIsNotNone(match, result.content)
                        session = visible_session(handler, match.group(1))
                        self.assertIsNotNone(session)
                        for _ in range(100):
                            if (workspace / "child.pid").exists():
                                break
                            await asyncio.sleep(0.05)
                        child_pid = int((workspace / "child.pid").read_text(encoding="ascii"))
                        self.assertTrue(pid_running(child_pid))
                        sessions.append((session, child_pid))
                    own_session, own_child = sessions[0]
                    foreign_session, foreign_child = sessions[1]
                    await aclose_tools(own)
                    self.assertIsNone(visible_session(own, own_session.session_id))
                    self.assertIsNotNone(own_session.process.returncode)
                    for _ in range(100):
                        if not pid_running(own_child):
                            break
                        await asyncio.sleep(0.05)
                    self.assertFalse(pid_running(own_child), "Owned background descendant survived cleanup")
                    self.assertIs(visible_session(foreign, foreign_session.session_id), foreign_session)
                    self.assertIsNone(foreign_session.process.returncode)
                    self.assertTrue(pid_running(foreign_child), "Cleanup affected another handler's child")
                    self.assertIsNone(own._metaskill_approval_queue)
                    await aclose_tools(own)  # repeat close is harmless
            finally:
                for handler in handlers:
                    await aclose_tools(handler)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
