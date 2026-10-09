from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script_module():
    path = Path("scripts/eval_subagent_delegation.py")
    spec = importlib.util.spec_from_file_location("eval_subagent_delegation", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    module = _load_script_module()
    dataset = tmp_path / "cases.json"
    dataset.write_text(
        json.dumps(
            [
                {"id": "same", "prompt": "one", "expected_delegation": False},
                {"id": "same", "prompt": "two", "expected_delegation": True},
            ]
        ),
        encoding="utf-8",
    )

    try:
        module._load_cases(dataset)
    except ValueError as exc:
        assert "duplicate case id" in str(exc)
    else:
        raise AssertionError("duplicate case ids must be rejected")


def test_candidate_tool_uses_authoritative_delegate_contract() -> None:
    module = _load_script_module()

    tool = module._candidate_tool()

    assert tool.name == "delegate_task"
    assert set(tool.input_schema.required) == {
        "task",
        "task_key",
        "acceptance_criteria",
    }


def test_metrics_separate_dispatch_recall_directness_fanout_and_truncation() -> None:
    module = _load_script_module()
    metrics = module._metrics(
        [
            {
                "expected_delegation": False,
                "delegated": False,
                "delegate_call_count": 0,
                "finish_reason": "stop",
                "latency_ms": 10,
                "usage": {},
                "error": "",
            },
            {
                "expected_delegation": True,
                "delegated": True,
                "delegate_call_count": 2,
                "minimum_delegate_calls": 2,
                "minimum_fanout_satisfied": True,
                "minimum_task_contract_count": 2,
                "finish_reason": "tool_calls",
                "latency_ms": 20,
                "usage": {},
                "error": "",
            },
            {
                "expected_delegation": True,
                "delegated": False,
                "delegate_call_count": 0,
                "minimum_delegate_calls": 2,
                "minimum_fanout_satisfied": False,
                "finish_reason": "length",
                "latency_ms": 30,
                "usage": {},
                "error": "",
            },
        ]
    )

    assert metrics["decomposable_delegation_rate"] == 0.5
    assert metrics["simple_direct_rate"] == 1.0
    assert metrics["balanced_accuracy"] == 0.75
    assert metrics["minimum_fanout_satisfied_rate"] == 0.5
    assert metrics["minimum_task_contract_rate"] == 1.0
    assert metrics["length_limited_count"] == 1
