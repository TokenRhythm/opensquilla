"""Synthetic orchestration checks. These are not historical benchmark runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from metaskill_benchmark import runner
from metaskill_benchmark.plans import render_plan, meta_name
from test_documented_modes import LocalModelServer


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="MetaSkill batch 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / "source-skills"
        ordinary = self.skills / "synthetic-writer"
        ordinary.mkdir(parents=True)
        (ordinary / "SKILL.md").write_text(
            "---\nname: synthetic-writer\ndescription: Synthetic writing test\n---\n"
            "Write only answer.txt in the workspace, with the exact text 'synthetic answer'.\n", encoding="utf-8")
        self.canonical = {"task_id": "synthetic_task", "nodes": [{
            "id": "write", "skill": "synthetic-writer", "objective": "Write answer.txt",
            "required_outputs": ["answer.txt"], "write_access": True, "network_access": False,
            "depends_on": [], "inputs": {},
        }]}
        self.plan = self.root / "canonical.json"
        runner.write_json(self.plan, self.canonical)
        meta = self.skills / meta_name("synthetic_task")
        meta.mkdir()
        (meta / "SKILL.md").write_text(render_plan({"task_id": "synthetic_task"}, self.canonical,
                                                  {"synthetic-writer": "synthetic-writer"}), encoding="utf-8")
        self.prepared = {"schema_version": 1, "upstream_root": str(self.root / "upstream"), "tasks": {
            "synthetic_task": {"task_config_path": str(self.root / "task.json"),
                               "prompt": "Synthetic test only.\nWrite a file. 中文",
                               "skills_dir": str(self.skills), "meta_name": meta.name,
                               "plan_path": str(self.plan), "input_assets": []}}}
        self.prepared_path = self.root / "prepared.json"
        runner.write_json(self.prepared_path, self.prepared)
        self.config = self.root / "config.toml"
        self.output = self.root / "batch"
        self.config.write_text('[model]\nprovider="openrouter"\nname="local-demo-model"\n'
                               'api_key_env="METASKILL_LOCAL_TEST_KEY"\nbase_url="http://127.0.0.1:1/v1"\n'
                               '[tools]\nrun_mode="full"\n', encoding="utf-8")
        self.validation = patch.object(runner, "validate_prepared", return_value=self.prepared)
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.preflight = patch.object(runner, "check", return_value={"errors": [], "missing_tools": []})
        self.preflight.start()
        self.addCleanup(self.preflight.stop)

    def objective(self, _task, workspace, _upstream):
        self.assertFalse((workspace / "skills").exists())
        self.assertFalse((workspace / ".metaskill-state").exists())
        score = 100 if (workspace / "answer.txt").read_text(encoding="utf-8") == "synthetic answer" else 0
        return {"status": "completed", "score_kind": "synthetic_test", "total_score": score, "max_score": 100}

    def fake_execute(self, command, directory, timeout):
        workspace = directory / "workspace"
        self.assertFalse((workspace / "answer.txt").exists(), "A repeat reused old output")
        (workspace / "answer.txt").write_text("synthetic answer", encoding="utf-8")
        (workspace / "skills/synthetic-writer/new-cache.txt").write_text("local mutation", encoding="utf-8")
        return {"exit_code": 0, "timed_out": False}

    def test_three_isolated_trials_resume_and_fingerprint_refusal(self):
        with patch.object(runner, "execute", side_effect=self.fake_execute) as execute, \
                patch.object(runner, "evaluate_artifacts", side_effect=self.objective):
            result = runner.run_batch(self.prepared_path, self.config, self.output)
            self.assertEqual(execute.call_count, 3)
            self.assertEqual(result["coverage"]["completed_trials"], 3)
            runner.run_batch(self.prepared_path, self.config, self.output, resume=True)
            self.assertEqual(execute.call_count, 3)
            with self.assertRaisesRegex(ValueError, "not empty"):
                runner.run_batch(self.prepared_path, self.config, self.output)
            self.config.write_text(self.config.read_text(encoding="utf-8") + "\n# changed budget", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identical"):
                runner.run_batch(self.prepared_path, self.config, self.output, resume=True)
        records = runner.records_in(self.output)
        self.assertEqual(len({r["workspace"] for r in records}), 3)
        self.assertFalse((self.skills / "synthetic-writer/new-cache.txt").exists())

    def test_failed_attempt_is_retained_and_retry_is_explicit(self):
        def fail(command, directory, timeout):
            self.fake_execute(command, directory, timeout)
            return {"exit_code": 7, "timed_out": False}
        with patch.object(runner, "execute", side_effect=fail), \
                patch.object(runner, "evaluate_artifacts", side_effect=self.objective):
            result = runner.run_batch(self.prepared_path, self.config, self.output, repeats=1)
        self.assertEqual(result["coverage"]["completed_trials"], 0)
        with patch.object(runner, "execute", side_effect=self.fake_execute) as execute, \
                patch.object(runner, "evaluate_artifacts", side_effect=self.objective):
            runner.run_batch(self.prepared_path, self.config, self.output, repeats=1, resume=True)
            self.assertEqual(execute.call_count, 0)
            result = runner.run_batch(self.prepared_path, self.config, self.output, repeats=1, resume=True, retry_failed=True)
            self.assertEqual(execute.call_count, 1)
        records = runner.records_in(self.output)
        self.assertEqual([r["attempt"] for r in records], [1, 2])
        self.assertEqual([r["status"] for r in records], ["failed", "completed"])
        self.assertEqual(result["coverage"]["completed_trials"], 1)

    def test_timeout_and_evaluation_failure_are_not_successes(self):
        def timeout(command, directory, budget):
            self.fake_execute(command, directory, budget)
            return {"exit_code": -1, "timed_out": True}
        error = {"status": "error", "total_score": None, "error": "synthetic evaluator error"}
        with patch.object(runner, "execute", side_effect=timeout), \
                patch.object(runner, "evaluate_artifacts", return_value=error):
            result = runner.run_batch(self.prepared_path, self.config, self.output, repeats=1)
        record = runner.records_in(self.output)[0]
        self.assertEqual(record["status"], "timeout")
        self.assertIsNone(record["objective"]["total_score"])
        self.assertEqual(result["coverage"]["completed_trials"], 0)

    def test_source_config_edits_cannot_change_later_trials(self):
        original_model = "local-demo-model"
        def execute(command, directory, timeout):
            deployment = Path(command[command.index("--config") + 1])
            self.assertNotEqual(deployment, self.config)
            self.assertIn(original_model, deployment.read_text(encoding="utf-8"))
            self.config.write_text(self.config.read_text(encoding="utf-8").replace(original_model, "changed-model"), encoding="utf-8")
            return self.fake_execute(command, directory, timeout)
        with patch.object(runner, "execute", side_effect=execute), \
                patch.object(runner, "evaluate_artifacts", side_effect=self.objective):
            result = runner.run_batch(self.prepared_path, self.config, self.output)
        self.assertEqual(result["coverage"]["completed_trials"], 3)
        self.assertEqual({r["model"] for r in runner.records_in(self.output)}, {original_model})

    def test_assets_cannot_escape_or_overwrite_runtime_inputs(self):
        task = dict(self.prepared["tasks"]["synthetic_task"])
        task["input_assets"] = [{"source": str(self.plan), "relative_destination": "../escape.json"}]
        with self.assertRaisesRegex(ValueError, "destination"):
            runner.stage(task, self.root / "invalid-stage")

    def test_preflight_requires_the_soffice_command_used_by_skills(self):
        self.preflight.stop()
        xlsx = self.skills / "xlsx"
        xlsx.mkdir()
        (xlsx / "SKILL.md").write_text("Synthetic dependency fixture", encoding="utf-8")
        with patch("run_metaskill.provider_config"), \
                patch.object(runner.shutil, "which", side_effect=lambda name: "/tools/libreoffice" if name == "libreoffice" else None):
            report = runner.check(self.prepared_path, self.config)
        self.assertEqual(report["missing_tools"], [{"task_id": "synthetic_task", "skill": "xlsx", "requirement": "soffice"}])

    def test_preflight_reports_missing_poppler_for_each_document_task(self):
        self.preflight.stop()
        template = self.prepared["tasks"]["synthetic_task"]
        self.prepared["tasks"] = {}
        for skill in ("docx", "pptx", "xlsx"):
            task_id = f"synthetic_{skill}"
            skills = self.root / task_id / "skills"
            package = skills / skill
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text("Synthetic dependency fixture", encoding="utf-8")
            self.prepared["tasks"][task_id] = dict(template, skills_dir=str(skills))
        available = {"node", "pandoc", "soffice"}
        with patch("run_metaskill.provider_config"), \
                patch.object(runner.shutil, "which", side_effect=lambda name: f"/tools/{name}" if name in available else None):
            report = runner.check(self.prepared_path, self.config)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["missing_tools"], [
                {"task_id": "synthetic_docx", "skill": "docx", "requirement": "pdftoppm"},
                {"task_id": "synthetic_pptx", "skill": "pptx", "requirement": "pdftoppm"},
            ])
            for task_id in ("synthetic_docx", "synthetic_pptx"):
                self.assertIn("pdftoppm", report["skill_requirements"][task_id])
            self.assertNotIn("pdftoppm", report["skill_requirements"]["synthetic_xlsx"])
            available.add("pdftoppm")
            ready = runner.check(self.prepared_path, self.config)
            self.assertEqual(ready["missing_tools"], [])

    def test_preflight_does_not_require_poppler_for_selected_non_document_task(self):
        self.preflight.stop()
        xlsx = self.skills / "xlsx"
        xlsx.mkdir()
        (xlsx / "SKILL.md").write_text("Synthetic dependency fixture", encoding="utf-8")
        unselected_skills = self.root / "unselected-skills"
        document = unselected_skills / "docx"
        document.mkdir(parents=True)
        (document / "SKILL.md").write_text("Synthetic dependency fixture", encoding="utf-8")
        self.prepared["tasks"]["synthetic_docx"] = dict(
            self.prepared["tasks"]["synthetic_task"], skills_dir=str(unselected_skills))
        with patch("run_metaskill.provider_config"), \
                patch.object(runner.shutil, "which", side_effect=lambda name: "/tools/soffice" if name == "soffice" else None) as which:
            report = runner.check(self.prepared_path, self.config, tasks=["synthetic_task"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["missing_tools"], [])
        self.assertEqual(report["tasks"], ["synthetic_task"])
        self.assertNotIn("pdftoppm", report["skill_requirements"]["synthetic_task"])
        self.assertNotIn("pdftoppm", [call.args[0] for call in which.call_args_list])

    def test_tool_profiles_are_per_attempt_without_changing_parent_environment(self):
        previous = dict(os.environ)
        first = runner.execution_environment(self.root / "first")
        second = runner.execution_environment(self.root / "second")
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "TEMP", "UserInstallation"):
            self.assertNotEqual(first[key], second[key], key)
        profile = self.root / "first/tool-profile"
        expected = profile / ("Library/Application Support/LibreOffice/4" if sys.platform == "darwin"
                              else ".config/libreoffice/4")
        self.assertEqual(first["UserInstallation"], expected.as_uri())
        self.assertTrue(Path(first["TEMP"]).is_dir())
        self.assertEqual(dict(os.environ), previous)

    def test_real_native_agent_runs_three_times_over_local_http(self):
        def respond(payload, _index):
            if any(message.get("role") == "tool" for message in payload["messages"]):
                return "text", "Synthetic task complete."
            return "tool", "write_file", {"path": "answer.txt", "content": "synthetic answer"}

        with LocalModelServer(respond) as server:
            self.config.write_text(
                '[model]\nprovider="openrouter"\nname="local-demo-model"\n'
                f'base_url="http://127.0.0.1:{server.server.server_port}/v1"\n'
                'api_key_env="METASKILL_LOCAL_TEST_KEY"\n[runtime]\nmax_iterations=4\n'
                'max_tokens=256\ntimeout_seconds=30\nrequest_timeout=15\n'
                '[tools]\nrun_mode="full"\n', encoding="utf-8")
            environment = {key: value for key, value in os.environ.items()
                           if not key.upper().endswith(("_API_KEY", "_TOKEN"))
                           and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
                           and not key.upper().startswith("OPENSQUILLA_")}
            environment.update(METASKILL_LOCAL_TEST_KEY="local-test-key", NO_PROXY="*")
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(runner, "evaluate_artifacts", side_effect=self.objective):
                result = runner.run_batch(self.prepared_path, self.config, self.output, timeout=90)
            self.assertFalse(server.errors, server.errors)
            self.assertGreaterEqual(len(server.requests), 6)
        self.assertEqual(result["coverage"]["completed_trials"], 3)
        records = runner.records_in(self.output)
        for record in records:
            self.assertEqual(record["objective"]["total_score"], 100)
            self.assertEqual(record["conformance"]["completed_nodes"], 1)
            self.assertEqual(record["conformance"]["conformance_status"], "valid")
            self.assertTrue(Path(record["trace_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
