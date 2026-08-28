"""`opensquilla skills meta ...` subcommand tree.

Currently exposes ``runs {list, show, steps, failures, replay, diff,
cost, validate, eval-baseline}``. The
``meta_app`` container is forward-compatible with P0 #2 (which will add
``list``/``show``/``validate`` siblings); this PR ships only ``runs``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from opensquilla.paths import state_dir
from opensquilla.persistence.meta_run_query import parse_since_ms
from opensquilla.persistence.meta_run_writer import (
    MetaRunWriter,
    RunRecord,
    StepRecord,
    open_meta_run_writer,
    summarize_run_record,
)
from opensquilla.skills.meta.author_seed import draft_meta_skill_seed
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaPlan

meta_app = typer.Typer(
    help="Meta-skill operations: runs, replay, proposals.",
)
runs_app = typer.Typer(help="Meta-skill execution history.")
meta_app.add_typer(runs_app, name="runs")


def _resolve_db_path() -> str:
    """Resolve the meta-skill runs SQLite path used by the gateway writer.

    Resolution order (matches the gateway's ``_state_path(config, ...)``
    helper so the CLI sees the same rows the running gateway writes):

      1. ``OPENSQUILLA_META_RUNS_DB`` env var (explicit override)
      2. ``GatewayConfig.state_dir`` (loaded from
         ``OPENSQUILLA_GATEWAY_CONFIG_PATH`` env var,
         ``./opensquilla.toml``, or ``~/.opensquilla/config.toml`` —
         identical precedence to the gateway's own loader)
      3. ``~/.opensquilla/state/sessions.db`` (built-in default)

    The earlier (1)+(3) shortcut missed any deployment that customised
    ``state_dir`` in toml — operators ran ``opensquilla skills meta runs
    list`` and saw "(no runs)" while the gateway was happily writing
    to a different directory.
    """
    env = os.environ.get("OPENSQUILLA_META_RUNS_DB")
    if env:
        return env

    # Load GatewayConfig to honour state_dir from the same source the
    # gateway uses. Local import to keep the CLI startup path lean.
    try:
        from opensquilla.gateway.config import GatewayConfig

        config_path_env = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
        cfg = GatewayConfig.load(config_path_env or None)
        configured = (cfg.state_dir or "").strip()
        if configured:
            return os.path.join(configured, "sessions.db")
    except Exception:  # noqa: BLE001 — fall back to default on any load failure
        pass

    return str(state_dir("sessions.db"))


def _open_writer() -> MetaRunWriter:
    return open_meta_run_writer(_resolve_db_path())


_NO_HISTORY_MESSAGE = (
    "no meta-run history yet — the database is created on first gateway run"
)


def _db_file_exists() -> bool:
    return os.path.exists(_resolve_db_path())


def _exit_no_history_list(*, json_out: bool, empty_json: Any) -> None:
    """Friendly exit for list-style commands when the DB was never created.

    The CLI is a read surface: opening the path with sqlite's default
    read-write-CREATE mode would either traceback (state dir missing) or
    silently create a stray empty ``sessions.db``. Exit 0 with the
    command's empty-output shape instead, and never create the file.
    """
    if json_out:
        typer.echo(json.dumps(empty_json, default=str))
    else:
        typer.echo(f"({_NO_HISTORY_MESSAGE})")
    raise typer.Exit(0)


def _exit_no_history_lookup(run_id: str) -> None:
    """No-DB exit for run-lookup commands — keeps the not-found convention."""
    typer.echo(f"run not found: {run_id} ({_NO_HISTORY_MESSAGE})", err=True)
    raise typer.Exit(2)


def _parse_since(value: str | None) -> int | None:
    try:
        return parse_since_ms(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _serialize_record(rec: RunRecord) -> dict[str, Any]:
    d = asdict(rec)
    d["steps"] = [asdict(s) for s in rec.steps]
    d["summary"] = summarize_run_record(rec)
    return d


def _serialize_step(step: StepRecord) -> dict[str, Any]:
    return asdict(step)


def _hydrate_records(writer: MetaRunWriter, rows: list[RunRecord]) -> list[RunRecord]:
    return writer.hydrate_runs(rows)


def _loaded_specs_for_conflicts() -> list[Any]:
    try:
        from opensquilla.skills.loader import SkillLoader

        return SkillLoader().load_all()
    except Exception:  # noqa: BLE001 - author draft should remain usable
        return []


def _print_runs_table(rows: list[RunRecord]) -> None:
    if not rows:
        typer.echo("(no runs)")
        return
    typer.echo(f"{'RUN_ID':28} {'META_SKILL':30} {'STATUS':10} {'TRIGGER':17} {'STARTED':20}")
    for r in rows:
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.started_at_ms / 1000))
        typer.echo(
            f"{r.run_id:28} {r.meta_skill_name:30.30} {r.status:10} "
            f"{r.triggered_by:17} {started}"
        )


@runs_app.command("list")
def runs_list(
    name: str | None = typer.Option(None, "--name", help="Filter by meta-skill name"),
    status: str | None = typer.Option(None, "--status", help="ok|failed|running|cancelled"),
    session: str | None = typer.Option(None, "--session", help="Filter by session_key"),
    since: str | None = typer.Option(None, "--since", help="e.g., 5m, 24h, 7d"),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """List meta-skill runs."""
    if not _db_file_exists():
        _exit_no_history_list(json_out=json_out, empty_json=[])
    writer = _open_writer()
    try:
        rows = writer.list_runs(
            name=name,
            status=status,
            session_key=session,
            since_ms=_parse_since(since),
            limit=limit,
        )
    finally:
        writer.close()

    if json_out:
        writer = _open_writer()
        try:
            rows = _hydrate_records(writer, rows)
        finally:
            writer.close()
        typer.echo(json.dumps([_serialize_record(r) for r in rows], default=str))
    else:
        _print_runs_table(rows)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show a single run with its steps."""
    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        rec = writer.get_run(run_id)
    finally:
        writer.close()
    if rec is None:
        typer.echo(f"run not found: {run_id}", err=True)
        raise typer.Exit(2)

    if json_out:
        typer.echo(json.dumps(_serialize_record(rec), default=str))
        return

    typer.echo(f"run_id:        {rec.run_id}")
    typer.echo(f"meta_skill:    {rec.meta_skill_name}")
    typer.echo(f"digest:        {rec.meta_skill_digest[:16]}...")
    typer.echo(f"status:        {rec.status}")
    typer.echo(f"triggered_by:  {rec.triggered_by}")
    typer.echo(f"session_key:   {rec.session_key}")
    typer.echo(
        "started:       "
        + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.started_at_ms / 1000))
    )
    if rec.ended_at_ms:
        typer.echo(
            "ended:         "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.ended_at_ms / 1000))
        )
    if rec.final_text:
        typer.echo(f"final_text:    {rec.final_text[:200]}...")
    if rec.error:
        typer.echo(f"error:         {rec.error}")
    typer.echo(f"steps:         {len(rec.steps)}")


