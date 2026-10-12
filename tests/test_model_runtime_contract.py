"""Check one-shot model settings and failures with synthetic provider events."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_metaskill


class ConfigurationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill config contract ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / "local.toml"

    def read(self, text):
        self.config.write_text(text, encoding="utf-8")
        return run_metaskill.read_config(self.config)

    def test_invalid_search_values_are_configuration_errors(self) -> None:
        cases = (
            ("provider", "[]"), ("provider", "17"),
            ("api_key_env", "[]"), ("proxy", "[]"),
            ("max_results", "0"), ("max_results", "-1"),
            ("max_results", "1.5"), ("max_results", "true"),
            ("max_results", '"5"'),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, f"search\\.{field}"):
                    self.read(f"[search]\n{field}={value}\n")

    def test_invalid_tool_values_are_configuration_errors(self) -> None:
        cases = (
            ("run_mode", "[]"), ("run_mode", "true"),
            ("run_mode", '"invalid-mode"'), ("run_mode", '""'),
            ("sandbox_backend", "[]"), ("sandbox_backend", "17"),
            ("sandbox_backend", '"invalid-backend"'),
            ("sandbox_backend", '"windows_restricted_token"'),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, f"tools\\.{field}"):
                    self.read(f"[tools]\n{field}={value}\n")

    def test_valid_search_and_native_backend_settings_are_preserved(self) -> None:
        for mode, backend in (
            ("safe", "auto"), ("full", "noop"), ("safe", "bubblewrap"),
            ("safe", "seatbelt"), ("safe", "windows_default"),
        ):
            with self.subTest(mode=mode, backend=backend):
                config = self.read(
                    f'[tools]\nrun_mode="{mode}"\nsandbox_backend="{backend}"\n'
                    '[search]\nprovider="duckduckgo"\napi_key_env="SYNTHETIC_SEARCH_KEY"\n'
                    'proxy=""\nmax_results=1\n'
                )
                self.assertEqual(config["tools"], {"run_mode": mode, "sandbox_backend": backend})
                self.assertEqual(config["search"]["max_results"], 1)
                self.assertEqual(config["search"]["api_key_env"], "SYNTHETIC_SEARCH_KEY")

    def test_cli_misconfigurations_fail_cleanly_before_execution(self) -> None:
        cases = (
            ("search.proxy", "[search]\nproxy=[]\n"),
            ("search.api_key_env", "[search]\napi_key_env=[]\n"),
            ("search.provider", "[search]\nprovider=[]\n"),
            ("search.max_results", "[search]\nmax_results=true\n"),
            ("tools.run_mode", "[tools]\nrun_mode=[]\n"),
            ("tools.sandbox_backend", "[tools]\nsandbox_backend=[]\n"),
            ("tools.sandbox_backend", '[tools]\nsandbox_backend="invalid-backend"\n'),
        )
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_API_KEY", "_TOKEN"))
            and "PROXY" not in key.upper()
            and not key.upper().startswith("OPENSQUILLA_")
        }
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        workspace = self.directory / "must-not-run"
        for field, text in cases:
            with self.subTest(field=field, text=text):
                self.config.write_text(text, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "run_metaskill.py"),
                     "--name", "meta-tool-demo", "--config", str(self.config),
                     "--workspace", str(workspace)],
                    cwd=self.directory, env=env, capture_output=True,
                    text=True, encoding="utf-8", timeout=15, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith(f"Error: {field}"), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(workspace.exists(), "Invalid configuration must not start a run")


class OfflineProvider:
    """Capture call arguments without constructing an HTTP transport."""

    provider_name = "openai"
    model = "synthetic-model"

    def __init__(self, events):
        self.events = events
        self.calls = []

    async def chat(self, messages, *, tools=None, config=None):
        self.calls.append((messages, tools, config))
        for event in self.events:
            yield event


class ModelRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill model contract ")
        self.addCleanup(self.temp.cleanup)
        self.state = run_metaskill.isolated_state(Path(self.temp.name))
        self.state.__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)

    def build_chat(self, provider, **settings):
        from opensquilla.engine.types import AgentConfig
        from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider

        base = AgentConfig(
            model_id=provider.model, provider_id=provider.provider_name,
            max_tokens=256, **settings,
        )
        return make_llm_chat_from_provider(
            provider=provider, base_config=base, max_tokens=base.max_tokens,
        )

    async def test_provider_error_without_text_cannot_be_successful_output(self) -> None:
        from opensquilla.provider.types import ErrorEvent

        provider = OfflineProvider([
            ErrorEvent(message="Synthetic authorization failure", code="auth"),
        ])
        chat = self.build_chat(provider)
        with self.assertRaisesRegex(RuntimeError, "Synthetic authorization failure"):
            await chat("Return a message.", "Synthetic request")
        self.assertEqual(len(provider.calls), 1)

    async def test_partial_text_followed_by_provider_error_is_not_a_deliverable(self) -> None:
        from opensquilla.provider.types import ErrorEvent, TextDeltaEvent

        provider = OfflineProvider([
            TextDeltaEvent(text="An incomplete answer"),
            ErrorEvent(message="Synthetic interrupted stream", code="transport"),
        ])
        chat = self.build_chat(provider)
        with self.assertRaisesRegex(RuntimeError, "Synthetic interrupted stream"):
            await chat("Return a complete message.", "Synthetic request")
        self.assertEqual(len(provider.calls), 1)

    async def test_provider_error_masks_credential_shaped_text(self) -> None:
        from opensquilla.provider.types import ErrorEvent

        bearer = "synthetic-bearer-secret"
        key = "sk-test-synthetic-secret"
        provider = OfflineProvider([
            ErrorEvent(
                message=f"Synthetic bad credential: Bearer {bearer}; api_key={key}",
                code="auth",
            ),
        ])
        chat = self.build_chat(provider)
        with self.assertRaises(RuntimeError) as captured:
            await chat("Return a message.", "Synthetic request")
        message = str(captured.exception)
        self.assertNotIn(bearer, message)
        self.assertNotIn(key, message)
        self.assertIn("Synthetic bad credential", message)

    async def test_single_turn_rejects_truncated_or_tool_stopped_responses(self) -> None:
        from opensquilla.provider.types import DoneEvent, TextDeltaEvent

        for reason in ("length", "max_tokens", "error", "tool_calls", "tool_use"):
            with self.subTest(reason=reason):
                provider = OfflineProvider([
                    TextDeltaEvent(text="An incomplete answer"),
                    DoneEvent(stop_reason=reason, model=OfflineProvider.model),
                ])
                chat = self.build_chat(provider)
                with self.assertRaises(RuntimeError):
                    await chat("Return a complete message.", "Synthetic request")

    async def test_single_turn_requires_a_completion_event(self) -> None:
        from opensquilla.provider.types import TextDeltaEvent

        provider = OfflineProvider([TextDeltaEvent(text="An incomplete stream")])
        chat = self.build_chat(provider)
        with self.assertRaises(RuntimeError):
            await chat("Return a complete message.", "Synthetic request")

    async def test_single_turn_accepts_normal_completion_reasons(self) -> None:
        from opensquilla.provider.types import DoneEvent, TextDeltaEvent

        for reason in ("stop", "end_turn"):
            with self.subTest(reason=reason):
                provider = OfflineProvider([
                    TextDeltaEvent(text="A complete answer."),
                    DoneEvent(stop_reason=reason, model=OfflineProvider.model),
                ])
                chat = self.build_chat(provider)
                self.assertEqual(
                    await chat("Return a message.", "Synthetic request"),
                    "A complete answer.",
                )

    async def test_llm_chat_step_rejects_empty_visible_response(self) -> None:
        from opensquilla.provider.types import DoneEvent
        from opensquilla.skills.meta.executors.llm_classify import run_llm_chat_step
        from opensquilla.skills.meta.types import MetaStep

        provider = OfflineProvider([DoneEvent(stop_reason="stop", model=OfflineProvider.model)])
        chat = self.build_chat(provider)
        step = MetaStep(
            id="answer", skill="answer", kind="llm_chat",
            with_args={"task": "{{ inputs.user_message }}"},
        )
        with self.assertRaises(RuntimeError):
            await run_llm_chat_step(
                step, {"user_message": "Synthetic request"}, {},
                llm_chat=chat, agent_runner=run_metaskill._no_model,
            )

    async def test_single_turn_call_uses_explicit_runtime_options(self) -> None:
        from opensquilla.engine.types import ThinkingLevel
        from opensquilla.provider.types import DoneEvent, TextDeltaEvent

        provider = OfflineProvider([
            TextDeltaEvent(text="A synthetic answer."),
            DoneEvent(stop_reason="stop", model=OfflineProvider.model),
        ])
        chat = self.build_chat(
            provider, request_timeout=13.0, temperature=0.25, top_p=0.8,
            thinking=ThinkingLevel.HIGH, thinking_budget_tokens=128,
        )
        self.assertEqual(await chat("Return a message.", "Synthetic request"), "A synthetic answer.")
        self.assertEqual(len(provider.calls), 1)
        messages, tools, config = provider.calls[0]
        self.assertIsNone(tools)
        self.assertEqual(messages[0].content, "Synthetic request")
        self.assertEqual(config.system, "Return a message.")
        self.assertEqual(config.timeout, 13.0)
        self.assertEqual(config.temperature, 0.25)
        self.assertEqual(config.top_p, 0.8)
        self.assertEqual(config.max_tokens, 256)
        self.assertTrue(config.thinking)
        self.assertEqual(config.thinking_level, ThinkingLevel.HIGH)
        self.assertEqual(config.thinking_budget_tokens, 128)
        self.assertTrue(config.thinking_budget_explicit)

    def test_provider_configuration_type_errors_are_user_errors(self) -> None:
        cases = {
            "provider": ["custom"],
            "base_url": 17,
            "api_key_env": 17,
            "proxy": ["https://unused.invalid"],
            "org_id": 17,
            "provider_routing": ["synthetic-route"],
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                settings = {
                    "provider": "custom", "name": "synthetic-model",
                    "base_url": "https://unused.invalid/v1", "api_key_env": "",
                    field: value,
                }
                with self.assertRaises(ValueError):
                    run_metaskill.provider_config(settings)

    def test_environment_key_belongs_to_explicit_provider_configuration(self) -> None:
        with patch.dict(os.environ, {"SYNTHETIC_SUPPLIER_KEY": "synthetic-key-only"}):
            config = run_metaskill.provider_config({
                "provider": "custom", "name": "synthetic-model",
                "base_url": "https://unused.invalid/v1",
                "api_key_env": "SYNTHETIC_SUPPLIER_KEY",
            })
        self.assertEqual(config.provider, "custom")
        self.assertEqual(config.model, "synthetic-model")
        self.assertEqual(config.base_url, "https://unused.invalid/v1")
        self.assertEqual(config.api_key, "synthetic-key-only")

    def test_unknown_denied_tool_is_not_silently_ignored(self) -> None:
        from metaskill_tools import build_tools

        workspace = Path(self.temp.name)
        skills = workspace / "skills"
        skills.mkdir()
        with self.assertRaisesRegex(ValueError, "unknown_synthetic_tool"):
            build_tools(
                workspace, skills,
                config={"tools": {"deny": ["unknown_synthetic_tool"]}},
            )

    async def test_skill_tools_read_only_the_pinned_package_and_resources(self) -> None:
        from metaskill_tools import build_tools, close_tools
        from opensquilla.skills.loader import SkillLoader
        from opensquilla.tool_boundary import ToolCall
        from opensquilla.tools.builtin import skill_tools
        from opensquilla.tools.types import current_tool_context

        workspace = Path(self.temp.name)
        skills = workspace / "package roots 中文"
        package = skills / "synthetic-package"
        resource = package / "references" / "message.txt"
        resource.parent.mkdir(parents=True)
        resource.write_text("A synthetic resource keeps {baseDir} verbatim.\n", encoding="utf-8")
        (package / "SKILL.md").write_text(
            "---\nname: synthetic-package\n"
            "description: Read a synthetic resource.\n---\n"
            "Read {baseDir}/references/message.txt.\n",
            encoding="utf-8",
        )
        loader = SkillLoader(
            workspace_dir=skills,
            snapshot_path=workspace / ".metaskill-state" / "snapshot.json",
            lockfile_path=workspace / ".metaskill-state" / "lock.json",
        )
        loader.load_all()
        catalog = loader.snapshot()
        self.assertEqual({skill.name for skill in catalog.skills}, {"synthetic-package"})
        definitions, handler = build_tools(
            workspace, skills, catalog=catalog,
            config={"tools": {"run_mode": "full", "allow": ["skill_view", "skill_list"]}},
        )
        self.addCleanup(close_tools, handler)
        self.assertEqual({tool.name for tool in definitions}, {"skill_view", "skill_list"})
        self.assertIs(handler._metaskill_context.skill_catalog, catalog)
        self.assertFalse(
            {"skill_create", "skill_edit", "skill_delete", "skill_install_community"}
            & set(handler._metaskill_registry.list_names())
        )

        # If a tool falls back to a mutable/global loader, the sentinel lacks
        # every loader method and the real dispatcher reports an error.
        token = current_tool_context.set(handler._metaskill_context)
        try:
            with patch.object(skill_tools, "_loader", object()):
                listed = await handler(ToolCall("list", "skill_list", {}))
                body = await handler(ToolCall("body", "skill_view", {
                    "name": "synthetic-package", "file_path": "SKILL.md",
                }))
                contents = await handler(ToolCall("resource", "skill_view", {
                    "name": "synthetic-package", "file_path": "references/message.txt",
                }))
                missing = await handler(ToolCall("missing", "skill_view", {
                    "name": "uninstalled-synthetic-package",
                }))
        finally:
            current_tool_context.reset(token)

        for result in (listed, body, contents, missing):
            self.assertFalse(result.is_error, result.content)
        self.assertIn("Available skills (1)", listed.content)
        self.assertIn("synthetic-package", listed.content)
        self.assertIn(str(package), body.content)
        self.assertNotIn("{baseDir}", body.content)
        self.assertEqual(contents.content, resource.read_text(encoding="utf-8"))
        self.assertIn("Skill not found", missing.content)


if __name__ == "__main__":
    unittest.main()
