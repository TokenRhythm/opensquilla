"""Coverage gate for dispatch.py driven by the golden corpus.

Uses the stdlib ``trace`` module (no external dependency) to measure which
lines of the ``_handler`` closure in :func:`build_tool_handler` are executed
by ALL_CASES together.

Denominator: lines reported by ``code.co_lines()`` for the ``_handler`` code
object. This matches exactly the lines that ``trace`` can record — no
import-time, factory, or sibling-closure lines are included.

Target: >=95% line coverage of the _handler body. The remaining allowance is
reserved for invariant guards and bytecode layout shifts between CPython
releases.
"""

from __future__ import annotations

import asyncio
import importlib.util
import trace
import types
from pathlib import Path

import pytest
from test_tools.dispatch_corpus import ALL_CASES

import opensquilla.tools.dispatch as _dispatch_module
from opensquilla.engine.hooks import NoopToolHook
from opensquilla.result_budget import ToolRunBudgetExceededError, ToolRunBudgetPolicy
from opensquilla.tool_boundary import ToolCall, ToolContinuation
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec, current_tool_context

_DISPATCH_SOURCE: Path = Path(_dispatch_module.__file__).resolve()


# ---------------------------------------------------------------------------
# Executable line discovery via co_lines()
# ---------------------------------------------------------------------------

def _all_code_objects(code: types.CodeType):  # type: ignore[return]
    """Yield all code objects reachable from ``code`` (including nested closures)."""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _all_code_objects(const)


def _handler_executable_lines() -> set[int]:
    """Return executable line numbers inside the _handler closure."""
    spec = importlib.util.spec_from_file_location(
        _dispatch_module.__name__, str(_DISPATCH_SOURCE)
    )
    module_code = spec.loader.get_code(_dispatch_module.__name__)  # type: ignore[union-attr]

    executable: set[int] = set()
    build_tool_handler_code = next(
        (
            code
            for code in _all_code_objects(module_code)
            if code.co_name == "build_tool_handler"
        ),
        None,
    )
    if build_tool_handler_code is None:
        return executable
    for code in _all_code_objects(build_tool_handler_code):
        if code.co_name != "_handler":
            continue
        for _start, _end, lineno in code.co_lines():
            if lineno is not None:
                executable.add(lineno)
    return executable


# ---------------------------------------------------------------------------
# Executed line collection via stdlib trace
# ---------------------------------------------------------------------------

class _RaisingToolHook:
    """ToolHook that raises in every callback — drives the defensive
    ``except Exception`` branches around hook fan-out so the safety net
    locks them in place."""

    name = "raising_tool"

    def before_tool(self, call):  # type: ignore[no-untyped-def]
        raise RuntimeError("before_tool boom")

    def after_tool(self, call, outcome):  # type: ignore[no-untyped-def]
        raise RuntimeError("after_tool boom")


