#!/usr/bin/env python3
"""Prepare, run, and evaluate the MetaSkill-only AgentSkillOS benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Download pinned public tasks, evaluators, and complete Skills")
    prepare.add_argument("--cache", type=Path, default=ROOT / "workspace/benchmark/cache")
    prepare.add_argument("--upstream", type=Path, help="Use an existing pinned upstream checkout instead of downloading")
    for name in ("check", "run"):
        command = commands.add_parser(name)
        command.add_argument("--prepared", type=Path, default=ROOT / "workspace/benchmark/cache/prepared.json")
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--task-id", action="append", help="Select a task (repeat flag); default: all 20")
        command.add_argument("--allow-missing-tools", action="store_true", help="Explicitly allow unresolved optional Skill dependencies")
        if name == "run":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--repeats", type=int, default=3)
            command.add_argument("--timeout", type=int, default=5400, help="Whole-DAG deadline per attempt, in seconds")
            command.add_argument("--resume", action="store_true", help="Skip existing trials; never silently rerun them")
            command.add_argument("--retry-failed", action="store_true", help="With --resume, create new attempts for failed trials")
    evaluate = commands.add_parser("evaluate", help="Re-evaluate retained workspaces without model calls")
    evaluate.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    try:
        if args.command == "prepare":
            from metaskill_benchmark.prepare import prepare
            path = prepare(args.cache, upstream=args.upstream)
            print(f"Prepared: {path}")
            return 0
        from metaskill_benchmark.runner import check, run_batch, evaluate_batch
        if args.command == "check":
            report = check(args.prepared, args.config, args.task_id)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if not report["errors"] and (args.allow_missing_tools or not report["missing_tools"]) else 1
        if args.command == "evaluate":
            summary = evaluate_batch(args.output)
        else:
            summary = run_batch(args.prepared, args.config, args.output, tasks=args.task_id,
                                repeats=args.repeats, timeout=args.timeout, resume=args.resume,
                                retry_failed=args.retry_failed, allow_missing_tools=args.allow_missing_tools)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["coverage"]["completed_trials"] == summary["coverage"]["expected_trials"] else 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
