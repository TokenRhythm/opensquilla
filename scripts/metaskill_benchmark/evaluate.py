"""Local objective evaluation and invocation-level coordination auditing.

No pairwise judging is performed here. Objective scores measure the upstream
task's structural checks, rather than relative or absolute artifact quality.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
from typing import Any

OBJECTIVE_TIMEOUT_SECONDS = 300
_REFERENCE = re.compile(r"\boutputs\.([A-Za-z0-9_-]+)")
_EVALUATE_CHILD = """
import contextlib, json, pathlib, sys
upstream = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(upstream))
with contextlib.redirect_stdout(sys.stderr):
    from benchmark.AgentSkillOS_bench import orchestrator
    if not pathlib.Path(orchestrator.__file__).resolve().is_relative_to(upstream):
        raise RuntimeError('Evaluator was imported outside the pinned checkout')
    result = orchestrator.evaluate_task_sync(sys.argv[2], sys.argv[3]).to_dict()
print(json.dumps(result, ensure_ascii=False, allow_nan=False))
"""


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def evaluate_artifacts(task_config: Path, workspace: Path, upstream_root: Path) -> dict:
    """Call the official evaluator in a separate interpreter.

    Preparation must verify the upstream checkout's pinned revision before
    calling this function. A failed execution can still have its partial
    artifacts evaluated; evaluator failure is never represented as score zero.
    """
    outcome = {
        "status": "error", "score_kind": "official_objective",
        "total_score": None, "max_score": None,
        "evaluator_result": None, "error": None,
    }
    task_config = task_config.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    upstream_root = upstream_root.expanduser().resolve()
    try:
        if not task_config.is_file():
            raise ValueError("Task configuration is missing")
        if not workspace.is_dir():
            raise ValueError("Artifact workspace is missing")
        if not (upstream_root / "benchmark/AgentSkillOS_bench/orchestrator.py").is_file():
            raise ValueError("The pinned upstream objective evaluator is missing")
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("OPENSQUILLA_")
            and not key.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))
            and key.upper() not in {"PYTHONPATH", "PYTHONHOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        }
        # The upstream evaluator recursively searches the artifact workspace.
        # Evaluation state must stay outside it, including during scoring.
        with tempfile.TemporaryDirectory(prefix="metaskill-objective-") as directory:
            env.update(TMP=directory, TEMP=directory, TMPDIR=directory,
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            process = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-I", "-c", _EVALUATE_CHILD, str(upstream_root),
                 str(task_config), str(workspace)],
                cwd=directory, env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=OBJECTIVE_TIMEOUT_SECONDS,
                check=False,
            )
        if process.returncode:
            raise RuntimeError(f"Official evaluator exited {process.returncode}: {process.stderr[-4000:].strip()}")
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or not _number(result.get("total_score")) or not _number(result.get("max_score")):
            raise RuntimeError("Official evaluator returned an invalid score result")
        if result["max_score"] <= 0 or not 0 <= result["total_score"] <= result["max_score"]:
            raise RuntimeError("Official evaluator returned a score outside its declared range")
        # The pinned evaluator catches its own handler errors and assigns 0.
        # Retain its raw report, but do not treat an infrastructure error as
        # a measured task failure in benchmark aggregates.
        outcome["evaluator_result"] = result
        checks = result.get("evaluator_results", [])
        if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
            raise RuntimeError("Official evaluator returned invalid check records")
        errors = [item for item in checks
                  if isinstance(item, dict) and isinstance(item.get("message"), str)
                  and item["message"].startswith(("Evaluator exception:", "Unknown evaluator function:",
                                                   "Dependency not found:"))]
        if errors:
            outcome["evaluation_errors"] = errors
            raise RuntimeError("Official objective checks did not complete: "
                               + "; ".join(item["message"] for item in errors))
        outcome.update(status="completed", total_score=result["total_score"],
                       max_score=result["max_score"], evaluator_result=result)
    except subprocess.TimeoutExpired:
        outcome["error"] = f"Official objective evaluation exceeded {OBJECTIVE_TIMEOUT_SECONDS} seconds"
    except (OSError, ValueError, RuntimeError) as exc:
        outcome["error"] = str(exc)
    return outcome


def _strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _rendered_inputs(step: dict) -> dict | None:
    if step.get("rendered_inputs_truncated") is True:
        return None
    marks = step.get("truncated_fields", [])
    if not isinstance(marks, (list, tuple, set, frozenset)):
        return None
    if any(str(mark).startswith("rendered_inputs") for mark in marks):
        return None
    value = step.get("rendered_inputs")
    if value is None and isinstance(step.get("rendered_inputs_json"), str):
        try:
            value = json.loads(step["rendered_inputs_json"])
        except ValueError:
            return None
    if (not isinstance(value, dict) or value.get("_redaction_overflow") is True
            or value.get("__trace_field_truncated__") is True):
        return None
    return value


def audit_trace(plan: dict, trace: dict | None) -> dict:
    """Check a canonical plan against a native trace without inventing evidence.

    Count references declared in canonical ``nodes[].inputs``. The renderer
    can supply more dependency outputs; these do not inflate declared counts.
    Missing, redacted or clipped binding evidence is unknown, never a pass.
    """
    if not isinstance(plan, dict):
        raise ValueError("Conformance requires a canonical plan object")
    nodes = plan.get("nodes")
    if not isinstance(nodes, list) or not nodes or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("Conformance requires a non-empty canonical nodes list")
    ids = [node.get("id") for node in nodes]
    if any(not isinstance(node_id, str) or not node_id for node_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("Canonical node identities must be non-empty and unique")
    node_ids = set(ids)
    dependencies = {}
    declarations = {}
    for node in nodes:
        deps = node.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) or dep not in node_ids for dep in deps):
            raise ValueError("Canonical dependency refers to an undefined node")
        dependencies[node["id"]] = set(deps)
        declarations[node["id"]] = [
            tuple(_REFERENCE.findall(value))
            for value in _strings(node.get("inputs", [])) if _REFERENCE.search(value)
        ]
    expected_refs = sum(map(len, declarations.values()))
    expected_edges = sum(map(len, dependencies.values()))
    # Validate the plan even when no trace exists. Concurrency excludes
    # dependency-related pairs, including transitive predecessors.
    def ancestors(node_id: str, active: frozenset[str] = frozenset()) -> set[str]:
        if node_id in active:
            raise ValueError("Canonical plan contains a dependency cycle")
        found = set(dependencies[node_id])
        for predecessor in dependencies[node_id]:
            found |= ancestors(predecessor, active | {node_id})
        return found

    ancestry = {node_id: ancestors(node_id) for node_id in ids}
    result = {
        "trace_available": isinstance(trace, dict),
        "expected_nodes": len(nodes), "completed_nodes": None,
        "missing_nodes": len(nodes), "failed_nodes": 0, "unknown_nodes": len(nodes),
        "dependency_edges": expected_edges, "dependency_violations": None,
        "dependency_violations_observed": 0, "dependency_unknown": expected_edges,
        "explicit_refs": expected_refs, "populated_refs": None,
        "populated_refs_observed": 0, "binding_invalid": 0, "binding_unknown": expected_refs,
        "parallel_observed": None, "conformance_status": "unknown", "issues": [],
    }
    if not isinstance(trace, dict) or not isinstance(trace.get("steps"), list):
        result["issues"].append("Trace or step records are unavailable")
        return result
    records = {}
    duplicate = False
    malformed = False
    for step in trace["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), str):
            malformed = True
            result["issues"].append("Malformed step record")
            continue
        if step["step_id"] in records:
            duplicate = True
            result["issues"].append(f"Repeated invocation record: {step['step_id']}")
        records[step["step_id"]] = step
    unexpected = set(records) - node_ids
    if unexpected:
        result["issues"].append("Trace contains nodes outside the canonical plan")
    completed = failed = unknown_nodes = 0
    intervals = {}
    invalid_timing = False
    for node_id in ids:
        step = records.get(node_id)
        if step is None or step.get("status") not in {"ok", "failed", "cancelled"}:
            unknown_nodes += 1
        elif step["status"] == "ok":
            completed += 1
        else:
            failed += 1
        if step is None:
            continue
        start, end = step.get("started_at_ms"), step.get("ended_at_ms")
        if _number(start) and _number(end):
            if end < start:
                invalid_timing = True
                result["issues"].append(f"Node finishes before it starts: {node_id}")
            else:
                intervals[node_id] = (start, end)
    violations = unknown_edges = 0
    for node_id, deps in dependencies.items():
        for predecessor in deps:
            if node_id not in intervals or predecessor not in intervals:
                unknown_edges += 1
            elif intervals[predecessor][1] > intervals[node_id][0]:
                violations += 1
    populated = invalid_bindings = unknown_bindings = 0
    for node_id, fields in declarations.items():
        rendered = _rendered_inputs(records[node_id]) if node_id in records else None
        prior = rendered.get("prior_outputs") if rendered is not None else None
        for refs in fields:
            if any(ref not in node_ids for ref in refs):
                invalid_bindings += 1
            elif not isinstance(prior, dict) or any(ref not in prior or ref not in records for ref in refs):
                unknown_bindings += 1
            elif any(not isinstance(prior[ref], str) or not prior[ref].strip() for ref in refs):
                invalid_bindings += 1
            elif any("[REDACTED]" in prior[ref] or prior[ref].endswith("…") for ref in refs):
                unknown_bindings += 1
            else:
                populated += 1
    overlapping = any(
        left not in ancestry[right] and right not in ancestry[left]
        and max(intervals[left][0], intervals[right][0]) < min(intervals[left][1], intervals[right][1])
        for index, left in enumerate(ids) for right in ids[index + 1:]
        if left in intervals and right in intervals
    )
    parallel = True if overlapping else (False if len(intervals) == len(nodes) else None)
    missing = len(node_ids - records.keys())
    invalid = bool(failed or violations or invalid_bindings or duplicate or unexpected or invalid_timing)
    uncertain = bool(malformed or unknown_nodes or unknown_edges or unknown_bindings or len(intervals) != len(nodes))
    result.update(
        completed_nodes=completed, missing_nodes=missing, failed_nodes=failed,
        unknown_nodes=unknown_nodes, dependency_violations=violations if not unknown_edges else None,
        dependency_violations_observed=violations, dependency_unknown=unknown_edges,
        populated_refs=populated if not unknown_bindings else None,
        populated_refs_observed=populated, binding_invalid=invalid_bindings,
        binding_unknown=unknown_bindings, parallel_observed=parallel,
        conformance_status="invalid" if invalid else ("unknown" if uncertain else "valid"),
    )
    return result


def _conformance_totals(records: list[dict]) -> dict:
    audits = [record.get("conformance") for record in records]

    def observed(key: str) -> int:
        return sum(audit[key] for audit in audits
                   if isinstance(audit, dict) and isinstance(audit.get(key), int)
                   and not isinstance(audit[key], bool) and audit[key] >= 0)

    def total(key: str) -> int | None:
        if any(not isinstance(audit, dict) or not isinstance(audit.get(key), int)
               or isinstance(audit[key], bool) or audit[key] < 0 for audit in audits):
            return None
        return observed(key)

    result = {key: total(key) for key in (
        "expected_nodes", "failed_nodes", "unknown_nodes", "dependency_edges",
        "dependency_violations", "dependency_unknown", "explicit_refs", "populated_refs",
        "binding_invalid", "binding_unknown",
    )}
    completed = total("completed_nodes")
    result.update(
        completed_nodes=completed if result["unknown_nodes"] == 0 else None,
        completed_nodes_observed=observed("completed_nodes"),
        dependency_violations_observed=observed("dependency_violations_observed"),
        populated_refs_observed=observed("populated_refs_observed"),
        missing_audits=sum(not isinstance(audit, dict) for audit in audits),
        parallel_runs_observed=sum(isinstance(audit, dict) and audit.get("parallel_observed") is True for audit in audits),
        parallel_runs_unknown=sum(not isinstance(audit, dict) or audit.get("parallel_observed") is None for audit in audits),
    )
    return result


def _summarize_group(records: list[dict]) -> dict:
    counts = Counter(str(record.get("status", "unknown")) for record in records)
    scores = [record["objective"]["total_score"] for record in records
              if isinstance(record.get("objective"), dict)
              and record["objective"].get("status") == "completed"
              and _number(record["objective"].get("total_score"))]
    conformance = Counter(
        record.get("conformance", {}).get("conformance_status", "unknown")
        if isinstance(record.get("conformance"), dict) else "unknown" for record in records
    )
    return {
        "run_count": len(records), "status_counts": dict(sorted(counts.items())),
        "completed": counts["completed"],
        "failed": sum(counts[status] for status in ("failed", "timeout", "preparation_failed", "evaluation_failed")),
        "noinput": sum(bool(record.get("noinput") or record.get("missing_inputs")
                            or record.get("status") == "noinput") for record in records),
        "objective_scored": len(scores), "objective_unscored": len(records) - len(scores),
        "mean_objective_score": sum(scores) / len(scores) if scores else None,
        "objective_stddev": statistics.pstdev(scores) if scores else None,
        "objective_stddev_kind": "population across scored runs",
        "objective_min": min(scores) if scores else None,
        "objective_max": max(scores) if scores else None,
        "score_kind": "official_objective", "conformance_counts": dict(sorted(conformance.items())),
        "conformance_totals": _conformance_totals(records),
    }


def summarize(run_records: list[dict]) -> dict:
    """Summarize latest attempts per task/repetition, retaining attempt counts.

    Scored failed executions remain in objective summaries. Missing objective
    evaluations remain unscored; they are never filled with zero or dropped
    from the effective-run denominator.
    """
    selected = {}
    for index, record in enumerate(run_records):
        if (not isinstance(record, dict) or not isinstance(record.get("task_id"), str)
                or not record["task_id"] or not isinstance(record.get("trial"), int)
                or isinstance(record["trial"], bool) or record["trial"] < 1):
            raise ValueError("Run metadata requires task_id and a positive integer trial")
        attempt = record.get("attempt", 1)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("Run metadata requires a positive attempt number")
        key = (record["task_id"], record["trial"])
        previous = selected.get(key)
        if previous is None or (attempt, index) > previous[:2]:
            selected[key] = (attempt, index, record)
    effective = [selected[key][2] for key in sorted(selected)]
    by_task = defaultdict(list)
    by_trial = defaultdict(list)
    for record in effective:
        by_task[record["task_id"]].append(record)
        by_trial[str(record["trial"])].append(record)
    return {
        "schema_version": 1, "scope": "MetaSkill-only local benchmark execution",
        "effective": _summarize_group(effective),
        "all_attempts": _summarize_group(run_records),
        "by_task": {key: _summarize_group(value) for key, value in sorted(by_task.items())},
        "by_trial": {key: _summarize_group(value) for key, value in sorted(by_trial.items())},
        "effective_runs": effective,
    }
