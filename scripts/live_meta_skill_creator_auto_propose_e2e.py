#!/usr/bin/env python3
"""Live auto-propose E2E for meta-skill-creator.

This harness intentionally does not accept a user prompt. It verifies the
unattended creator path used by cron and dream hooks: aggregate decision-log
history, synthesize a candidate through meta-skill-creator, run its gates, and
persist a proposal with auto_* provenance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load_env_file(path: Path | None) -> None:
    if path is None or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _seed_history(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    rows: list[dict[str, object]] = []
    chains = [
        (["history-explorer", "docx"], 7),
        (["deep-research", "docx"], 4),
        (["github", "docx"], 2),
    ]
    for chain, count in chains:
        for _ in range(count):
            rows.append({
                "ts": now,
                "agent_id": "main",
                "user_message": "recent decision history operational recap",
                "skills_invoked": chain,
            })
    path = log_dir / f"decisions-{datetime.now(UTC).strftime('%Y%m%d')}.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    home = args.home.expanduser().resolve()
    log_dir = args.log_dir.expanduser().resolve() if args.log_dir else home / "logs"
    proposals_dir = (
        args.proposals_dir.expanduser().resolve()
        if args.proposals_dir
        else home / "proposals"
    )
    workspace_dir = args.workspace.expanduser().resolve() if args.workspace else home / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if args.seed_history:
        history_path = _seed_history(log_dir)
    else:
        history_path = None

    os.environ["OPENSQUILLA_STATE_DIR"] = str(home)
    os.environ["OPENSQUILLA_LOG_DIR"] = str(log_dir)
    os.environ["OPENSQUILLA_LLM_PROVIDER"] = args.provider
    os.environ["OPENSQUILLA_LLM_MODEL"] = args.model

    # Imports happen after env setup so default_opensquilla_home() users resolve
    # to this isolated state root.
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.gateway.boot import _make_auto_propose_tool_context, build_services
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.scheduler.auto_propose_handler import make_auto_propose_handler
    from opensquilla.scheduler.types import CronJob, SessionTarget
    from opensquilla.skills.creator.auto_propose import auto_propose
    from opensquilla.skills.creator.proposer import (
        reset_runtime_e2e_context,
        reset_smoke_fixture_context,
        set_runtime_e2e_context,
        set_smoke_fixture_context,
    )
    from opensquilla.skills.creator.runtime_e2e import make_runtime_e2e_context
    from opensquilla.skills.meta.orchestrator import (
        MetaOrchestrator,
        make_agent_runner_from_parent,
        make_llm_chat_from_provider,
        make_tool_invoker_from_handler,
    )
    from opensquilla.tools.dispatch import build_tool_handler

    text_tiers = {
        "c0": {"provider": args.provider, "model": args.model, "thinking_level": "off"},
        "c1": {"provider": args.provider, "model": args.model, "thinking_level": "low"},
        "c2": {"provider": args.provider, "model": args.model, "thinking_level": "medium"},
        "c3": {"provider": args.provider, "model": args.model, "thinking_level": "high"},
    }
    actual_cron = args.actual_scheduler and args.trigger == "cron"
    actual_dream = args.actual_scheduler and args.trigger == "dream"
    auto_enabled = actual_cron or not args.actual_scheduler
    config = GatewayConfig(
        workspace_dir=str(workspace_dir),
        llm={
            "provider": args.provider,
            "model": args.model,
            "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "base_url": args.base_url,
        },
        squilla_router={
            "enabled": True,
            "tiers": text_tiers,
            "default_tier": "c3",
        },
        meta_skill={
            "auto_propose": {
                "enabled": auto_enabled,
                "cron": args.cron,
                "on_dream_complete": actual_dream or not args.actual_scheduler,
                "window_days": args.window_days,
                "min_freq": args.min_freq,
                "top_k": args.top_k,
                "auto_enable": args.auto_enable,
                "auto_enable_max_risk": args.auto_enable_max_risk,
            },
        },
        memory={
            "dream": {
                "enabled": actual_dream,
                "auto_schedule": actual_dream,
                "cron": args.cron if actual_dream else None,
                "preview_mode": True,
                "min_batch_size": 1,
            },
        },
    )
    server_handle = None
    if args.actual_scheduler:
        from opensquilla.gateway.boot import start_gateway_server

        server_handle = await start_gateway_server(config=config, run=False)
        svc = getattr(server_handle, "_services", None)
        if svc is None:
            raise RuntimeError("gateway boot did not expose services")
    else:
        svc = await build_services(
            config=config,
            session_db_path=str(home / "state" / "sessions.sqlite"),
            seed_agent_workspaces=True,
        )
    assert svc.provider_selector is not None
    assert svc.tool_registry is not None
    assert svc.skill_loader is not None

    def build_orchestrator(agent_id: str) -> MetaOrchestrator:
        provider_selector = svc.provider_selector
        clone_selector = getattr(provider_selector, "clone", None)
        if callable(clone_selector):
            provider_selector = clone_selector()
            override_model = getattr(provider_selector, "override_model", None)
            if callable(override_model):
                override_model(args.model)
        provider = provider_selector.resolve()
        ctx = _make_auto_propose_tool_context(
            agent_id=agent_id,
            workspace_dir=str(workspace_dir),
        )
        tool_handler = build_tool_handler(svc.tool_registry, ctx)
        base_config = AgentConfig(
            model_id=args.model,
            workspace_dir=str(workspace_dir),
            metadata={
                "routing_source": "meta_skill_auto_propose",
                "routing_applied": True,
                "routed_tier": "c3",
                "routed_model": args.model,
                "applied_model": args.model,
                "thinking_requested": True,
                "thinking_level": "high",
            },
        )
        tool_definitions = svc.tool_registry.to_tool_definitions(ctx)
        llm_chat = make_llm_chat_from_provider(
            provider=provider,
            base_config=base_config,
            usage_tracker=svc.usage_tracker,
            session_key=f"auto_propose:{agent_id}",
        )
        base_tool_invoker = make_tool_invoker_from_handler(tool_handler=tool_handler)
        runtime_e2e_ctx = make_runtime_e2e_context(
            provider=provider,
            base_config=base_config,
            skill_loader=svc.skill_loader,
            tool_definitions=tool_definitions,
            tool_handler=tool_handler,
            agent_factory=Agent,
            llm_chat=llm_chat,
            tool_invoker=base_tool_invoker,
            workspace_dir=str(workspace_dir),
            usage_tracker=svc.usage_tracker,
            session_key=f"auto_propose:{agent_id}",
            tool_registry=svc.tool_registry,
            tool_context=ctx,
            system_prompt=base_config.system_prompt or "",
            baseline_model=args.model,
        )

        async def tool_invoker(tool_name: str, tool_args: dict[str, Any]) -> Any:
            if tool_name == "meta_skill_persist_proposal":
                tool_args = dict(tool_args)
                tool_args.setdefault("home", str(home))
                tool_args.setdefault("auto_enable_manual", False)
            token = set_runtime_e2e_context(runtime_e2e_ctx)
            smoke_token = set_smoke_fixture_context({"llm_chat": llm_chat})
            try:
                return await base_tool_invoker(tool_name, tool_args)
            finally:
                reset_smoke_fixture_context(smoke_token)
                reset_runtime_e2e_context(token)

        return MetaOrchestrator(
            agent_runner=make_agent_runner_from_parent(
                provider=provider,
                base_config=base_config,
                tool_definitions=tool_definitions,
                tool_handler=tool_handler,
                agent_factory=Agent,
                workspace_dir=str(workspace_dir),
                usage_tracker=svc.usage_tracker,
                session_key=f"auto_propose:{agent_id}",
            ),
            skill_loader=svc.skill_loader,
            llm_chat=llm_chat,
            tool_invoker=tool_invoker,
            workspace_dir=str(workspace_dir),
            run_writer=getattr(svc, "meta_run_writer", None),
            triggered_by=f"auto_{args.trigger}",
            session_key=f"auto_propose:{agent_id}",
            turn_id=None,
            usage_tracker=svc.usage_tracker,
        )

    async def scheduler_snapshot() -> dict[str, Any]:
        scheduler = getattr(svc, "cron_scheduler", None)
        if scheduler is None:
            return {"jobs": []}
        jobs = await scheduler.list_jobs()
        rows = []
        for job in jobs:
            if not str(getattr(job, "name", "")).startswith(("auto_propose:", "memory_dream:")):
                continue
            runs = await scheduler.get_runs(getattr(job, "id"), limit=5)
            rows.append({
                "id": getattr(job, "id", ""),
                "name": getattr(job, "name", ""),
                "handler_key": getattr(job, "handler_key", ""),
                "schedule_kind": str(getattr(job, "schedule_kind", "")),
                "schedule_raw": getattr(job, "schedule_raw", ""),
                "status": str(getattr(job, "status", "")),
                "next_run_at": (
                    getattr(job, "next_run_at", None).isoformat()
                    if getattr(job, "next_run_at", None) is not None else None
                ),
                "run_count": getattr(job, "run_count", 0),
                "runs": [
                    {
                        "success": getattr(run, "success", False),
                        "summary": getattr(run, "summary", ""),
                        "delivery_status": getattr(run, "delivery_status", ""),
                        "started_at": (
                            getattr(run, "started_at", None).isoformat()
                            if getattr(run, "started_at", None) is not None else None
                        ),
                        "finished_at": (
                            getattr(run, "finished_at", None).isoformat()
                            if getattr(run, "finished_at", None) is not None else None
                        ),
                    }
                    for run in runs
                ],
            })
        return {"jobs": rows}

    async def wait_for_automatic_execution() -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + args.wait_seconds
        last: dict[str, Any] = {}
        while True:
            proposals_now = [
                sub.name
                for sub in sorted(proposals_dir.iterdir())
                if sub.is_dir()
            ] if proposals_dir.is_dir() else []
            snapshot = await scheduler_snapshot()
            last = {
                "triggered_by": args.trigger,
                "actual_scheduler": True,
                "proposal_ids": proposals_now,
                "scheduler": snapshot,
            }
            target_prefix = "memory_dream:" if args.trigger == "dream" else "auto_propose:"
            target_jobs = [
                job for job in snapshot.get("jobs", [])
                if str(job.get("name", "")).startswith(target_prefix)
            ]
            target_finished = any(
                int(job.get("run_count") or 0) > 0
                or bool(job.get("runs"))
                for job in target_jobs
            )
            if target_jobs and target_finished and (
                not args.wait_for_proposal or proposals_now
            ):
                return last
            if asyncio.get_running_loop().time() >= deadline:
                return last
            await asyncio.sleep(args.poll_seconds)

    if args.actual_scheduler:
        result = await wait_for_automatic_execution()
    elif args.via_handler:
        handler = make_auto_propose_handler(
            build_orchestrator=build_orchestrator,
            skill_loader=svc.skill_loader,
            log_dir=log_dir,
            proposals_dir=proposals_dir,
            config=config.meta_skill.auto_propose,
            enabled_predicate=lambda: True,
        )
        job = CronJob(
            id=f"live-auto-propose-{args.trigger}",
            name="auto_propose:main",
            cron_expr="* * * * *",
            schedule_raw="* * * * *",
            handler_key="auto_propose",
            payload={"agent_id": "main"},
            session_target=SessionTarget.ISOLATED,
        )
        handler_result = await handler(job)
        result = {
            "handler": {
                "summary": handler_result.summary,
                "delivery_status": handler_result.delivery_status,
            },
            "triggered_by": args.trigger,
        }
    else:
        result_obj = await auto_propose(
            orchestrator=build_orchestrator("main"),
            skill_loader=svc.skill_loader,
            log_dir=log_dir,
            window_days=args.window_days,
            min_freq=args.min_freq,
            top_k=args.top_k,
            triggered_by=args.trigger,
            proposals_dir=proposals_dir,
            auto_enable=args.auto_enable,
            auto_enable_max_risk=args.auto_enable_max_risk,
        )
        result = {
            "summary": result_obj.summary(),
            "proposals_created": result_obj.proposals_created,
            "proposals_enabled": result_obj.proposals_enabled,
            "auto_enable": result_obj.auto_enable,
            "skipped": result_obj.skipped,
            "errors": result_obj.errors,
            "triggered_by": result_obj.triggered_by,
        }

    proposals = []
    if proposals_dir.is_dir():
        for sub in sorted(proposals_dir.iterdir()):
            gates_path = sub / "gates.json"
            gates = (
                json.loads(gates_path.read_text(encoding="utf-8"))
                if gates_path.is_file()
                else {}
            )
            proposals.append({
                "id": sub.name,
                "skill": (sub / "SKILL.md").read_text(encoding="utf-8")[:400]
                if (sub / "SKILL.md").is_file() else "",
                "gates": gates,
            })

    try:
        return {
            "ok": True,
            "provider": args.provider,
            "model": args.model,
            "home": str(home),
            "log_dir": str(log_dir),
            "history_path": str(history_path) if history_path else "",
            "proposals_dir": str(proposals_dir),
            "result": result,
            "proposal_count": len(proposals),
            "proposals": proposals,
        }
    finally:
        if server_handle is not None:
            await server_handle.close(reason="meta_skill_creator_auto_propose_e2e")
        else:
            close = getattr(svc, "close", None)
            if callable(close):
                await close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--proposals-dir", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"))
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--trigger", choices=["cron", "dream"], default="cron")
    parser.add_argument("--cron", default="* * * * *")
    parser.add_argument("--via-handler", action="store_true")
    parser.add_argument("--actual-scheduler", action="store_true")
    parser.add_argument(
        "--wait-for-proposal",
        action="store_true",
        help=(
            "For --actual-scheduler, wait until at least one proposal directory "
            "exists instead of returning as soon as a scheduled run starts."
        ),
    )
    parser.add_argument("--wait-seconds", type=float, default=120.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--seed-history", action="store_true")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--min-freq", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--auto-enable", action="store_true")
    parser.add_argument("--auto-enable-max-risk", default="medium")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _load_env_file(args.env_file)
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
