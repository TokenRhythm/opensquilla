"""Human-readable terminal report for a code-task result.

The same TaskResult is emitted three ways (terminal text here, --json for
programs, result.json on disk). This module owns the text form.
"""

from __future__ import annotations

from opensquilla.contrib.codetask.types import TaskResult, TaskState

_STATE_GLYPH = {
    TaskState.VERIFIED: "✔",
    TaskState.ALREADY_SATISFIED: "•",
    TaskState.NOT_TESTABLE: "•",
    TaskState.ENVIRONMENT_BLOCKED: "✘",
    TaskState.INVALID_ACCEPTANCE_TEST: "✘",
    TaskState.FAILED: "✘",
}

_STATE_BLURB = {
    TaskState.VERIFIED: "acceptance test passed (verified)",
    TaskState.ALREADY_SATISFIED: "expected behavior already held on the base commit",
    TaskState.NOT_TESTABLE: "work done but not expressible as an automated test",
    TaskState.ENVIRONMENT_BLOCKED: "could not build/test the repo environment",
    TaskState.INVALID_ACCEPTANCE_TEST: "no valid acceptance manifest was produced",
    TaskState.FAILED: "acceptance test did not pass, or a regression was introduced",
}


def render(result: TaskResult) -> str:
    glyph = _STATE_GLYPH.get(result.state, "•")
    lines: list[str] = []
    lines.append(f"{glyph} {result.branch} · {result.state.value}")
    lines.append(f"  {_STATE_BLURB.get(result.state, '')}")
    lines.append("")
    lines.append(f"  task     {result.task_slug} ({result.source})")
    lines.append(f"  repo     {result.repo} @ {result.base_ref}")
    lines.append(f"  change   {result.files_changed} file(s), {result.commits} commit(s)")
    if result.diffstat:
        for dl in result.diffstat.splitlines():
            lines.append(f"           {dl}")

    if result.assumptions:
        lines.append("")
        lines.append("  assumptions (check these — a wrong one means a wrong fix):")
        for a in result.assumptions:
            lines.append(f"    - {a}")

    if result.acceptance:
        lines.append("")
        lines.append("  acceptance tests")
        for c in result.acceptance:
            arrow = ""
            if c.before is not None:
                arrow = f"{c.before} → {c.after}"
            else:
                arrow = f"{c.after}"
            mark = "✔" if c.after == "pass" else "✘"
            lines.append(f"    {mark} {c.name}   {arrow}")

    if result.regression and result.regression.ran:
        r = result.regression
        nf = r.new_failures if r.new_failures is not None else "?"
        lines.append("")
        lines.append(
            f"  regression  {r.command} → "
            f"{r.passed if r.passed is not None else '?'} passed / "
            f"{r.failed if r.failed is not None else '?'} failed / "
            f"{nf} new"
        )

    if result.usage:
        cost = result.usage.get("cost_usd")
        reqs = result.usage.get("request_count")
        dur = result.duration_seconds
        bits = []
        if cost is not None:
            bits.append(f"${cost:.4f}")
        if reqs is not None:
            bits.append(f"{reqs} requests")
        if dur is not None:
            bits.append(f"{dur:.0f}s")
        if bits:
            lines.append("")
            lines.append("  cost     " + " · ".join(bits))

    _note = getattr(result, "final_failure_reason", None) or result.error
    if _note:
        lines.append("")
        lines.append(f"  note     {_note}")

    if result.patch_path:
        lines.append("")
        lines.append(f"  diff     {result.patch_path}")
    if result.artifact_dir:
        lines.append(f"  artifacts {result.artifact_dir}")
    _build = getattr(result, "build", None)
    if _build is not None and getattr(_build, "installer_paths", None):
        for _p in _build.installer_paths:
            lines.append(f"  installer {_p}")

    return "\n".join(lines)
