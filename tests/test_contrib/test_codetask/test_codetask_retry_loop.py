"""Tests for the verify->fix retry loop in runner.solve."""

import re
from types import SimpleNamespace

from opensquilla.contrib.codetask import runner
from opensquilla.contrib.codetask.types import (
    AcceptanceCheck,
    RegressionResult,
    TaskState,
)
from opensquilla.contrib.codetask.verification import VerificationOutcome


class _Outcome:
    def __init__(self, timeout=False):
        self.usage = {"total_tokens": 10, "model": "m"}
        self.duration_seconds = 1.0
        self.timeout = timeout


class _FakeAdapter:
    runs = 0
    last_timeout = None

    def __init__(self, **kw):
        _FakeAdapter.last_timeout = kw.get("timeout")

    def run(
        self,
        prompt,
        *,
        repo,
        scratch_dir,
        artifact_dir,
        status_callback=None,
        on_agent_started=None,
    ):
        _FakeAdapter.runs += 1
        (artifact_dir / "agent_stdout.log").write_text("log", encoding="utf-8")
        if on_agent_started is not None:
            on_agent_started()
        return _Outcome()


def _vout(state, *, nf=None, failing=None):
    acc = []
    for n in failing or []:
        c = AcceptanceCheck(name=n, command="cmd")
        c.after = "fail"
        acc.append(c)
    reg = RegressionResult(new_failures=nf) if nf is not None else None
    return VerificationOutcome(
        state=state, acceptance=acc, regression=reg, assumptions=[], detail="d"
    )


def _wire(monkeypatch, tmp_path, outcomes, collects=None):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    # Subagent-config assembly is kept hermetic by the package autouse fixture
    # (_isolate_agent_config_discovery), so solve()'s up-front build reads no
    # developer config/env.
    _FakeAdapter.runs = 0
    repo = tmp_path / "repo"
    repo.mkdir()
    prepared = SimpleNamespace(path=repo, base_commit="base", base_ref="main", branch="task/x")
    monkeypatch.setattr(runner.workspace, "prepare_repo", lambda *a, **k: prepared)
    monkeypatch.setattr(
        runner, "resolve_task", lambda **k: SimpleNamespace(slug="x", source="inline")
    )
    monkeypatch.setattr(runner, "render_task_md", lambda *a, **k: "task body")
    monkeypatch.setattr(runner.envprobe, "probe", lambda p: SimpleNamespace(as_hints=lambda: ""))
    monkeypatch.setattr(runner, "_render_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(runner, "LocalAdapter", _FakeAdapter)
    cit = iter(collects or [])
    monkeypatch.setattr(
        runner.workspace, "collect_change", lambda *a, **k: next(cit, (1, "stat", "diff"))
    )
    monkeypatch.setattr(runner.workspace, "count_commits", lambda *a, **k: 1)
    monkeypatch.setattr(runner, "_archive_manifest", lambda *a, **k: None)
    oit = iter(outcomes)
    monkeypatch.setattr(runner, "verify", lambda **k: next(oit))


def test_loop_retries_failed_then_verifies(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [
        _vout(TaskState.FAILED, nf=2), _vout(TaskState.FAILED, nf=1), _vout(TaskState.VERIFIED),
    ])
    observed: list[str] = []
    res = runner.solve(
        repo="/tmp/x",
        task="do",
        max_attempts=3,
        timeout=3600,
        coding_mode_usage_recorder=observed.append,
    )
    assert res.state == TaskState.VERIFIED
    assert res.attempts == 3
    assert _FakeAdapter.runs == 3
    assert res.retry_exhausted is False
    run_dir = runner.config.run_dir(res.run_id)
    assert (run_dir / "attempts" / "01").is_dir()
    assert (run_dir / "attempts" / "03").is_dir()
    assert res.usage.get("total_tokens") == 30
    assert len(observed) == 1
    assert observed[0] != res.run_id
    assert re.fullmatch(r"coding-use-[0-9a-f]{24}", observed[0])


def test_loop_stops_at_max_attempts_and_marks_exhausted(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_vout(TaskState.FAILED, nf=2), _vout(TaskState.FAILED, nf=1)])
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=2, timeout=3600)
    assert res.state == TaskState.FAILED
    assert res.attempts == 2
    assert _FakeAdapter.runs == 2
    assert res.retry_exhausted is True
    assert res.relaunch_recommended is False
    assert res.final_failure_reason


def test_loop_stops_on_no_progress(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [
        _vout(TaskState.FAILED, nf=1, failing=["t1"]),
        _vout(TaskState.FAILED, nf=1, failing=["t1"]),
        _vout(TaskState.VERIFIED),
    ])
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=3600)
    assert res.state == TaskState.FAILED
    assert res.attempts == 2
    assert "no progress" in (res.final_failure_reason or "")


def test_loop_stops_on_diff_explosion(monkeypatch, tmp_path):
    _wire(
        monkeypatch, tmp_path,
        [_vout(TaskState.FAILED, nf=2), _vout(TaskState.FAILED, nf=1), _vout(TaskState.VERIFIED)],
        collects=[(2, "stat", "x" * 100), (80, "stat", "x" * 100)],
    )
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=3600)
    assert res.attempts == 2
    assert "diff cap" in (res.final_failure_reason or "")


def test_loop_does_not_retry_not_testable(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_vout(TaskState.NOT_TESTABLE), _vout(TaskState.VERIFIED)])
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=3600)
    assert res.state == TaskState.NOT_TESTABLE
    assert res.attempts == 1
    assert _FakeAdapter.runs == 1


