"""Exercise documented examples through the CLI and a local HTTP/SSE provider.

All inputs, credentials, and model replies in this module are synthetic.
The server binds only to loopback; no real provider is contacted.
"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "run_metaskill.py"


class LocalModelServer:
    """A minimal OpenAI-compatible streaming endpoint for real adapter tests."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != "/v1/chat/completions":
                        raise AssertionError(f"Unexpected provider endpoint: {self.path}")
                    if self.headers.get("Authorization") != "Bearer local-test-key":
                        raise AssertionError("The explicitly configured synthetic key was not used")
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    if payload.get("model") != "local-demo-model" or payload.get("stream") is not True:
                        raise AssertionError("Expected the configured model and streaming request")
                    owner.requests.append(payload)
                    result = owner.responder(payload, len(owner.requests))
                    if result[0] == "text":
                        delta = {"content": result[1]}
                        finish = "stop"
                    else:
                        _, name, arguments = result
                        delta = {"tool_calls": [{
                            "index": 0,
                            "id": f"local-call-{len(owner.requests)}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                        }]}
                        finish = "tool_calls"
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    ]
                    body = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
                    body = (body + "data: [DONE]\n\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except Exception as exc:
                    owner.errors.append(str(exc))
                    body = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

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


def request_text(payload):
    return json.dumps(payload.get("messages", []), ensure_ascii=False)


def system_text(payload):
    return str(next(item["content"] for item in payload["messages"] if item["role"] == "system"))


def normalized_path(value):
    return str(value).replace("\\", "/")


def assert_native_tools(payload):
    names = {item["function"]["name"] for item in payload.get("tools", [])}
    if not {"read_file", "write_file", "exec_command", "execute_code"} <= names:
        raise AssertionError("The full native tool definitions were not sent to the model")


class DocumentedModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="documented modes 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.skills = self.directory / "Skill packages 中文"
        shutil.copytree(ROOT / "examples" / "skills", self.skills)
        self.workspace = self.directory / "workspace 中文"

    def cli(self, name, *, text="Hello MetaSkill 中文", config=None, extra_args=()):
        # Ensure inherited provider credentials and proxies cannot select or
        # authenticate an external endpoint in these offline tests.
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_API_KEY", "_TOKEN"))
            and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
            and not key.upper().startswith("OPENSQUILLA_")
        }
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="*", METASKILL_LOCAL_TEST_KEY="local-test-key")
        argv = [sys.executable, str(CLI), "--skills-dir", str(self.skills),
                "--name", name, "--input", text, "--workspace", str(self.workspace), "--run-mode", "full"]
        if config is not None:
            argv.extend(["--config", str(config)])
        argv.extend(extra_args)
        return subprocess.run(argv, cwd=self.directory, env=env, text=True, encoding="utf-8",
                              capture_output=True, timeout=90, check=False)

    @contextmanager
    def model(self, responder, *, provider="openrouter"):
        with LocalModelServer(responder) as server:
            config = self.directory / "local model.toml"
            config.write_text(
                f'[model]\nprovider="{provider}"\nname="local-demo-model"\n'
                f'base_url="http://127.0.0.1:{server.server.server_port}/v1"\n'
                'api_key_env="METASKILL_LOCAL_TEST_KEY"\n'
                '[runtime]\nmax_iterations=8\nmax_tokens=256\n'
                'timeout_seconds=30\nrequest_timeout=15\n'
                '[tools]\nrun_mode="full"\n', encoding="utf-8",
            )
            yield server, config

    def success(self, result, server=None):
        detail = result.stdout + result.stderr
        if server:
            detail += "\n" + "\n".join(server.errors)
            self.assertFalse(server.errors, detail)
        self.assertEqual(result.returncode, 0, detail)
        self.assertNotIn("local-test-key", result.stdout + result.stderr)
        self.assertNotIn("dispatch.registry_miss", result.stderr)

    def test_documented_python_parallel_join(self):
        result = self.cli("meta-text-demo")
        self.success(result)
        self.assertEqual(result.stdout.strip(), "UPPER: HELLO METASKILL 中文\nlower: hello metaskill 中文")

    def test_documented_native_tool_chain(self):
        result = self.cli("meta-tool-demo")
        self.success(result)
        self.assertEqual((self.workspace / "source.txt").read_text(encoding="utf-8"), "Hello MetaSkill 中文")
        self.assertIn("Hello MetaSkill 中文", (self.workspace / "handoff.txt").read_text(encoding="utf-8"))

    def test_text_only_calls_use_http_adapter_and_bind_upstream_text(self):
        def respond(payload, index):
            self.assertFalse(payload.get("tools"), "llm_chat must not expose Agent tools")
            if index == 1:
                self.assertIn("Write one concise sentence", request_text(payload))
                self.assertIn("The library opens at nine.", request_text(payload))
                return "text", "SYNTHETIC FIRST DRAFT 中文"
            self.assertEqual(index, 2)
            self.assertIn("SYNTHETIC FIRST DRAFT 中文", request_text(payload))
            self.assertIn("Polish the supplied draft", request_text(payload))
            return "text", "SYNTHETIC POLISHED SENTENCE 中文"

        with self.model(respond) as (server, config):
            result = self.cli("meta-chat-demo", text="The library opens at nine.", config=config)
            self.success(result, server)
            self.assertEqual(len(server.requests), 2)
        self.assertEqual(result.stdout.strip(), "SYNTHETIC POLISHED SENTENCE 中文")

    def test_classifier_selects_each_conditional_branch_and_routes_to_its_skill(self):
        for label in ("UPPER", "LOWER"):
            with self.subTest(label=label):
                self.workspace = self.directory / f"route {label} 中文"

                def respond(payload, index):
                    self.assertEqual(index, 1, "Classification should require only one exact-label response")
                    self.assertFalse(payload.get("tools"))
                    self.assertIn("EXACTLY ONE of: UPPER | LOWER", request_text(payload))
                    return "text", label

                text = f"Use {label.lower()}case: Hello MetaSkill 中文"
                with self.model(respond) as (server, config):
                    result = self.cli("meta-route-demo", text=text, config=config)
                    self.success(result, server)
                    self.assertEqual(len(server.requests), 1)
                self.assertEqual(result.stdout.strip(), text.upper() if label == "UPPER" else text.lower())
                self.assertTrue((self.workspace / f"{label.lower()}-selected.txt").is_file())
                other = "lower" if label == "UPPER" else "upper"
                self.assertFalse((self.workspace / f"{other}-selected.txt").exists(), "Skipped branch ran its file tool")

    def test_text_agent_skills_load_resources_and_bind_draft_over_http(self):
        def respond(payload, index):
            assert_native_tools(payload)
            content = request_text(payload)
            if index == 1:
                self.assertIn("# Draft an announcement", content)
                self.assertNotIn("{baseDir}", content)
                path = self.skills / "announcement-draft" / "templates" / "announcement.txt"
                self.assertIn(normalized_path(path.parent.parent), normalized_path(system_text(payload)))
                return "tool", "read_file", {"path": str(path)}
            if index == 2:
                self.assertTrue(any(item.get("role") == "tool" for item in payload["messages"]))
                return "text", "SYNTHETIC AGENT DRAFT 中文"
            if index == 3:
                self.assertIn("SYNTHETIC AGENT DRAFT 中文", content)
                path = self.skills / "announcement-edit" / "references" / "style-guide.txt"
                self.assertIn(normalized_path(path.parent.parent), normalized_path(system_text(payload)))
                return "tool", "read_file", {"path": str(path)}
            self.assertEqual(index, 4)
            self.assertTrue(any(item.get("role") == "tool" for item in payload["messages"]))
            return "text", "SYNTHETIC EDITED ANNOUNCEMENT 中文"

        with self.model(respond) as (server, config):
            result = self.cli("meta-writing-demo", text="Our reading group meets tomorrow.", config=config)
            self.success(result, server)
            self.assertEqual(len(server.requests), 4)
        self.assertEqual(result.stdout.strip(), "SYNTHETIC EDITED ANNOUNCEMENT 中文")

    def test_agent_name_does_not_bypass_tools_or_rewrite_output(self):
        # This historical name must behave like every other Agent Skill. The
        # package, task, resource, and response are new synthetic test fixtures.
        package = self.skills / "paper-section-author"
        resource = package / "references" / "synthetic-guide.txt"
        resource.parent.mkdir(parents=True)
        resource.write_text("SYNTHETIC RESOURCE CONTENT ONLY", encoding="utf-8")
        (package / "SKILL.md").write_text(
            '---\nname: paper-section-author\n'
            'description: Read a synthetic resource and return the supplied response.\n'
            '---\n\n# A synthetic Agent package\n\n'
            'Read `{baseDir}/references/synthetic-guide.txt` using read_file.\n',
            encoding="utf-8",
        )
        meta = self.skills / "meta-name-neutral-test"
        meta.mkdir()
        (meta / "SKILL.md").write_text(
            '---\nname: meta-name-neutral-test\n'
            'description: Exercise ordinary Agent execution with a historical name.\n'
            'kind: meta\nfinal_text_mode: "step:read"\n'
            'composition:\n  steps:\n    - id: read\n      kind: agent\n'
            '      skill: paper-section-author\n'
            '      with:\n        task: "{{ inputs.user_message }}"\n'
            '---\n\nA synthetic single-step graph.\n',
            encoding="utf-8",
        )
        expected = (
            "Resource loaded: synthetic guide.\n```latex\n"
            "\\section{Synthetic heading}\nSynthetic body.\n```\n"
            "File written to: synthetic.txt"
        )

        def respond(payload, index):
            assert_native_tools(payload)
            self.assertIn("# A synthetic Agent package", system_text(payload))
            self.assertNotIn("{baseDir}", system_text(payload))
            self.assertIn(normalized_path(resource), normalized_path(system_text(payload)))
            if index == 1:
                return "tool", "read_file", {"path": str(resource)}
            self.assertEqual(index, 2)
            tool_results = [item for item in payload["messages"] if item.get("role") == "tool"]
            self.assertEqual(len(tool_results), 1)
            self.assertIn("SYNTHETIC RESOURCE CONTENT ONLY", str(tool_results[0]))
            return "text", expected

        with self.model(respond) as (server, config):
            result = self.cli("meta-name-neutral-test", text="Read the synthetic guide.", config=config)
            self.success(result, server)
            self.assertEqual(len(server.requests), 2)
        self.assertEqual(result.stdout.strip(), expected)

    def test_file_agent_fork_join_orders_branches_and_binds_only_selected_text(self):
        note = "Note\n\nFacts: The library opens at nine. 中文"
        checks = "Supported: The library opens at nine. 中文"
        format_started = threading.Event()
        check_started = threading.Event()

        def respond(payload, _index):
            assert_native_tools(payload)
            content = request_text(payload)
            system = system_text(payload)
            results = [item for item in payload["messages"] if item.get("role") == "tool"]
            stage = len(results)
            if "# Create a note" in system:
                if stage == 0:
                    path = self.skills / "file-note-create" / "templates" / "note.txt"
                    self.assertIn(normalized_path(path), normalized_path(system))
                    return "tool", "read_file", {"path": str(path)}
                if stage == 1:
                    return "tool", "write_file", {"path": "note.txt", "content": note}
                self.assertEqual(stage, 2)
                self.assertTrue((self.workspace / "note.txt").is_file())
                return "text", "Created note.txt with the supplied library facts."
            if "# Format a note" in system or "# Check a note" in system:
                is_format = "# Format a note" in system
                skill = "file-note-format" if is_format else "file-note-check"
                self.assertIn("Created note.txt with the supplied library facts.", content)
                if stage == 0:
                    # Both HTTP requests must begin before either branch can
                    # proceed. A sequential scheduler deterministically fails.
                    mine, other = (format_started, check_started) if is_format else (check_started, format_started)
                    mine.set()
                    self.assertTrue(other.wait(timeout=10), "Independent Agent steps did not overlap")
                    self.assertTrue((self.workspace / "note.txt").is_file())
                    guide = "format-guide.txt" if is_format else "check-guide.txt"
                    path = self.skills / skill / "references" / guide
                    self.assertIn(normalized_path(path), normalized_path(system))
                    return "tool", "read_file", {"path": str(path)}
                if stage == 1:
                    return "tool", "read_file", {"path": "note.txt"}
                if stage == 2:
                    self.assertIn("The library opens at nine.", str(results[-1]))
                    return "tool", "write_file", {
                        "path": "formatted.txt" if is_format else "checks.txt",
                        "content": note if is_format else checks,
                    }
                self.assertEqual(stage, 3)
                return "text", "FORMATTER HANDOFF" if is_format else "CHECKER HANDOFF NOT BOUND"
            self.assertIn("# Review a note", system)
            self.assertIn("FORMATTER HANDOFF", content)
            self.assertNotIn("CHECKER HANDOFF NOT BOUND", content)
            self.assertTrue((self.workspace / "formatted.txt").is_file(), "Join ran before formatter completed")
            self.assertTrue((self.workspace / "checks.txt").is_file(), "Join ran before checker completed")
            if stage == 0:
                path = self.skills / "file-note-review" / "references" / "review-guide.txt"
                self.assertIn(normalized_path(path), normalized_path(system))
                return "tool", "read_file", {"path": str(path)}
            if stage == 1:
                return "tool", "read_file", {"path": "formatted.txt"}
            if stage == 2:
                return "tool", "read_file", {"path": "checks.txt"}
            if stage == 3:
                self.assertIn("Supported: The library opens at nine.", str(results[-1]))
                return "tool", "write_file", {"path": "reviewed.txt", "content": note}
            self.assertEqual(stage, 4)
            return "text", "Reviewed note saved to reviewed.txt."

        with self.model(respond, provider="tokenrhythm") as (server, config):
            trace_path = self.workspace / "trace.json"
            result = self.cli("meta-file-agent-demo", text="The library opens at nine. 中文", config=config,
                              extra_args=("--trace-json", str(trace_path)))
            self.success(result, server)
            self.assertEqual(len(server.requests), 16)
        self.assertTrue(format_started.is_set() and check_started.is_set())
        self.assertEqual(result.stdout.strip(), "Reviewed note saved to reviewed.txt.")
        self.assertEqual((self.workspace / "note.txt").read_text(encoding="utf-8"), note)
        self.assertEqual((self.workspace / "formatted.txt").read_text(encoding="utf-8"), note)
        self.assertEqual((self.workspace / "checks.txt").read_text(encoding="utf-8"), checks)
        self.assertEqual((self.workspace / "reviewed.txt").read_text(encoding="utf-8"), note)
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(trace["status"], "ok")
        steps = {step["step_id"]: step for step in trace["steps"]}
        self.assertEqual(set(steps), {"create", "format", "check", "review"})
        for step in steps.values():
            self.assertEqual(step["status"], "ok")
            self.assertLessEqual(step["started_at_ms"], step["ended_at_ms"])
        for branch in ("format", "check"):
            self.assertLessEqual(steps["create"]["ended_at_ms"], steps[branch]["started_at_ms"])
            self.assertLessEqual(steps[branch]["ended_at_ms"], steps["review"]["started_at_ms"])
        self.assertLess(steps["format"]["started_at_ms"], steps["check"]["ended_at_ms"])
        self.assertLess(steps["check"]["started_at_ms"], steps["format"]["ended_at_ms"])
        self.assertEqual(steps["review"]["rendered_inputs"]["handoff"], "FORMATTER HANDOFF")
        self.assertNotIn("CHECKER HANDOFF NOT BOUND", json.dumps(steps["review"]["rendered_inputs"]))


if __name__ == "__main__":
    unittest.main()
