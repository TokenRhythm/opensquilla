from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "script_name",
    [
        "run_draco_routing_experiment.py",
        "run_draco_routing_experiment_resume.py",
    ],
)
def test_runner_cancels_workers_before_publishing_aborted_manifest(
    script_name: str,
) -> None:
    script_path = REPO_ROOT / "scripts" / script_name
    module = ast.parse(script_path.read_text(encoding="utf-8"))
    amain = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "amain"
    )
    handler = next(
        node
        for node in ast.walk(amain)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "BaseException"
    )

    cancel_call = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "cancel_and_wait"
    )
    manifest_call = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "write_manifest"
    )
    status = next(keyword.value for keyword in manifest_call.keywords if keyword.arg == "status")
    rows_written = next(
        keyword.value for keyword in manifest_call.keywords if keyword.arg == "rows_written"
    )
    reraises = [node for node in ast.walk(handler) if isinstance(node, ast.Raise)]

    assert isinstance(status, ast.Constant) and status.value == "aborted"
    assert isinstance(rows_written, ast.Name) and rows_written.id == "rows_persisted"
    assert reraises and any(node.exc is None for node in reraises)
    assert cancel_call.lineno < manifest_call.lineno < max(node.lineno for node in reraises)