def _collect_executed_lines() -> set[int]:
    """Return lines of dispatch.py executed across ALL_CASES corpus runs.

    Each case runs three times:

    * ``hooks=None`` — legacy fast path, no hook fan-out.
    * ``hooks=(NoopToolHook(),)`` — hook seam happy path.
    * ``hooks=(_RaisingToolHook(),)`` — hook seam exception branches.

    All three paths must stay reachable; a regression that drops any of them
    will cut coverage well below the floor below.
    """

    tracer = trace.Trace(count=True, trace=False)

    async def _run_all() -> None:
        hook_variants: tuple[tuple, ...] = (
            (),
            (NoopToolHook(),),
            (_RaisingToolHook(),),
        )
        for hooks in hook_variants:
            for case in ALL_CASES:
                ctx = case.ctx_factory()
                registry = case.registry_factory()
                handler = build_tool_handler(
                    registry,
                    ctx,
                    known_skill_names=(
                        set(case.known_skill_names) if case.known_skill_names else None
                    ),
                    tool_hooks=hooks or None,
                )
                token = current_tool_context.set(None)
                if case.setup is not None:
                    case.setup()
                try:
                    await handler(case.tool_call)
                except Exception:
                    pass
                finally:
                    current_tool_context.reset(token)
                    if case.teardown is not None:
                        case.teardown()

        async def _run_coverage_only(
            *,
            tool_name: str,
            handler_exc: BaseException,
            hooks: tuple,
            continuation: ToolContinuation | None = None,
            arguments: dict[str, object] | None = None,
        ) -> None:
            registry = ToolRegistry()

            async def _handler(approval_id: str | None = None) -> str:
                raise handler_exc

            registry.register(
                ToolSpec(
                    name=tool_name,
                    description="coverage-only branch driver",
                    parameters={},
                    runtime_only_arguments=(
                        frozenset({"approval_id"})
                        if tool_name
                        in {
                            "coverage_runtime_approval_id",
                            "coverage_runtime_continuation",
                        }
                        else frozenset()
                    ),
                    result_budget_class="external",
                ),
                _handler,
            )
            ctx = ToolContext(
                tool_run_budget_key=f"coverage-{tool_name}",
                tool_run_budget_policy=ToolRunBudgetPolicy(
                    max_web_fetch_calls_per_turn=10,
                    max_single_fetch_chars=1_000,
                    max_external_text_chars_per_turn=1_000,
                ),
            )
            handler = build_tool_handler(
                registry,
                ctx,
                tool_hooks=hooks or None,
            )
            token = current_tool_context.set(None)
            try:
                await handler(
                    ToolCall(
                        tool_use_id=f"tc-{tool_name}",
                        tool_name=tool_name,
                        arguments=dict(arguments or {}),
                        continuation=continuation,
                    )
                )
            except BaseException:
                pass
            finally:
                current_tool_context.reset(token)

        for hooks in hook_variants:
            await _run_coverage_only(
                tool_name="coverage_cancel",
                handler_exc=asyncio.CancelledError(),
                hooks=hooks,
                continuation=ToolContinuation(
                    approval_id="approval-coverage",
                    tool_use_id="tc-coverage_cancel",
                    session_key="",
                ),
            )
            await _run_coverage_only(
                tool_name="coverage_runtime_approval_id",
                handler_exc=RuntimeError("must not execute"),
                hooks=hooks,
                arguments={"approval_id": "provider-supplied"},
            )
            await _run_coverage_only(
                tool_name="coverage_runtime_continuation",
                handler_exc=RuntimeError("continuation reached handler"),
                hooks=hooks,
                continuation=ToolContinuation(
                    approval_id="approval-runtime",
                    tool_use_id="tc-coverage_runtime_continuation",
                    session_key="",
                ),
            )
            await _run_coverage_only(
                tool_name="coverage_continuation_mismatch",
                handler_exc=RuntimeError("must not execute"),
                hooks=hooks,
                continuation=ToolContinuation(
                    approval_id="approval-mismatch",
                    tool_use_id="another-call",
                    session_key="another-session",
                ),
            )
            await _run_coverage_only(
                tool_name="coverage_budget_exhausted",
                handler_exc=ToolRunBudgetExceededError(
                    "coverage_budget_exhausted",
                    "coverage",
                ),
                hooks=hooks,
            )

    tracer.runfunc(asyncio.run, _run_all())

    executed: set[int] = set()
    for (filename, lineno), count in tracer.counts.items():
        if Path(filename).resolve() == _DISPATCH_SOURCE and count > 0:
            executed.add(lineno)
    return executed


# ---------------------------------------------------------------------------
# The gate test
# ---------------------------------------------------------------------------

def test_dispatch_handler_line_coverage_from_corpus() -> None:
    """Assert the corpus achieves >=95% line coverage of the _handler body.

    Denominator: all lines in the _handler closure as reported by co_lines().

    If this test fails, add a new corpus case targeting the uncovered branch,
    or document the branch here.
    """
    executable = _handler_executable_lines()
    if not executable:
        pytest.fail(
            "Could not determine executable lines for dispatch._handler — "
            "the _handler closure may have been renamed or moved out of "
            "build_tool_handler."
        )
    assert len(executable) >= 30, (
        f"Suspiciously few executable lines found in _handler: got {len(executable)}. "
        "The handler may have moved or the coverage guard may need to follow the "
        "new dispatch entry point."
    )

    executed = _collect_executed_lines()
    covered = executable & executed
    uncovered = executable - executed

    coverage_pct = len(covered) / len(executable) * 100

    src_lines = _DISPATCH_SOURCE.read_text(encoding="utf-8").splitlines()
    uncovered_snippets = []
    for lineno in sorted(uncovered)[:20]:
        idx = lineno - 1
        snippet = src_lines[idx].strip() if 0 <= idx < len(src_lines) else "<unknown>"
        uncovered_snippets.append(f"  line {lineno}: {snippet}")

    diagnostic = (
        f"\nHandler line coverage: {coverage_pct:.1f}%"
        f" ({len(covered)}/{len(executable)} lines)\n"
        f"Uncovered lines ({len(uncovered)} total):\n"
        + ("\n".join(uncovered_snippets) if uncovered_snippets else "  (none)")
    )

    # 95% floor: the only unreachable lines are the invariant-guard ``raise
    # RuntimeError`` (PolicyCheck returns a denial without an envelope —
    # impossible by construction) and bytecode-layout cushion. All hook fan-out
    # branches, including the defensive ``except Exception`` around each hook
    # call, are exercised by the no-hook / NoopToolHook / RaisingToolHook
    # variants above.
    assert coverage_pct >= 95.0, (
        f"dispatch.py _handler coverage {coverage_pct:.1f}% < 95% target."
        f"{diagnostic}"
    )
