"""Runtime E2E gate for meta-skill creator proposals."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

RuntimeResult = dict[str, Any] | Awaitable[dict[str, Any]]
RuntimeRunner = Callable[..., RuntimeResult]
RuntimeJudge = Callable[..., RuntimeResult]


def _normalise_prompts(eval_prompts: object, skill_md: str) -> list[str]:
    if isinstance(eval_prompts, str):
        text = eval_prompts.strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                prompts = [line.strip() for line in text.splitlines() if line.strip()]
            else:
                prompts = parsed if isinstance(parsed, list) else [text]
        else:
            prompts = []
    elif isinstance(eval_prompts, list):
        prompts = eval_prompts
    else:
        prompts = []

    out = [str(p).strip() for p in prompts if str(p).strip()]
    if out:
        return out

    match = re.search(r"triggers:\s*\n(?:\s*-\s*\"?([^\"\n]+)\"?\s*\n?)", skill_md)
    trigger = match.group(1).strip() if match else "this meta skill"
    return [f"please use {trigger}"]


async def _call_runner(
    runner: RuntimeRunner,
    *,
    route: str,
    prompt: str,
    skill_md: str,
    baseline_model: str,
) -> dict[str, Any]:
    result = runner(
        route=route,
        prompt=prompt,
        skill_md=skill_md,
        baseline_model=baseline_model,
    )
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return dict(result)
    return {"text": str(result)}


async def _call_judge(
    judge: RuntimeJudge,
    *,
    prompt: str,
    meta: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    result = judge(prompt=prompt, meta=meta, baseline=baseline)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return dict(result)
    return {"winner": str(result).strip().lower()}


def _normalise_winner(value: object) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "orchestrated": "meta",
        "meta-skill": "meta",
        "metaskill": "meta",
        "no-meta": "baseline",
        "single-model": "baseline",
    }
    return aliases.get(raw, raw)


def _is_git_repo(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _prepare_runtime_workspace(root: Path, workspace_dir: str | None) -> Path:
    if workspace_dir:
        candidate = Path(workspace_dir).expanduser().resolve()
        if candidate.is_dir() and _is_git_repo(candidate):
            return candidate

    runtime_workspace = root / "runtime-workspace"
    runtime_workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=runtime_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "runtime-e2e@example.test"],
        cwd=runtime_workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime E2E"],
        cwd=runtime_workspace,
        check=True,
    )
    sample = runtime_workspace / "README.md"
    sample.write_text("# Runtime E2E fixture\n\nbaseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=runtime_workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=runtime_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    sample.write_text(
        "# Runtime E2E fixture\n\nbaseline\n\ncandidate change\n",
        encoding="utf-8",
    )
    return runtime_workspace


def _baseline_invalid_reason(baseline: dict[str, Any]) -> str:
    text = str(baseline.get("text") or "").strip().lower()
    error = str(baseline.get("error") or "").strip()
    if error:
        return "baseline_error"
    refusal_markers = (
        "runtime e2e baseline mode",
        "meta-skill creator tools are disabled",
        "meta_skill creator tools are disabled",
        "meta_skill_* creator tools are disabled",
        "i cannot complete this request",
        "i can’t complete this request",
    )
    if any(marker in text for marker in refusal_markers):
        return "baseline_invalid_or_blocked"
    return ""


async def run_runtime_e2e_gate(
    *,
    skill_md: str,
    eval_prompts: object = None,
    baseline_model: str = "",
    runner: RuntimeRunner,
    judge: RuntimeJudge,
) -> dict[str, Any]:
    """Run candidate meta-skill output against a no-meta highest-tier baseline."""

    prompts = _normalise_prompts(eval_prompts, skill_md)
    cases: list[dict[str, Any]] = []
    winners: list[str] = []
    for prompt in prompts:
        meta = await _call_runner(
            runner,
            route="meta",
            prompt=prompt,
            skill_md=skill_md,
            baseline_model=baseline_model,
        )
        baseline = await _call_runner(
            runner,
            route="baseline",
            prompt=prompt,
            skill_md=skill_md,
            baseline_model=baseline_model,
        )
        invalid_baseline = _baseline_invalid_reason(baseline)
        if invalid_baseline:
            winners.append("invalid")
            cases.append({
                "prompt": prompt,
                "winner": "invalid",
                "regression": invalid_baseline,
                "reason": (
                    "Baseline comparison was invalid because the no-meta "
                    "route returned an error/refusal instead of its strongest "
                    "standalone answer."
                ),
                "meta": meta,
                "baseline": baseline,
            })
            continue
        verdict = await _call_judge(judge, prompt=prompt, meta=meta, baseline=baseline)
        winner = _normalise_winner(verdict.get("winner"))
        winners.append(winner)
        regression = str(
            verdict.get("regression")
            or verdict.get("required_improvements")
            or verdict.get("required_improvement")
            or ""
        ).strip()
        cases.append({
            "prompt": prompt,
            "winner": winner,
            "regression": regression,
            "reason": str(verdict.get("reason") or verdict.get("reasons") or ""),
            "meta": meta,
            "baseline": baseline,
        })

    blocked = [
        case for case in cases
        if case["winner"] not in {"meta", "tie"} or bool(case["regression"])
    ]
    aggregate_winner = "invalid" if any(w == "invalid" for w in winners) else (
        "baseline" if any(w == "baseline" for w in winners) else (
            "meta" if any(w == "meta" for w in winners) else "tie"
        )
    )
    return {
        "status": "ok",
        "passed": not blocked,
        "winner": aggregate_winner,
        "baseline_model": baseline_model,
        "cases": cases,
    }


def make_runtime_e2e_context(
    *,
    provider: Any,
    base_config: Any,
    skill_loader: Any,
    tool_definitions: list[Any] | None,
    tool_handler: Any,
    agent_factory: Any,
    llm_chat: Any,
    tool_invoker: Any,
    workspace_dir: str | None = None,
    usage_tracker: Any = None,
    session_key: str = "",
    tool_registry: Any = None,
    tool_context: Any = None,
    system_prompt: str = "",
    baseline_model: str = "",
) -> dict[str, Any]:
    """Build the runner/judge context used by the creator runtime E2E gate."""

    from opensquilla.engine.types import DoneEvent, TextDeltaEvent, done_text_snapshot
    from opensquilla.execution_status import runtime_execution_status
    from opensquilla.skills.meta.inputs import make_meta_inputs
    from opensquilla.skills.meta.orchestrator import (
        MetaOrchestrator,
        make_agent_runner_from_parent,
    )
    from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan
    from opensquilla.skills.meta.types import MetaMatch
    from opensquilla.tool_boundary import ToolCall, ToolResult

    resolved_baseline_model = baseline_model or getattr(base_config, "model_id", "") or ""

    def _without_meta_tools(
        definitions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if definitions is None:
            return None
        filtered: list[dict[str, Any]] = []
        for definition in definitions:
            function = definition.get("function") if isinstance(definition, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if name == "meta_invoke" or str(name or "").startswith("meta_skill_"):
                continue
            filtered.append(definition)
        return filtered

    async def _runtime_e2e_runner(
        *,
        route: str,
        prompt: str,
        skill_md: str,
        baseline_model: str,
    ) -> dict[str, Any]:
        selected_baseline_model = baseline_model or resolved_baseline_model
        if route == "baseline":
            metadata_no_meta = dict(getattr(base_config, "metadata", {}) or {})
            metadata_no_meta.pop("skill_loader", None)
            metadata_no_meta.pop("meta_match", None)
            baseline_config = replace(
                base_config,
                model_id=selected_baseline_model or getattr(base_config, "model_id", None),
                metadata=metadata_no_meta,
                request_context_prompt=(
                    "Runtime E2E baseline mode: answer the same user request as the "
                    "highest-tier single model with ordinary non-meta tools only. "
                    "Produce a complete standalone proposal, decision brief, or "
                    "final artifact from the user prompt. Do not mention runtime "
                    "tool restrictions, do not refuse because orchestration is "
                    "unavailable, and do not ask the user to enable meta-skill "
                    "creator tools."
                ),
            )

            async def baseline_tool_handler(tc: ToolCall) -> ToolResult:
                if tc.tool_name.startswith("meta_skill_"):
                    return ToolResult(
                        tool_use_id=tc.tool_use_id,
                        tool_name=tc.tool_name,
                        content=(
                            "Continue without this tool and write the strongest "
                            "standalone answer directly in the final response."
                        ),
                        is_error=False,
                        execution_status=runtime_execution_status(
                            "success",
                            reason="not_available_in_baseline",
                        ),
                    )
                if tool_handler is None:
                    return ToolResult(
                        tool_use_id=tc.tool_use_id,
                        tool_name=tc.tool_name,
                        content=f"No tool handler registered for tool '{tc.tool_name}'",
                        is_error=True,
                        execution_status=runtime_execution_status(
                            "error",
                            reason="runtime_error",
                        ),
                    )
                return cast(ToolResult, await tool_handler(tc))

            baseline_agent = agent_factory(
                provider=provider,
                config=baseline_config,
                tool_definitions=_without_meta_tools(tool_definitions),
                tool_handler=baseline_tool_handler,
                usage_tracker=usage_tracker,
                session_key=f"{session_key}:runtime_e2e:baseline",
                tool_registry=tool_registry,
                tool_context=tool_context,
            )
            parts: list[str] = []
            done_text_present = False
            done_text = ""
            async for event in baseline_agent.run_turn(prompt):
                if isinstance(event, TextDeltaEvent):
                    parts.append(event.text)
                elif isinstance(event, DoneEvent):
                    done_text_present, done_text = done_text_snapshot(event)
            return {
                "route": "baseline",
                "text": (done_text if done_text_present else "".join(parts)).strip(),
                "model": selected_baseline_model,
            }

        match_name = re.search(r"^name:\s*\"?([\w\-]+)\"?\s*$", skill_md, re.MULTILINE)
        skill_name = match_name.group(1) if match_name else "candidate"
        with tempfile.TemporaryDirectory(prefix="opensquilla-meta-e2e-") as tmp:
            tmp_path = Path(tmp)
            candidate_root = Path(tmp) / "candidate-skills"
            skill_dir = candidate_root / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
            runtime_workspace = _prepare_runtime_workspace(tmp_path, workspace_dir)

            from opensquilla.skills.loader import SkillLoader

            candidate_loader = SkillLoader(
                bundled_dir=getattr(skill_loader, "_bundled_dir", None),
                workspace_dir=getattr(skill_loader, "_workspace_dir", None),
                managed_dir=getattr(skill_loader, "_managed_dir", None),
                personal_agents_dir=getattr(skill_loader, "_personal_agents_dir", None),
                project_agents_dir=getattr(skill_loader, "_project_agents_dir", None),
                extra_dirs=[candidate_root, *getattr(skill_loader, "_extra_dirs", [])],
                snapshot_path=Path(tmp) / "snapshot.json",
            )
            candidate_loader.invalidate_cache()
            candidate_spec = candidate_loader.get_by_name(skill_name)
            if candidate_spec is None:
                return {
                    "route": "meta",
                    "text": "",
                    "ok": False,
                    "error": f"candidate meta-skill {skill_name!r} did not load",
                }
            try:
                candidate_plan = parse_meta_plan(candidate_spec)
            except MetaPlanError as exc:
                return {"route": "meta", "text": "", "ok": False, "error": str(exc)}
            if candidate_plan is None:
                return {
                    "route": "meta",
                    "text": "",
                    "ok": False,
                    "error": f"candidate {skill_name!r} is not a meta-skill",
                }

            runtime_runner = make_agent_runner_from_parent(
                provider=provider,
                base_config=base_config,
                tool_definitions=tool_definitions or [],
                tool_handler=tool_handler,
                agent_factory=agent_factory,
                workspace_dir=str(runtime_workspace),
                usage_tracker=usage_tracker,
                session_key=f"{session_key}:runtime_e2e:meta",
            )
            runtime_orch = MetaOrchestrator(
                agent_runner=runtime_runner,
                skill_loader=candidate_loader,
                llm_chat=llm_chat,
                tool_invoker=tool_invoker,
                workspace_dir=str(runtime_workspace),
                triggered_by="runtime_e2e_gate",
                session_key=f"{session_key}:runtime_e2e:meta",
                usage_tracker=usage_tracker,
            )
            runtime_inputs = make_meta_inputs(
                user_message=prompt,
                system_prompt=(
                    system_prompt
                    or getattr(base_config, "system_prompt", "")
                    or ""
                ),
            )
            runtime_inputs["workspace_dir"] = str(runtime_workspace)
            runtime_match = MetaMatch(
                plan=candidate_plan,
                inputs=runtime_inputs,
            )
            runtime_result = await runtime_orch.run(runtime_match)
            return {
                "route": "meta",
                "text": runtime_result.final_text,
                "ok": runtime_result.ok,
                "error": runtime_result.error or "",
            }

    async def _runtime_e2e_judge(
        *,
        prompt: str,
        meta: dict[str, Any],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        if llm_chat is None:
            return {
                "winner": "baseline",
                "regression": "runtime judge unavailable",
                "reason": "llm_chat dependency missing",
            }
        judge_prompt = (
            "Compare two final answers for the same user prompt. "
            "A is OpenSquilla using the candidate meta-skill. "
            "B is OpenSquilla without meta-skills using the highest-tier model. "
            "If either answer is an error/refusal instead of a substantive "
            "answer, set winner to baseline unless A is the error/refusal, and "
            "include a regression explaining that the comparison is invalid. "
            "Judge final product quality with highest weight: completeness, "
            "specificity, correctness, actionability, and reusable output "
            "quality matter more than process narration. "
            "Return strict JSON only with keys winner (meta|baseline|tie), "
            "regression (empty string if none), and reason.\n\n"
            f"User prompt:\n{prompt}\n\n"
            f"A meta answer:\n{meta.get('text', '')}\n\n"
            f"B baseline answer:\n{baseline.get('text', '')}\n"
        )
        raw = await llm_chat(
            "You are a strict evaluator for runtime E2E meta-skill gates.",
            judge_prompt,
        )
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            lowered = text.lower()
            winner = "meta" if "meta" in lowered else (
                "tie" if "tie" in lowered else "baseline"
            )
            return {"winner": winner, "regression": "", "reason": text[:500]}
        return parsed if isinstance(parsed, dict) else {"winner": "baseline"}

    return {
        "runner": _runtime_e2e_runner,
        "judge": _runtime_e2e_judge,
        "baseline_model": resolved_baseline_model,
    }
