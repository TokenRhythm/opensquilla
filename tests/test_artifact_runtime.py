"""Native standalone artifact contracts with synthetic files and loopback HTTP."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from metaskill_tools import aclose_tools, build_tools
import run_metaskill
from test_documented_modes import LocalModelServer


class ArtifactRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill artifact 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.workspace = self.directory / "workspace"
        self.skills = self.directory / "skills"
        self.skills.mkdir()

    def cli(self, name, *, config=None):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_API_KEY", "_TOKEN"))
            and "PROXY" not in key.upper()
            and not key.upper().startswith("OPENSQUILLA_")
        }
        environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="*",
                           METASKILL_LOCAL_TEST_KEY="local-test-key")
        command = [sys.executable, str(ROOT / "scripts/run_metaskill.py"),
                   "--skills-dir", str(self.skills), "--name", name,
                   "--workspace", str(self.workspace), "--run-mode", "full"]
        if config is not None:
            command.extend(("--config", str(config)))
        return subprocess.run(command, cwd=self.directory, env=environment,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=60, check=False)

    def package(self, name, text):
        directory = self.skills / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(text, encoding="utf-8")

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("artifact session scope is not configured", result.stderr)
        self.assertNotIn("local-test-key", result.stdout + result.stderr)

    def metadata(self):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.workspace / ".metaskill-state/artifacts").rglob("meta.json")]

    def test_native_publish_and_authoring_use_the_cli_session_scope(self):
        from opensquilla.artifacts import ArtifactStore
        from opensquilla.tool_boundary import ToolCall

        async def check():
            with run_metaskill.isolated_state(self.workspace):
                _, handler = build_tools(self.workspace, self.skills,
                                         config={"tools": {"run_mode": "full"}},
                                         session_key="metaskill-cli:synthetic-invocation")
                context = handler._metaskill_context
                try:
                    (self.workspace / "synthetic.txt").write_text("Synthetic file 中文", encoding="utf-8")
                    published = await handler(ToolCall("publish", "publish_artifact", {
                        "path": "synthetic.txt", "bundle": "none",
                    }))
                    self.assertFalse(published.is_error, published.content)
                    payload = json.loads(published.content)
                    self.assertEqual(payload["status"], "published")
                    self.assertEqual(payload["artifact"]["session_id"], "synthetic-invocation")
                    self.assertEqual(payload["artifact"]["workspace_path"], "synthetic.txt")
                    store = ArtifactStore(context.artifact_media_root)
                    reference, material = store.resolve_for_download(
                        payload["artifact"]["id"], session_id="synthetic-invocation")
                    self.assertEqual(reference.session_key, "metaskill-cli:synthetic-invocation")
                    self.assertEqual(material.read_text(encoding="utf-8"), "Synthetic file 中文")
                    duplicate = await handler(ToolCall("republish", "publish_artifact", {
                        "path": "synthetic.txt", "bundle": "none",
                    }))
                    self.assertFalse(duplicate.is_error, duplicate.content)
                    self.assertEqual(json.loads(duplicate.content)["status"], "already_published")
                    authored = await handler(ToolCall("author", "create_csv", {
                        "name": "synthetic.csv", "rows": [["label", "value"], ["中文", 7]],
                    }))
                    self.assertFalse(authored.is_error, authored.content)
                    authored_payload = json.loads(authored.content)
                    reference, material = store.resolve_for_download(
                        authored_payload["artifact"]["id"], session_id="synthetic-invocation")
                    self.assertEqual(reference.source, "create_csv")
                    self.assertEqual(material.read_text(encoding="utf-8-sig"), "label,value\n中文,7\n")
                    self.assertEqual(context.tool_result_store_session_id, "synthetic-invocation")
                finally:
                    await aclose_tools(handler)

        asyncio.run(check())

    def test_direct_native_callers_get_isolated_scopes_in_the_same_workspace(self):
        from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
        from opensquilla.tool_boundary import ToolCall

        async def check():
            with run_metaskill.isolated_state(self.workspace):
                handlers = []
                try:
                    (self.workspace / "synthetic.txt").write_text("Synthetic file", encoding="utf-8")
                    for index in range(2):
                        _, handler = build_tools(self.workspace, self.skills,
                                                 config={"tools": {"run_mode": "full"}})
                        handlers.append(handler)
                        result = await handler(ToolCall(f"publish-{index}", "publish_artifact", {
                            "path": "synthetic.txt", "bundle": "none",
                        }))
                        self.assertFalse(result.is_error, result.content)
                    first, second = (handler._metaskill_context for handler in handlers)
                    self.assertNotEqual(first.session_key, second.session_key)
                    self.assertNotEqual(first.artifact_session_id, second.artifact_session_id)
                    self.assertEqual(len(self.metadata()), 2)
                    with self.assertRaises(ArtifactNotFoundError):
                        ArtifactStore(first.artifact_media_root).resolve_for_download(
                            first.published_artifacts[0]["id"], session_id=second.artifact_session_id)
                finally:
                    for handler in handlers:
                        await aclose_tools(handler)

        asyncio.run(check())

    def test_no_model_tool_call_dag_publishes_with_a_fresh_scope_on_each_run(self):
        self.package("synthetic-publish", (
            "---\nname: synthetic-publish\ndescription: Publish synthetic text.\nkind: meta\n"
            'final_text_mode: "step:publish"\ncomposition:\n  steps:\n'
            "    - id: write\n      kind: tool_call\n      tool: write_file\n"
            '      tool_args:\n        path: synthetic.txt\n        content: "Synthetic CLI file"\n'
            "    - id: publish\n      kind: tool_call\n      tool: publish_artifact\n"
            "      depends_on: [write]\n      tool_args:\n        path: synthetic.txt\n"
            "        bundle: none\n---\n"
        ))
        for _ in range(2):
            self.assert_success(self.cli("synthetic-publish"))
        records = self.metadata()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["session_id"] for record in records}), 2)
        self.assertEqual(len({record["session_key"] for record in records}), 2)
        self.assertTrue(all(record["session_key"] == f'metaskill-cli:{record["session_id"]}'
                            for record in records))
        self.assertEqual((self.workspace / "synthetic.txt").read_text(encoding="utf-8"), "Synthetic CLI file")

    def test_agent_can_write_then_publish_through_the_real_provider_and_native_handler(self):
        self.package("synthetic-writer", (
            "---\nname: synthetic-writer\ndescription: Publish a synthetic file.\n---\n"
            "Write synthetic.txt, publish it, and return a brief completion message.\n"
        ))
        self.package("synthetic-agent-meta", (
            "---\nname: synthetic-agent-meta\ndescription: Run a synthetic writer.\nkind: meta\n"
            'final_text_mode: "step:write"\ncomposition:\n  steps:\n'
            "    - id: write\n      kind: agent\n      skill: synthetic-writer\n"
            '      with:\n        task: "{{ inputs.user_message }}"\n---\n'
        ))

        def respond(payload, index):
            if index == 1:
                return "tool", "write_file", {"path": "synthetic.txt", "content": "Synthetic Agent file 中文"}
            if index == 2:
                return "tool", "publish_artifact", {"path": "synthetic.txt", "bundle": "none"}
            results = [message for message in payload["messages"] if message.get("role") == "tool"]
            self.assertEqual(len(results), 2)
            self.assertIn('"status": "published"', str(results[-1]["content"]))
            return "text", "Synthetic file published."

        with LocalModelServer(respond) as server:
            config = self.directory / "loopback.toml"
            config.write_text(
                '[model]\nprovider="openrouter"\nname="local-demo-model"\n'
                f'base_url="http://127.0.0.1:{server.server.server_port}/v1"\n'
                'api_key_env="METASKILL_LOCAL_TEST_KEY"\n[runtime]\n'
                'max_iterations=4\nmax_tokens=256\ntimeout_seconds=30\nrequest_timeout=15\n',
                encoding="utf-8",
            )
            self.assert_success(self.cli("synthetic-agent-meta", config=config))
            self.assertEqual(server.errors, [])
            # Native Agent artifact delivery ends the turn after a successful
            # publication, without requesting an extra model summary.
            self.assertEqual(len(server.requests), 2)
        records = self.metadata()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_key"], f'metaskill-cli:{records[0]["session_id"]}')
        self.assertEqual((self.workspace / "synthetic.txt").read_text(encoding="utf-8"), "Synthetic Agent file 中文")


if __name__ == "__main__":
    unittest.main()
