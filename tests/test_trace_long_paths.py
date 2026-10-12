"""Real filesystem trace exports beyond Windows MAX_PATH, without model calls."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from metaskill_trace import LocalUsageSink, open_trace
from opensquilla.paths import native_io_path
import run_metaskill


class LongPathTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="metaskill long path 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.deep = self.root / "workspace"
        index = 0
        while len(str(self.deep)) < 280:
            self.deep /= f"parent {index} 中文 with spaces"
            index += 1
        self.assertGreater(len(str(self.deep)), 260)
        # The cleanup also uses the native spelling, so its recursive calls
        # retain the prefix instead of depending on machine-wide path policy.
        self.addCleanup(self.cleanup_deep_tree)

    def cleanup_deep_tree(self):
        target = self.root / "workspace"
        self.assertTrue(target.resolve().is_relative_to(self.root))
        self.assertNotEqual(target, self.root)
        if native_io_path(target).exists():
            shutil.rmtree(native_io_path(target))

    def test_real_dag_export_replaces_a_long_unicode_destination_atomically(self):
        from opensquilla.skills.loader import SkillLoader
        from opensquilla.skills.meta.orchestrator import MetaOrchestrator
        from opensquilla.skills.meta.parser import parse_meta_plan
        from opensquilla.skills.meta.types import MetaMatch

        # Execution and its ledger are short; the real export target and its
        # randomly named atomic-write temporary file are both over 260 chars.
        workspace = self.root / "execution"
        destination = self.deep / "trace with space 中文.json"
        with run_metaskill.isolated_state(workspace) as state:
            loader = SkillLoader(workspace_dir=ROOT / "examples/skills",
                                 snapshot_path=state / "skills_snapshot.json",
                                 lockfile_path=state / "skills-lock.json")
            plan = parse_meta_plan(loader.get_by_name("meta-text-demo"))
            with open_trace(state, destination) as trace:
                orchestrator = MetaOrchestrator(agent_runner=run_metaskill._no_model,
                                               skill_loader=loader, workspace_dir=str(workspace),
                                               run_writer=trace.writer, memory_persist_enabled=False)
                result = asyncio.run(orchestrator.run(MetaMatch(plan=plan, inputs={
                    "user_message": "Synthetic long path 中文",
                })))
                self.assertTrue(result.ok, result.error)
                self.assertEqual(trace.export(), destination)
                native_io_path(destination).write_text("old marker", encoding="utf-8")
                self.assertEqual(trace.export(), destination)
        payload = json.loads(native_io_path(destination).read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual({step["step_id"] for step in payload["steps"]}, {"upper", "lower", "merge"})
        self.assertEqual(payload["final_text"], result.final_text)
        self.assertEqual(list(native_io_path(destination.parent).glob(f".{destination.name}.*.tmp")), [])
        if os.name == "nt":
            self.assertFalse(str(destination).startswith("\\\\?\\"), "Public paths must remain logical")

    def test_native_usage_jsonl_and_trace_state_use_long_unicode_paths(self):
        from opensquilla.engine.usage_accounting import UsageCallStart

        state = self.deep / "state with spaces 中文"
        destination = self.deep / "unused trace.json"
        with open_trace(state, destination) as trace:
            self.assertEqual(trace.writer.list_runs(), [])
            database = trace.database
        self.assertTrue(native_io_path(database).is_file())
        usage_file = state / "usage with spaces 中文.jsonl"
        sink = LocalUsageSink(usage_file)
        call = UsageCallStart(event_id="synthetic-event", execution_id="synthetic-execution",
                              call_index=1, agent_run_id="synthetic-agent", turn_id="synthetic-turn",
                              parent_turn_id=None, session_id="synthetic-session", agent_id="standalone",
                              run_kind="metaskill_cli", provider="synthetic", model="synthetic-model",
                              started_at_ms=1)
        asyncio.run(sink.start(call))
        asyncio.run(sink.mark_unknown(call, "provider_error"))
        rows = [json.loads(line) for line in native_io_path(usage_file).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["status"] for row in rows], ["started", "unknown"])
        self.assertEqual(sink.snapshot()[0]["reason"], "provider_error")


if __name__ == "__main__":
    unittest.main()