@runs_app.command("steps")
def runs_steps(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show step-by-step trace of a run."""
    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        steps = writer.get_steps(run_id)
    finally:
        writer.close()
    if not steps:
        typer.echo(f"no steps for run: {run_id}", err=True)
        raise typer.Exit(2)

    if json_out:
        typer.echo(json.dumps([_serialize_step(s) for s in steps], default=str))
        return

    typer.echo(f"{'STEP':12} {'KIND':14} {'SKILL':24} {'STATUS':12} {'DURATION':12}")
    for s in steps:
        dur = (
            f"{s.ended_at_ms - s.started_at_ms}ms"
            if s.ended_at_ms is not None
            else "—"
        )
        typer.echo(
            f"{s.step_id:12} {s.step_kind:14} {s.effective_skill:24.24} "
            f"{s.status:12} {dur:12}"
        )


@runs_app.command("draft")
def runs_draft(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Draft a meta-skill authoring seed from a historical run."""
    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        rec = writer.get_run(run_id)
    finally:
        writer.close()
    if rec is None:
        typer.echo(f"run not found: {run_id}", err=True)
        raise typer.Exit(2)

    seed = draft_meta_skill_seed(rec, existing_specs=_loaded_specs_for_conflicts())
    if json_out:
        typer.echo(json.dumps(seed, default=str))
        return

    typer.echo(f"name:          {seed['name']}")
    typer.echo(f"description:   {seed['description']}")
    typer.echo("triggers:")
    for trigger in seed.get("trigger_candidates", []):
        typer.echo(f"  - {trigger}")
    conflicts = seed.get("trigger_conflicts", [])
    if conflicts:
        typer.echo("trigger_conflicts:")
        for item in conflicts:
            typer.echo(f"  - {item['trigger']} -> {item['skill']}")
    typer.echo(f"steps:         {len(seed.get('composition', {}).get('steps', []))}")


@runs_app.command("failures")
def runs_failures(
    name: str | None = typer.Option(None, "--name"),
    since: str | None = typer.Option(None, "--since"),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List failed runs."""
    if not _db_file_exists():
        _exit_no_history_list(json_out=json_out, empty_json=[])
    writer = _open_writer()
    try:
        rows = writer.list_failures(
            name=name, since_ms=_parse_since(since), limit=limit,
        )
    finally:
        writer.close()
    if json_out:
        writer = _open_writer()
        try:
            rows = _hydrate_records(writer, rows)
        finally:
            writer.close()
        typer.echo(json.dumps([_serialize_record(r) for r in rows], default=str))
    else:
        _print_runs_table(rows)


@runs_app.command("diff")
def runs_diff(
    left_run_id: str = typer.Argument(...),
    right_run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two meta-skill runs."""
    from opensquilla.skills.meta.run_reports import build_run_diff

    if not _db_file_exists():
        _exit_no_history_lookup(left_run_id)
    writer = _open_writer()
    try:
        left = writer.get_run(left_run_id)
        right = writer.get_run(right_run_id)
    finally:
        writer.close()
    if left is None:
        typer.echo(f"run not found: {left_run_id}", err=True)
        raise typer.Exit(2)
    if right is None:
        typer.echo(f"run not found: {right_run_id}", err=True)
        raise typer.Exit(2)
    diff = build_run_diff(left, right)
    if json_out:
        typer.echo(json.dumps(diff, default=str))
        return
    typer.echo(f"left:                 {left_run_id}")
    typer.echo(f"right:                {right_run_id}")
    typer.echo(f"status_changed:       {diff['status_changed']}")
    typer.echo(f"failed_step_changed:  {diff['failed_step_changed']}")
    typer.echo(f"final_text_delta:     {diff['final_text_chars_delta']}")
    typer.echo(f"step_count_delta:     {diff['step_count_delta']}")


@runs_app.command("cost")
def runs_cost(
    name: str | None = typer.Option(None, "--name"),
    status: str | None = typer.Option(None, "--status"),
    session: str | None = typer.Option(None, "--session"),
    since: str | None = typer.Option(None, "--since"),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Summarize meta-skill run usage and cost telemetry."""
    from opensquilla.skills.meta.run_reports import build_cost_summary

    if not _db_file_exists():
        _exit_no_history_list(json_out=json_out, empty_json=build_cost_summary([]))
    writer = _open_writer()
    try:
        rows = writer.list_runs(
            name=name,
            status=status,
            session_key=session,
            since_ms=_parse_since(since),
            limit=limit,
        )
        rows = _hydrate_records(writer, rows)
    finally:
        writer.close()
    summary = build_cost_summary(rows)
    if json_out:
        typer.echo(json.dumps(summary, default=str))
        return
    usage = summary["aggregate"]["usage"]
    typer.echo(f"runs:          {summary['aggregate']['run_count']}")
    typer.echo(f"usage:         {'available' if usage['available'] else 'unavailable'}")
    typer.echo(f"cost_usd:      {usage['cost_usd']:.4f}")
    if not usage["available"]:
        typer.echo(f"reason:        {usage['reason']}")


@runs_app.command("validate")
def runs_validate(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show validation metadata for a historical run."""
    from opensquilla.skills.meta.run_reports import build_validation_summary

    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        rec = writer.get_run(run_id)
    finally:
        writer.close()
    if rec is None:
        typer.echo(f"run not found: {run_id}", err=True)
        raise typer.Exit(2)
    summary = build_validation_summary(rec)
    if json_out:
        typer.echo(json.dumps(summary, default=str))
        return
    typer.echo(f"meta_skill:      {summary['meta_skill_name']}")
    typer.echo(
        "required_fields: "
        + ", ".join(summary["request_template"]["required_fields"])
    )
    typer.echo(f"eval_prompts:    {len(summary['eval_prompts'])}")
    typer.echo(f"policy_tags:     {', '.join(summary['policy_tags'])}")


@runs_app.command("eval-baseline")
def runs_eval_baseline(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show deterministic eval baseline metadata for a historical run."""
    from opensquilla.skills.meta.run_reports import build_eval_baseline

    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        rec = writer.get_run(run_id)
    finally:
        writer.close()
    if rec is None:
        typer.echo(f"run not found: {run_id}", err=True)
        raise typer.Exit(2)
    baseline = build_eval_baseline(rec)
    if json_out:
        typer.echo(json.dumps(baseline, default=str))
        return
    typer.echo(f"available:    {baseline['available']}")
    for item in baseline["items"]:
        typer.echo(
            f"- {item['name']}: {len(item['rubric'])} rubric items "
            f"({item['judge']['status']})"
        )


def _deserialize_plan(snapshot_json: str) -> MetaPlan:
    """Restore a MetaPlan snapshot from its JSON column.

    Delegates to ``plan_serde.from_jsonable`` so we honour the envelope
    format (``{"v": 1, "plan": {...}}``) PR2 introduced and still accept
    legacy snapshots written before PR2 (which used the bare plan dict).
    """
    from opensquilla.skills.meta.plan_serde import from_jsonable
    return from_jsonable(json.loads(snapshot_json))


def _print_dag(
    plan: MetaPlan,
    plan_source: str,
    rendered_inputs_by_step: dict[str, dict[str, Any]],
) -> None:
    typer.echo(f"Meta-skill: {plan.name}     Source: {plan_source}")
    typer.echo(f"Trigger priority: {plan.priority}")
    typer.echo("DAG (topological order):")
    for i, step in enumerate(plan.steps, 1):
        typer.echo(f"  [{i:02}] {step.id}  kind={step.kind}  skill={step.skill}")
        if step.depends_on:
            typer.echo(f"       depends_on: {list(step.depends_on)}")
        if step.on_failure:
            typer.echo(f"       on_failure: {step.on_failure}")
        rendered = rendered_inputs_by_step.get(step.id, {})
        if rendered:
            typer.echo("       rendered inputs (truncated to 200 chars per field):")
            for k, v in rendered.items():
                s = str(v)[:200]
                typer.echo(f"         {k}: {s}")


def _dag_to_json(
    plan: MetaPlan,
    plan_source: str,
    run_id: str,
    rendered_inputs_by_step: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "meta_skill_name": plan.name,
        "plan_source": plan_source,
        "trigger_priority": plan.priority,
        "steps": [
            {
                "order": i,
                "step_id": s.id,
                "kind": s.kind,
                "declared_skill": s.skill,
                "effective_skill": s.skill,
                "depends_on": list(s.depends_on),
                "on_failure": s.on_failure or None,
                "rendered_inputs": rendered_inputs_by_step.get(s.id, {}),
            }
            for i, s in enumerate(plan.steps, 1)
        ],
    }


@runs_app.command("replay")
def runs_replay(
    run_id: str = typer.Argument(...),
    latest: bool = typer.Option(
        False, "--latest", help="Use current registered plan, not historical snapshot",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Replay a historical meta-skill run."""
    if not _db_file_exists():
        _exit_no_history_lookup(run_id)
    writer = _open_writer()
    try:
        rec = writer.get_run(run_id)
    finally:
        writer.close()
    if rec is None:
        typer.echo(f"run not found: {run_id}", err=True)
        raise typer.Exit(2)

    if latest:
        from opensquilla.skills.loader import SkillLoader

        loader = SkillLoader()
        spec = loader.get_by_name(rec.meta_skill_name)
        if spec is None:
            typer.echo(
                "meta-skill no longer registered; cannot --latest replay", err=True,
            )
            raise typer.Exit(2)
        plan = parse_meta_plan(spec)
        if plan is None:
            typer.echo("meta-skill spec exists but is not a meta-skill", err=True)
            raise typer.Exit(2)
        plan_source = "latest_registered"
    else:
        plan = _deserialize_plan(rec.plan_snapshot_json)
        plan_source = "historical_snapshot"

    rendered_by_step: dict[str, dict[str, Any]] = {}
    for s in rec.steps:
        try:
            rendered_by_step[s.step_id] = json.loads(s.rendered_inputs_json)
        except json.JSONDecodeError:
            pass

    if dry_run:
        if json_out:
            typer.echo(
                json.dumps(
                    _dag_to_json(plan, plan_source, run_id, rendered_by_step),
                    default=str,
                )
            )
        else:
            _print_dag(plan, plan_source, rendered_by_step)
        return

    typer.echo(
        "Live replay requires a running gateway; CLI-direct mode unavailable "
        "in this build.",
        err=True,
    )
    typer.echo("Use --dry-run to inspect the DAG.", err=True)
    raise typer.Exit(2)


# ─── Proposals: list / accept ─────────────────────────────────────────────
# meta-skill-creator's `persist` step writes candidate SKILL.md files to
# ~/.opensquilla/proposals/<id>/ alongside a gates.json (lint/smoke
# results). Acceptance promotes a proposal into ~/.opensquilla/skills/
# so the next gateway boot picks it up as a MANAGED-layer skill. The
# core logic mirrors the in-tree
# ``skills/bundled/skill-creator-proposals/scripts/proposals.py`` cmd_accept
# so the CLI and the in-meta-skill code path stay byte-identical.


def _proposals_home() -> Path:
    from opensquilla.paths import default_opensquilla_home

    return Path(default_opensquilla_home())


def _proposals_dir() -> Path:
    return _proposals_home() / "proposals"


def _skills_managed_dir() -> Path:
    return _proposals_home() / "skills"


@meta_app.command("proposals")
def proposals_cmd(
    action: str = typer.Argument(
        ..., help="list | accept | show — proposal CRUD action",
    ),
    proposal_id: str | None = typer.Argument(
        None,
        help="8-hex proposal id (required for accept/show)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Accept even when gates did not all pass",
    ),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """List, inspect, or accept meta-skill proposals.

    ``proposals list``                  — enumerate all candidates
    ``proposals show <id>``             — print one candidate's SKILL.md + gates
    ``proposals accept <id> [--force]`` — promote to MANAGED-layer skill
    """
    import json as _json
    import re
    import shutil

    proposals_dir = _proposals_dir()

    if action == "list":
        rows: list[dict[str, Any]] = []
        if proposals_dir.is_dir():
            for sub in sorted(proposals_dir.iterdir()):
                if not sub.is_dir():
                    continue
                gates_path = sub / "gates.json"
                gates: dict[str, Any] = {}
                if gates_path.is_file():
                    try:
                        gates = _json.loads(gates_path.read_text(encoding="utf-8"))
                    except _json.JSONDecodeError:
                        gates = {}
                rows.append({
                    "proposal_id": sub.name,
                    "auto_enable_eligible": bool(
                        gates.get("auto_enable_eligible", False),
                    ),
                    "skill_md_present": (sub / "SKILL.md").is_file(),
                })
        if json_out:
            typer.echo(_json.dumps({"proposals": rows}, indent=2))
            return
        if not rows:
            typer.echo("(no proposals)")
            return
        typer.echo(f"{'PROPOSAL_ID':12} ELIGIBLE  SKILL_MD")
        typer.echo("-" * 40)
        for r in rows:
            typer.echo(
                f"{r['proposal_id']:12} "
                f"{('yes' if r['auto_enable_eligible'] else 'no'):8}  "
                f"{'present' if r['skill_md_present'] else 'MISSING'}"
            )
        return

    if action in ("show", "accept") and not proposal_id:
        typer.echo(f"Error: '{action}' requires a proposal_id argument", err=True)
        raise typer.Exit(2)

    # ID format check defends against path-traversal — mirrors the script's
    # I1 hardening (uuid.uuid4().hex[:8] write side, 8 hex on read side).
    if proposal_id and not re.fullmatch(r"[0-9a-f]{8}", proposal_id):
        typer.echo(
            f"Error: invalid proposal_id {proposal_id!r} "
            "(expected 8 lowercase hex chars)",
            err=True,
        )
        raise typer.Exit(2)

    src = proposals_dir / (proposal_id or "")

    if action == "show":
        if not (src / "SKILL.md").is_file():
            typer.echo(f"Error: proposal {proposal_id} not found", err=True)
            raise typer.Exit(1)
        gates_text = ""
        if (src / "gates.json").is_file():
            gates_text = (src / "gates.json").read_text(encoding="utf-8")
        skill_md = (src / "SKILL.md").read_text(encoding="utf-8")
        if json_out:
            typer.echo(_json.dumps({
                "proposal_id": proposal_id,
                "skill_md": skill_md,
                "gates": _json.loads(gates_text) if gates_text else {},
            }, indent=2))
            return
        typer.echo(f"=== Proposal {proposal_id} ===")
        if gates_text:
            typer.echo("\n-- gates.json --")
            typer.echo(gates_text)
        typer.echo("\n-- SKILL.md --")
        typer.echo(skill_md)
        return

    # action == "accept"
    if not (src / "SKILL.md").is_file():
        typer.echo(f"Error: proposal {proposal_id} not found", err=True)
        raise typer.Exit(1)

    gates = {}
    if (src / "gates.json").is_file():
        try:
            gates = _json.loads((src / "gates.json").read_text(encoding="utf-8"))
        except _json.JSONDecodeError:
            gates = {}
    if not gates.get("auto_enable_eligible") and not force:
        typer.echo(
            f"Refused: gates did not all pass for {proposal_id}. "
            "Use --force to override.",
            err=True,
        )
        if gates:
            typer.echo(_json.dumps(gates, indent=2), err=True)
        raise typer.Exit(1)

    skill_md = (src / "SKILL.md").read_text(encoding="utf-8")
    # Accept both quoted and unquoted YAML names (N3 fix).
    name_match = re.search(r'^name:\s*"?([\w\-]+)"?\s*$', skill_md, re.MULTILINE)
    if not name_match:
        typer.echo(
            "Error: cannot parse skill name from SKILL.md frontmatter",
            err=True,
        )
        raise typer.Exit(1)
    name = name_match.group(1)

    dst = _skills_managed_dir() / name
    if dst.exists():
        typer.echo(
            f"Refused: skill {name!r} already exists at {dst}. "
            "Remove the existing copy first or rename the proposal.",
            err=True,
        )
        raise typer.Exit(1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    typer.echo(
        f"✅ Accepted proposal {proposal_id} as skill `{name}` at {dst}\n"
        "Restart the gateway to load the new skill from the MANAGED layer."
    )
    if json_out:
        typer.echo(_json.dumps({
            "status": "ok",
            "proposal_id": proposal_id,
            "name": name,
            "skill_path": str(dst),
        }))
