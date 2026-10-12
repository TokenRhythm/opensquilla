"""Exercise benchmark evaluation with synthetic inputs, artifacts and traces."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from metaskill_benchmark.evaluate import audit_trace, evaluate_artifacts, summarize


def diamond():
    """A join deliberately binds one of its two dependencies, not both."""
    plan = {"nodes": [
        {"id": "seed", "depends_on": [], "inputs": ["task_request"]},
        {"id": "left", "depends_on": ["seed"], "inputs": ["outputs.seed"]},
        {"id": "right", "depends_on": ["seed"], "inputs": ["outputs.seed"]},
        {"id": "join", "depends_on": ["left", "right"], "inputs": ["outputs.left"]},
    ]}
    trace = {"steps": [
        {"step_id": "seed", "status": "ok", "started_at_ms": 0, "ended_at_ms": 10,
         "rendered_inputs": {"prior_outputs": {}}},
        {"step_id": "left", "status": "ok", "started_at_ms": 10, "ended_at_ms": 30,
         "rendered_inputs": {"prior_outputs": {"seed": "synthetic seed"}}},
        {"step_id": "right", "status": "ok", "started_at_ms": 12, "ended_at_ms": 35,
         "rendered_inputs": {"prior_outputs": {"seed": "synthetic seed"}}},
        {"step_id": "join", "status": "ok", "started_at_ms": 35, "ended_at_ms": 40,
         "rendered_inputs": {"prior_outputs": {"left": "synthetic left", "right": "extra output"}}},
    ]}
    return plan, trace


class ObjectiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="objective-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "artifacts"
        self.workspace.mkdir()
        self.task = self.root / "synthetic-task.json"
        self.task.write_text(json.dumps({"expected": "answer.txt"}), encoding="utf-8")
        self.upstream = self.root / "pinned"
        package = self.upstream / "benchmark" / "AgentSkillOS_bench"
        package.mkdir(parents=True)
        (package.parent / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.evaluator = package / "orchestrator.py"
        self.evaluator.write_text('''
import json, os
from pathlib import Path

class Result:
    def __init__(self, score):
        self.score = score
    def to_dict(self):
        return {"total_score": self.score, "max_score": 100,
                "summary": "合成结构检查", "evaluator_results": []}

def evaluate_task_sync(task_config_path, workspace_path):
    assert "PYTHONPATH" not in os.environ
    assert "SYNTHETIC_API_KEY" not in os.environ
    assert "OPENSQUILLA_STATE_DIR" not in os.environ
    workspace = Path(workspace_path)
    assert not Path(os.environ["TEMP"]).is_relative_to(workspace)
    assert not (workspace / ".metaskill-state").exists()
    expected = json.loads(Path(task_config_path).read_text(encoding="utf-8"))["expected"]
    print("synthetic evaluator diagnostic")
    return Result(100 if (workspace / expected).is_file() else 0)
''', encoding="utf-8")

    def test_subprocess_isolation_utf8_and_structural_score(self):
        (self.workspace / "answer.txt").write_text("synthetic artifact", encoding="utf-8")
        # A same-named package on inherited PYTHONPATH must not supply scoring.
        shadow = self.root / "shadow"
        package = shadow / "benchmark" / "AgentSkillOS_bench"
        package.mkdir(parents=True)
        (package.parent / "__init__.py").write_text("raise RuntimeError('shadow imported')", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(shadow), "SYNTHETIC_API_KEY": "synthetic",
                                     "OPENSQUILLA_STATE_DIR": "not-a-real-profile"}):
            result = evaluate_artifacts(self.task, self.workspace, self.upstream)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["total_score"], 100)
        self.assertEqual(result["score_kind"], "official_objective")
        self.assertEqual(result["evaluator_result"]["summary"], "合成结构检查")
        self.assertFalse((self.workspace / ".metaskill-state").exists())
        self.assertFalse(list(self.upstream.rglob("*.pyc")), "Evaluation mutated the immutable prepared cache")

    def test_partial_or_missing_artifact_is_a_real_zero(self):
        result = evaluate_artifacts(self.task, self.workspace, self.upstream)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["total_score"], 0)
        self.assertEqual(result["max_score"], 100)
        self.assertIsNone(result["error"])

    def test_failed_evaluator_is_unscored(self):
        self.evaluator.write_text("raise RuntimeError('synthetic evaluator failure')", encoding="utf-8")
        result = evaluate_artifacts(self.task, self.workspace, self.upstream)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["total_score"])
        self.assertIsNone(result["max_score"])
        self.assertIn("synthetic evaluator failure", result["error"])

    def test_timeout_is_unscored(self):
        with patch("metaskill_benchmark.evaluate.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("synthetic", 300)):
            result = evaluate_artifacts(self.task, self.workspace, self.upstream)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["total_score"])
        self.assertIn("300 seconds", result["error"])

    def test_upstream_caught_handler_errors_are_not_task_failure_scores(self):
        for message in ("Evaluator exception: synthetic missing dependency",
                        "Unknown evaluator function: synthetic-op",
                        "Dependency not found: synthetic-node"):
            raw = {"total_score": 0, "max_score": 100,
                   "evaluator_results": [{"evaluator_id": "synthetic", "score": 0, "message": message}]}
            with self.subTest(message=message), patch("metaskill_benchmark.evaluate.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
                result = evaluate_artifacts(self.task, self.workspace, self.upstream)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["total_score"])
                self.assertEqual(result["evaluator_result"], raw)
                self.assertIn(message, result["error"])

    def test_missing_input_and_invalid_evaluator_scores_are_unscored(self):
        result = evaluate_artifacts(self.root / "missing.json", self.workspace, self.upstream)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["total_score"])
        for score in (-1, 101, True, float("nan")):
            with self.subTest(score=score), patch("metaskill_benchmark.evaluate.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"total_score": score, "max_score": 100}), "")
                result = evaluate_artifacts(self.task, self.workspace, self.upstream)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["total_score"])


class TraceAuditTests(unittest.TestCase):
    def test_diamond_counts_declared_bindings_and_unrelated_overlap(self):
        plan, trace = diamond()
        result = audit_trace(plan, trace)
        self.assertEqual(result["conformance_status"], "valid")
        self.assertEqual(result["expected_nodes"], 4)
        self.assertEqual(result["completed_nodes"], 4)
        self.assertEqual(result["dependency_edges"], 4)
        self.assertEqual(result["dependency_violations"], 0)
        self.assertEqual(result["explicit_refs"], 3)
        self.assertEqual(result["populated_refs"], 3)
        self.assertTrue(result["parallel_observed"])

    def test_missing_trace_cannot_pass_or_report_zero_violations(self):
        plan, _ = diamond()
        for trace in (None, {}, {"steps": "not-a-list"}):
            with self.subTest(trace=trace):
                result = audit_trace(plan, trace)
                self.assertEqual(result["conformance_status"], "unknown")
                self.assertIsNone(result["completed_nodes"])
                self.assertIsNone(result["dependency_violations"])
                self.assertIsNone(result["populated_refs"])
                self.assertIsNone(result["parallel_observed"])
                self.assertEqual(result["unknown_nodes"], 4)
                self.assertEqual(result["binding_unknown"], 3)

    def test_missing_step_preserves_observed_counts_with_unknown_totals(self):
        plan, trace = diamond()
        del trace["steps"][2]
        result = audit_trace(plan, trace)
        self.assertEqual(result["conformance_status"], "unknown")
        self.assertEqual(result["completed_nodes"], 3)
        self.assertEqual(result["missing_nodes"], 1)
        self.assertEqual(result["dependency_unknown"], 2)
        self.assertIsNone(result["dependency_violations"])
        self.assertEqual(result["populated_refs_observed"], 2)
        self.assertEqual(result["binding_unknown"], 1)
        self.assertIsNone(result["populated_refs"])
        self.assertIsNone(result["parallel_observed"])

    def test_dependency_violation_is_not_evidence_of_valid_parallelism(self):
        plan = {"nodes": [
            {"id": "one", "depends_on": [], "inputs": []},
            {"id": "two", "depends_on": ["one"], "inputs": ["outputs.one"]},
        ]}
        trace = {"steps": [
            {"step_id": "one", "status": "ok", "started_at_ms": 0, "ended_at_ms": 20},
            {"step_id": "two", "status": "ok", "started_at_ms": 10, "ended_at_ms": 30,
             "rendered_inputs": {"prior_outputs": {"one": "synthetic"}}},
        ]}
        result = audit_trace(plan, trace)
        self.assertEqual(result["dependency_violations"], 1)
        self.assertEqual(result["conformance_status"], "invalid")
        self.assertFalse(result["parallel_observed"])

    def test_truncated_redacted_omitted_or_unparseable_bindings_are_unknown(self):
        variants = [
            {"rendered_inputs_truncated": True},
            {"truncated_fields": ["rendered_inputs_json"]},
            {"rendered_inputs": {"__trace_field_truncated__": True}},
            {"rendered_inputs": {"_redaction_overflow": True}},
            {"rendered_inputs": {"prior_outputs": {"seed": "[REDACTED]"}}},
            {"rendered_inputs": {"prior_outputs": {"seed": "clipped…"}}},
            {"rendered_inputs": {"prior_outputs": {}}},
            {"rendered_inputs": None, "rendered_inputs_json": "not-json"},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                plan, trace = diamond()
                trace["steps"][1].update(changes)
                result = audit_trace(plan, trace)
                self.assertEqual(result["conformance_status"], "unknown")
                self.assertEqual(result["binding_unknown"], 1)
                self.assertEqual(result["populated_refs_observed"], 2)
                self.assertIsNone(result["populated_refs"])

    def test_decoded_native_json_column_is_supported(self):
        plan, trace = diamond()
        for step in trace["steps"]:
            step["rendered_inputs_json"] = json.dumps(step.pop("rendered_inputs"))
        self.assertEqual(audit_trace(plan, trace)["conformance_status"], "valid")

    def test_blank_binding_failed_step_and_duplicate_records_are_invalid(self):
        plan, trace = diamond()
        for mutate in (
            lambda value: value["steps"][1]["rendered_inputs"]["prior_outputs"].update(seed=" "),
            lambda value: value["steps"][1].update(status="failed"),
            lambda value: value["steps"].append(deepcopy(value["steps"][1])),
        ):
            altered = deepcopy(trace)
            mutate(altered)
            self.assertEqual(audit_trace(plan, altered)["conformance_status"], "invalid")

    def test_invalid_plans_do_not_become_unknown_when_trace_missing(self):
        for plan in (
            {"nodes": []},
            {"nodes": [{"id": "a"}, {"id": "a"}]},
            {"nodes": [{"id": "a", "depends_on": ["missing"]}]},
            {"nodes": [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]},
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                audit_trace(plan, None)


class SummaryTests(unittest.TestCase):
    def test_retries_do_not_inflate_runs_and_failed_partial_scores_remain(self):
        records = [
            {"task_id": "synthetic-a", "trial": 1, "attempt": 1, "status": "failed",
             "objective": {"status": "completed", "total_score": 0}},
            {"task_id": "synthetic-a", "trial": 1, "attempt": 2, "status": "completed",
             "objective": {"status": "completed", "total_score": 100},
             "conformance": {"conformance_status": "valid"}},
            {"task_id": "synthetic-a", "trial": 2, "attempt": 1, "status": "timeout",
             "objective": {"status": "completed", "total_score": 0}},
            {"task_id": "synthetic-b", "trial": 1, "attempt": 1, "status": "preparation_failed",
             "missing_inputs": ["synthetic-missing.txt"],
             "objective": {"status": "error", "total_score": None}},
            {"task_id": "synthetic-b", "trial": 2, "attempt": 1, "status": "evaluation_failed",
             "objective": {"status": "error", "total_score": None}},
        ]
        # Selection uses attempt number even when records arrive out of order.
        result = summarize([records[1], records[0], *records[2:]])
        self.assertEqual(result["effective"]["run_count"], 4)
        self.assertEqual(result["effective"]["completed"], 1)
        self.assertEqual(result["effective"]["failed"], 3)
        self.assertEqual(result["effective"]["noinput"], 1)
        self.assertEqual(result["effective"]["objective_scored"], 2)
        self.assertEqual(result["effective"]["objective_unscored"], 2)
        self.assertEqual(result["effective"]["mean_objective_score"], 50)
        self.assertEqual(result["effective"]["objective_stddev"], 50)
        self.assertEqual(result["effective"]["objective_min"], 0)
        self.assertEqual(result["effective"]["objective_max"], 100)
        self.assertEqual(result["all_attempts"]["run_count"], 5)
        self.assertAlmostEqual(result["all_attempts"]["mean_objective_score"], 100 / 3)
        self.assertEqual(result["by_task"]["synthetic-a"]["run_count"], 2)
        self.assertEqual(result["by_trial"]["1"]["run_count"], 2)

    def test_empty_summary_has_no_synthetic_zero_score(self):
        result = summarize([])
        self.assertEqual(result["effective"]["run_count"], 0)
        self.assertIsNone(result["effective"]["mean_objective_score"])
        self.assertIsNone(result["effective"]["objective_stddev"])
        self.assertEqual(result["effective_runs"], [])

    def test_conformance_totals_preserve_partial_and_missing_evidence(self):
        plan, trace = diamond()
        valid = audit_trace(plan, trace)
        missing = audit_trace(plan, None)
        records = [
            {"task_id": "a", "trial": 1, "status": "completed", "conformance": valid},
            {"task_id": "a", "trial": 2, "status": "failed", "conformance": missing},
        ]
        totals = summarize(records)["effective"]["conformance_totals"]
        self.assertEqual(totals["expected_nodes"], 8)
        self.assertIsNone(totals["completed_nodes"])
        self.assertEqual(totals["completed_nodes_observed"], 4)
        self.assertEqual(totals["unknown_nodes"], 4)
        self.assertEqual(totals["dependency_edges"], 8)
        self.assertIsNone(totals["dependency_violations"])
        self.assertEqual(totals["dependency_unknown"], 4)
        self.assertEqual(totals["explicit_refs"], 6)
        self.assertIsNone(totals["populated_refs"])
        self.assertEqual(totals["populated_refs_observed"], 3)
        self.assertEqual(totals["binding_unknown"], 3)
        self.assertEqual(totals["parallel_runs_observed"], 1)
        self.assertEqual(totals["parallel_runs_unknown"], 1)
        records.append({"task_id": "b", "trial": 1, "status": "preparation_failed"})
        totals = summarize(records)["effective"]["conformance_totals"]
        self.assertEqual(totals["missing_audits"], 1)
        self.assertIsNone(totals["expected_nodes"])
        self.assertEqual(totals["completed_nodes_observed"], 4)

    def test_run_coordinates_are_explicit_and_positive(self):
        for record in ({"task_id": "a", "trial": True}, {"task_id": "a", "trial": 0},
                       {"task_id": "a", "trial": 1, "attempt": 0}, {"task_id": "", "trial": 1}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                summarize([record])


if __name__ == "__main__":
    unittest.main()
