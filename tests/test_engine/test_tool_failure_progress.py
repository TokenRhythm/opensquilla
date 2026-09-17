"""Recovery boundaries distinguish repaired checks from repeated diagnostics."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine import DoneEvent, ErrorEvent, ToolCall, ToolResult
from opensquilla.execution_status import execution_status_for_tool_result
from opensquilla.tool_boundary import ToolEffectOutcome
from opensquilla.tools.types import ToolContext
from tests.test_engine.test_tool_failure_recovery_edges import _agent, _EdgeProvider


def _write_verifier(workspace: Path, state: dict[str, bool]) -> tuple[Path, Path]:
    source = workspace / "state.json"
    source.write_text(json.dumps(state), encoding="utf-8")
    checker = workspace / "verify_state.py"
    checker.write_text(
        "import json, sys\n"
        "with open(sys.argv[1], encoding='utf-8') as stream:\n"
        "    state = json.load(stream)\n"
        "failed = sorted(key for key, ready in state.items() if not ready)\n"
        "print('failing checks:', ','.join(failed))\n"
        "sys.exit(bool(failed))\n",
        encoding="utf-8",
    )
    return checker, source


async def _verify(call: ToolCall, checker: Path, source: Path) -> ToolResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(checker), str(source),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    text = output.decode("utf-8").replace("\r\n", "\n")
    content = f"exit_code={process.returncode}\n{text}"
    return ToolResult(
        call.tool_use_id, call.tool_name, content,
        is_error=bool(process.returncode),
        execution_status=execution_status_for_tool_result("exec_command", content),
    )


def _record_repair(call: ToolCall, context: ToolContext, *, path: str, evidence: str) -> ToolResult:
    result = ToolResult(call.tool_use_id, call.tool_name, "Updated source")
    if evidence == "workspace_receipt":
        context.workspace_mutation_receipts.append({"path": path, "operation": "write"})
    else:
        result.effect_outcome = ToolEffectOutcome(
            effect_state="committed", retry_policy="same_turn", loop_action="continue",
            outcome_code="source_updated",
        )
    return result


def _assert_final(events: list[Any], provider: _EdgeProvider, *, exhausted: bool) -> None:
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) and event.text for event in events)
    assert (provider.requests[-1]["tools"] is None) is exhausted


@pytest.mark.parametrize("evidence", ["workspace_receipt", "committed_outcome"])
async def test_verified_partial_repairs_allow_same_command_to_reach_success(
    tmp_path: Path, evidence: str,
) -> None:
    checker, source = _write_verifier(tmp_path, {"alpha": False, "beta": False, "gamma": False})
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "python verify_state.py state.json"})
    operations = [probe]
    for field in ("alpha", "beta", "gamma"):
        operations.extend([("repair", {"field": field}), probe])
    provider = _EdgeProvider([[operation] for operation in operations])
    observed: list[tuple[str, dict[str, Any]]] = []
    outcomes: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        observed.append((call.tool_name, call.arguments))
        if call.tool_name == "repair":
            state = json.loads(source.read_text(encoding="utf-8"))
            state[call.arguments["field"]] = True
            source.write_text(json.dumps(state), encoding="utf-8")
            return _record_repair(call, context, path=source.name, evidence=evidence)
        result = await _verify(call, checker, source)
        outcomes.append(result.content)
        return result

    agent = _agent(provider, handler, context=context, timeout=5)
    events = [event async for event in agent.run_turn("Repair each failing check and verify it.")]

    assert observed == operations
    assert outcomes == [
        "exit_code=1\nfailing checks: alpha,beta,gamma\n",
        "exit_code=1\nfailing checks: beta,gamma\n",
        "exit_code=1\nfailing checks: gamma\n",
        "exit_code=0\nfailing checks: \n",
    ]
    assert all(json.loads(source.read_text(encoding="utf-8")).values())
    _assert_final(events, provider, exhausted=False)


@pytest.mark.parametrize("diagnostics", ["none", "prose", "json"])
async def test_unrelated_writes_do_not_renew_unchanged_failed_verifier(
    tmp_path: Path, diagnostics: str,
) -> None:
    checker, source = _write_verifier(tmp_path, {"alpha": False})
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "python verify_state.py state.json"})
    operations = [operation for _ in range(6) for operation in (probe, ("repair", {}))]
    provider = _EdgeProvider([[operation] for operation in operations])
    checks = 0
    writes = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal checks, writes
        if call.tool_name == "repair":
            writes += 1
            (tmp_path / "notes.txt").write_text(f"Review pass {writes}\n", encoding="utf-8")
            return _record_repair(
                call, context, path="notes.txt", evidence="workspace_receipt",
            )
        checks += 1
        result = await _verify(call, checker, source)
        if diagnostics == "prose":
            result.content += (
                f"attempt={checks}\n"
                f"timestamp=2030-01-01T00:00:{checks:02d}Z\n"
                f"elapsed=0.{checks}s\n"
            )
        elif diagnostics == "json":
            result.content = json.dumps({
                "code": "CHECK_FAILED", "output": result.content, "attempt": checks,
                "timestamp": f"2030-01-01T00:00:{checks:02d}Z",
                "duration_ms": checks, "call_id": f"synthetic-call-{checks}",
            })
        return result

    agent = _agent(provider, handler, context=context, timeout=5)
    events = [event async for event in agent.run_turn("Repair the failing check and verify it.")]

    assert checks == 3
    assert writes == 2
    assert json.loads(source.read_text(encoding="utf-8")) == {"alpha": False}
    _assert_final(events, provider, exhausted=True)


async def test_different_diagnostics_without_recorded_repair_do_not_renew_budget() -> None:
    provider = _EdgeProvider([[('exec_command', {"command": "python verify_state.py"})]] * 6)
    checks = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal checks
        checks += 1
        content = f"exit_code=1\nfailing check: sample_{checks}\n"
        return ToolResult(
            call.tool_use_id, call.tool_name, content, is_error=True,
            execution_status=execution_status_for_tool_result("exec_command", content),
        )

    events = [
        event async for event in _agent(provider, handler).run_turn("Verify the sample checks.")
    ]

    assert checks == 3
    _assert_final(events, provider, exhausted=True)


async def test_repairs_that_alternate_previous_failures_remain_bounded(tmp_path: Path) -> None:
    checker, source = _write_verifier(tmp_path, {"alpha": False, "beta": True})
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "python verify_state.py state.json"})
    operations = [operation for _ in range(8) for operation in (probe, ("repair", {}))]
    provider = _EdgeProvider([[operation] for operation in operations])
    outcomes: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        if call.tool_name == "repair":
            state = json.loads(source.read_text(encoding="utf-8"))
            source.write_text(
                json.dumps({field: not ready for field, ready in state.items()}), encoding="utf-8",
            )
            return _record_repair(
                call, context, path=source.name, evidence="workspace_receipt",
            )
        result = await _verify(call, checker, source)
        outcomes.append(result.content)
        return result

    agent = _agent(provider, handler, context=context, timeout=5)
    events = [event async for event in agent.run_turn("Repair both checks and verify success.")]

    assert outcomes == [
        "exit_code=1\nfailing checks: alpha\n",
        "exit_code=1\nfailing checks: beta\n",
        "exit_code=1\nfailing checks: alpha\n",
        "exit_code=1\nfailing checks: beta\n",
    ]
    _assert_final(events, provider, exhausted=True)


async def test_unrelated_write_and_changed_runtime_message_do_not_refund_capability_attempts(
    tmp_path: Path,
) -> None:
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "node --version"})
    operations = [operation for _ in range(6) for operation in (probe, ("repair", {}))]
    provider = _EdgeProvider([[operation] for operation in operations])
    checks = 0
    writes = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal checks, writes
        if call.tool_name == "repair":
            writes += 1
            (tmp_path / "notes.txt").write_text(f"Review pass {writes}\n", encoding="utf-8")
            return _record_repair(
                call, context, path="notes.txt", evidence="workspace_receipt",
            )
        checks += 1
        content = json.dumps({
            "status": "failed", "code": "RUNTIME_UNAVAILABLE", "componentId": "node",
            "retryable": False, "message": f"Synthetic unavailable runtime, attempt {checks}",
        })
        return ToolResult(
            call.tool_use_id, call.tool_name, content, is_error=True,
            execution_status=execution_status_for_tool_result("exec_command", content),
        )

    agent = _agent(provider, handler, context=context)
    events = [event async for event in agent.run_turn("Verify the runtime is available.")]

    assert checks == 2
    assert writes == 1
    _assert_final(events, provider, exhausted=True)


async def test_temporary_path_noise_and_unrelated_writes_cannot_renew_forever(
    tmp_path: Path,
) -> None:
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "pytest test_sample.py -q"})
    operations = [operation for _ in range(10) for operation in (probe, ("repair", {}))]
    provider = _EdgeProvider([[operation] for operation in operations])
    checks = 0
    writes = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal checks, writes
        if call.tool_name == "repair":
            writes += 1
            (tmp_path / "notes.txt").write_text(f"Review pass {writes}\n", encoding="utf-8")
            return _record_repair(
                call, context, path="notes.txt", evidence="workspace_receipt",
            )
        checks += 1
        # The assertion is unchanged; only the test runner's temporary directory
        # changes between executions, independently of writes to the notes file.
        content = (
            "exit_code=1\nE AssertionError: output is missing\nE assert False\n"
            f"E + where exists = PosixPath('/tmp/pytest-of-sample/pytest-{checks}/"
            "test_sample0/result.txt').exists\n1 failed in 0.01s\n"
        )
        return ToolResult(
            call.tool_use_id, call.tool_name, content, is_error=True,
            execution_status=execution_status_for_tool_result("exec_command", content),
        )

    events = [
        event async for event in _agent(provider, handler, context=context).run_turn(
            "Verify the sample output."
        )
    ]

    assert checks <= 6
    assert writes == checks - 1
    _assert_final(events, provider, exhausted=True)


async def test_partial_repairs_with_new_evidence_eventually_exhaust_renewals(
    tmp_path: Path,
) -> None:
    fields = [f"check_{index}" for index in range(10)]
    checker, source = _write_verifier(tmp_path, dict.fromkeys(fields, False))
    context = ToolContext(workspace_dir=str(tmp_path))
    probe = ("exec_command", {"command": "python verify_state.py state.json"})
    operations = [probe]
    for field in fields:
        operations.extend([("repair", {"field": field}), probe])
    provider = _EdgeProvider([[operation] for operation in operations])
    outcomes: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        if call.tool_name == "repair":
            state = json.loads(source.read_text(encoding="utf-8"))
            state[call.arguments["field"]] = True
            source.write_text(json.dumps(state), encoding="utf-8")
            return _record_repair(
                call, context, path=source.name, evidence="workspace_receipt",
            )
        result = await _verify(call, checker, source)
        outcomes.append(result.content)
        return result

    agent = _agent(provider, handler, context=context, timeout=5)
    events = [event async for event in agent.run_turn("Repair the sample checks.")]

    assert len(outcomes) == 6
    assert len(set(outcomes)) == len(outcomes)
    state = json.loads(source.read_text(encoding="utf-8"))
    assert sum(state.values()) == 5
    assert not all(state.values())
    _assert_final(events, provider, exhausted=True)
