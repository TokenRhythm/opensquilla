"""CLI smoke tests for `opensquilla skills meta runs ...`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app as cli_app
from opensquilla.persistence.meta_run_writer import open_meta_run_writer
from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded_db(migrated_db: Path, monkeypatch):
    db = str(migrated_db)
    w = open_meta_run_writer(db)

    plan_a = MetaPlan(
        name="alpha-skill", triggers=("t",), priority=10,
        steps=(MetaStep(id="s1", skill="x", kind="agent"),),
    )
    plan_b = MetaPlan(
        name="beta-skill", triggers=("t",), priority=10,
        steps=(MetaStep(id="s1", skill="y", kind="agent"),),
    )
    rid_ok = w.begin_run_sync(
        meta_skill_name="alpha-skill", meta_plan=plan_a,
        triggered_by="soft_meta_invoke", inputs={"user_message": "hi"},
        session_key="sess-1", turn_id="turn-1",
    )
    w.begin_step_sync(
        run_id=rid_ok, step=plan_a.steps[0], effective_skill="x",
        rendered_inputs={"a": 1},
    )
    w.finish_step_sync(
        run_id=rid_ok, step_id="s1", status="ok", output_text="alpha-out",
    )
    w.finish_run_sync(
        run_id=rid_ok, status="ok",
        result=MetaResult(ok=True, final_text="alpha-out"),
    )

    rid_fail = w.begin_run_sync(
        meta_skill_name="beta-skill", meta_plan=plan_b,
        triggered_by="hard_takeover", inputs={},
        session_key=None, turn_id=None,
    )
    w.finish_run_sync(
        run_id=rid_fail, status="failed",
        result=MetaResult(ok=False, error="boom", failed_step_id="s1"),
    )
    w.close()

    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", db)
    return {"db": db, "rid_ok": rid_ok, "rid_fail": rid_fail}


def test_runs_list(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2


def test_runs_list_filter_status(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app,
        ["skills", "meta", "runs", "list", "--status", "failed", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["status"] == "failed"


def test_runs_show(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "show", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["meta_skill_name"] == "alpha-skill"
    assert data["status"] == "ok"
    assert data["summary"]["step_count"] == 1
    assert data["summary"]["usage"]["available"] is False


def test_runs_steps(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "steps", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["step_id"] == "s1"
    assert data[0]["status"] == "ok"


def test_runs_failures(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "failures", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["status"] == "failed"


def test_runs_replay_dry_run(runner: CliRunner, seeded_db) -> None:
    """W8: --dry-run prints DAG in the spec'd format."""
    result = runner.invoke(
        cli_app,
        ["skills", "meta", "runs", "replay", seeded_db["rid_ok"], "--dry-run", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["meta_skill_name"] == "alpha-skill"
    assert data["plan_source"] == "historical_snapshot"
    assert len(data["steps"]) == 1


def test_runs_draft_json(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "draft", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["source_run"]["run_id"] == seeded_db["rid_ok"]
    assert data["name"] == "alpha-skill-draft"
    assert data["composition"]["steps"][0]["id"] == "s1"
    assert data["trigger_candidates"]


def test_runs_show_bad_id(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "show", "BOGUS", "--json"])
    assert result.exit_code != 0


def test_runs_list_empty(runner: CliRunner, migrated_db: Path, monkeypatch) -> None:
    db = str(migrated_db)
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", db)
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_runs_list_missing_db_prints_friendly_notice(
    runner: CliRunner, tmp_path: Path, monkeypatch,
) -> None:
    """Before first gateway boot there is no sessions.db: the CLI must not
    traceback on a missing state dir and must not create a stray DB file."""
    db = tmp_path / "state" / "sessions.db"  # parent dir intentionally missing
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", str(db))
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list"])
    assert result.exit_code == 0, result.output
    assert "no meta-run history yet" in result.output
    assert not db.exists()
    assert not db.parent.exists()


def test_runs_list_missing_db_json_emits_empty_list(
    runner: CliRunner, tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "state" / "sessions.db"
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", str(db))
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    assert not db.exists()


def test_runs_failures_missing_db_json_emits_empty_list(
    runner: CliRunner, tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "state" / "sessions.db"
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", str(db))
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "failures", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    assert not db.exists()


def test_runs_cost_missing_db_json_emits_empty_summary_shape(
    runner: CliRunner, tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "state" / "sessions.db"
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", str(db))
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "cost", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["aggregate"]["run_count"] == 0
    assert data["runs"] == []
    assert not db.exists()


def test_runs_show_missing_db_exits_not_found_without_creating_db(
    runner: CliRunner, tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "state" / "sessions.db"
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", str(db))
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "show", "SOMEID"])
    assert result.exit_code == 2
    assert not db.exists()
    assert not db.parent.exists()
