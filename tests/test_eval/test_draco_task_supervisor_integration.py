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
    supervised_main = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_amain_with_run_lock"
    )
    def is_cancel_and_wait(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "cancel_and_wait"
        )

    handler = next(
        node
        for node in ast.walk(supervised_main)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "BaseException"
        and any(is_cancel_and_wait(child) for child in ast.walk(node))
    )

    cancel_call = next(
        node
        for node in ast.walk(handler)
        if is_cancel_and_wait(node)
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