def test_attempt1_honors_small_timeout_not_inflated(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_vout(TaskState.VERIFIED)])
    runner.solve(repo="/tmp/x", task="do", max_attempts=1, timeout=60)
    # honors timeout minus an adaptive verify reserve (min(300, 60//4)=15)
    assert _FakeAdapter.last_timeout == 45


def test_retry_skipped_for_budget_is_not_counted(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_vout(TaskState.FAILED, nf=2), _vout(TaskState.VERIFIED)])
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=320)
    assert res.attempts == 1
    assert _FakeAdapter.runs == 1


def test_empty_patch_retry_clears_stale_artifacts(monkeypatch, tmp_path):
    _wire(
        monkeypatch, tmp_path,
        [_vout(TaskState.FAILED, nf=2), _vout(TaskState.FAILED, nf=1)],
        collects=[(1, "stat", "patch-1"), (0, "stat", "")],
    )
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=2, timeout=3600)
    run_dir = runner.config.run_dir(res.run_id)
    assert not (run_dir / "change.patch").exists()
    assert res.patch_path is None


def test_guardrail_stop_still_writes_verify_failure(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [
        _vout(TaskState.FAILED, nf=1, failing=["t1"]),
        _vout(TaskState.FAILED, nf=1, failing=["t1"]),
        _vout(TaskState.VERIFIED),
    ])
    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=3600)
    run_dir = runner.config.run_dir(res.run_id)
    assert (run_dir / "attempts" / "02" / "verify_failure.txt").exists()


def test_aggregate_usage():
    assert runner._aggregate_usage({}, {"a": 1}) == {"a": 1}
    assert runner._aggregate_usage({"a": 1, "m": "x"}, {"a": 2, "m": "y"}) == {"a": 3, "m": "y"}


def test_failure_signature_distinguishes_failures():
    a = _vout(TaskState.FAILED, nf=1, failing=["t1"])
    c = _vout(TaskState.FAILED, nf=2, failing=["t1"])
    assert runner._failure_signature(a) != runner._failure_signature(c)


def test_redact_masks_secrets():
    r1 = runner._redact("API_KEY=sk-abc123")
    assert "<redacted>" in r1 and "sk-abc123" not in r1
    r2 = runner._redact("Authorization: Bearer xyztoken")
    assert "<redacted>" in r2 and "xyztoken" not in r2
    assert runner._redact("normal output line") == "normal output line"


def test_solve_blocks_early_on_invalid_agent_config(monkeypatch, tmp_path):
    """A broken operator config fails loud BEFORE the clone, with the reason
    persisted where the calling agent reads it (result.json + status.json)."""
    import json

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))

    def _boom():
        raise runner.AgentConfigError(
            "code-task subagent config is invalid after inheriting provider "
            "settings from /x/config.toml: tier_profile mismatch"
        )

    monkeypatch.setattr(runner, "load_agent_config_bundle", _boom)
    cloned = []
    monkeypatch.setattr(
        runner.workspace, "prepare_repo", lambda *a, **k: cloned.append(1)
    )

    res = runner.solve(repo="/tmp/x", task="do")

    assert res.state == TaskState.ENVIRONMENT_BLOCKED
    assert "tier_profile mismatch" in (res.error or "")
    assert res.final_failure_reason == res.error
    assert cloned == []  # failed before any expensive work
    run_dir = runner.config.run_dir(res.run_id)
    persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert persisted["state"] == "environment_blocked"
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "completed"
    assert "tier_profile mismatch" in status["error"]


def test_solve_blocks_before_clone_on_preflight_failure(monkeypatch, tmp_path):
    """A rejected/missing credential fails the run before the clone, with the
    actionable reason persisted for the calling agent."""
    import json

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        runner, "provider_preflight", lambda *a, **k: (False, "provider X has no usable key")
    )
    cloned = []
    observed: list[str] = []
    monkeypatch.setattr(runner.workspace, "prepare_repo", lambda *a, **k: cloned.append(1))

    res = runner.solve(
        repo="/tmp/x",
        task="do",
        coding_mode_usage_recorder=observed.append,
    )

    assert res.state == TaskState.ENVIRONMENT_BLOCKED
    assert res.error == "provider X has no usable key"
    assert res.final_failure_reason == res.error
    assert cloned == []
    assert observed == []
    run_dir = runner.config.run_dir(res.run_id)
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "completed"
    assert "no usable key" in status["error"]


class _ProviderErrorAdapter:
    def __init__(self, **kw):
        pass

    def run(self, prompt, *, repo, scratch_dir, artifact_dir, **kw):
        (artifact_dir / "agent_stdout.log").write_text("boom", encoding="utf-8")
        return SimpleNamespace(
            timeout=False,
            finish_reason="error",
            error=None,
            errors=[{"code": "402", "message": "insufficient balance"}],
            usage={},
            duration_seconds=1.0,
        )


def test_solve_maps_midrun_credential_error_to_blocked(monkeypatch, tmp_path):
    """A credential-class provider error mid-run stops as ENVIRONMENT_BLOCKED
    with the real reason, instead of retrying into a misleading manifest error."""
    _wire(monkeypatch, tmp_path, [])  # verify is never reached
    monkeypatch.setattr(runner, "LocalAdapter", _ProviderErrorAdapter)
    verify_calls = []
    monkeypatch.setattr(runner, "verify", lambda **k: verify_calls.append(1))

    res = runner.solve(repo="/tmp/x", task="do", max_attempts=3, timeout=3600)

    assert res.state == TaskState.ENVIRONMENT_BLOCKED
    assert res.attempts == 1  # not retried
    assert verify_calls == []  # short-circuited before verification
    assert "provider" in (res.error or "").lower()
