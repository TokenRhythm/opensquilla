"""Check native trace persistence with synthetic DAG inputs only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from metaskill_trace import open_trace
import run_metaskill


class TraceTests(unittest.TestCase):
    def test_real_dag_records_lifecycle_and_rendered_bindings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="metaskill trace 中文 # ") as directory:
            workspace = Path(directory) / "workspace"
            destination = workspace / "trace.json"
            with run_metaskill.isolated_state(workspace) as state:
                from opensquilla.skills.loader import SkillLoader
                from opensquilla.skills.meta.orchestrator import MetaOrchestrator
                from opensquilla.skills.meta.parser import parse_meta_plan
                from opensquilla.skills.meta.types import MetaMatch

                loader = SkillLoader(
                    workspace_dir=ROOT / "examples" / "skills",
                    snapshot_path=state / "skills_snapshot.json",
                    lockfile_path=state / "skills-lock.json",
                )
                plan = parse_meta_plan(loader.get_by_name("meta-text-demo"))
                with open_trace(state, destination) as trace:
                    orchestrator = MetaOrchestrator(
                        agent_runner=run_metaskill._no_model,
                        skill_loader=loader,
                        workspace_dir=str(workspace),
                        run_writer=trace.writer,
                        memory_persist_enabled=False,
                    )
                    result = asyncio.run(orchestrator.run(MetaMatch(plan=plan, inputs={
                        "user_message": "Hello 中文", "api_key": "private-test-value",
                    })))
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(trace.export(), destination)
                    self.assertTrue(trace.database.is_relative_to(state))

            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertLessEqual(payload["started_at_ms"], payload["ended_at_ms"])
            self.assertEqual(payload["inputs"]["api_key"], "[REDACTED]")
            self.assertNotIn("private-test-value", destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["plan_snapshot"]["plan"]["name"], "meta-text-demo")
            steps = {step["step_id"]: step for step in payload["steps"]}
            self.assertEqual(set(steps), {"upper", "lower", "merge"})
            for predecessor in ("upper", "lower"):
                self.assertLessEqual(
                    steps[predecessor]["ended_at_ms"], steps["merge"]["started_at_ms"],
                )
                self.assertEqual(
                    steps["merge"]["rendered_inputs"][predecessor],
                    steps[predecessor]["output_text"],
                )
            self.assertEqual(payload["final_text"], result.final_text)
            self.assertIsInstance(steps["merge"]["usage"], dict)

    def test_missing_record_does_not_create_successful_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="metaskill missing trace ") as directory:
            workspace = Path(directory)
            destination = workspace / "trace.json"
            with open_trace(workspace / ".metaskill-state", destination) as trace:
                with self.assertRaisesRegex(RuntimeError, "exactly one persisted run"):
                    trace.export()
                with self.assertRaisesRegex(RuntimeError, "record not found"):
                    trace.export("unknown-run")
            self.assertFalse(destination.exists())

    def test_each_invocation_uses_its_own_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="metaskill separate trace ") as directory:
            workspace = Path(directory)
            state = workspace / ".metaskill-state"
            with open_trace(state, workspace / "first.json") as first:
                first_database = first.database
            with open_trace(state, workspace / "second.json") as second:
                self.assertNotEqual(first_database, second.database)
                self.assertEqual(second.writer.list_runs(), [])
            self.assertTrue(first_database.is_file())

    def test_successful_run_with_missing_steps_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="metaskill incomplete trace ") as directory:
            workspace = Path(directory)
            destination = workspace / "trace.json"
            with run_metaskill.isolated_state(workspace) as state:
                from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

                plan = MetaPlan(
                    name="synthetic-plan", triggers=(), priority=0,
                    steps=(MetaStep(id="one", skill="synthetic"),),
                )
                with open_trace(state, destination) as trace:
                    run_id = trace.writer.begin_run_sync(
                        meta_skill_name=plan.name, meta_plan=plan, triggered_by="manual_command",
                        inputs={}, session_key=None, turn_id=None,
                    )
                    trace.writer.finish_run_sync(run_id=run_id, status="ok", result=MetaResult(ok=True))
                    with self.assertRaisesRegex(RuntimeError, "missing required step records"):
                        trace.export(run_id)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
