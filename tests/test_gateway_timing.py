"""Offline parameter and isolation checks for the opt-in Gateway comparison."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import compare_gateway_timing as timing
from scripts.compare_gateway_timing import (
    sample_environment,
    validate_sources,
    verify_source_identity,
)


def test_sample_environment_does_not_inherit_secrets_or_python_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "synthetic-secret")
    monkeypatch.setenv("UNRECOGNIZED_PROVIDER_TOKEN", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "unrelated-source")
    monkeypatch.setenv("PYTHONSTARTUP", "unrelated-startup")
    env = sample_environment(tmp_path / "source", tmp_path / "profile")
    assert "DATABASE_URL" not in env
    assert "UNRECOGNIZED_PROVIDER_TOKEN" not in env
    assert "PYTHONSTARTUP" not in env
    assert env["PYTHONPATH"] == str(tmp_path / "source/src")
    assert env["PYTHONNOUSERSITE"] == "1"
    assert Path(env["HOME"]).is_relative_to(tmp_path / "profile")


@pytest.mark.parametrize("samples", [0, 11])
def test_comparison_rejects_unbounded_sample_counts(tmp_path, samples):
    with pytest.raises(ValueError, match="samples"):
        validate_sources(tmp_path, tmp_path, samples)


@pytest.mark.parametrize("different", ["uv.lock", "pyproject.toml"])
def test_comparison_rejects_different_dependency_inputs(tmp_path, different):
    roots = [tmp_path / "baseline", tmp_path / "candidate"]
    for root in roots:
        module = root / "src/opensquilla/gateway/boot.py"
        module.parent.mkdir(parents=True)
        module.write_text("# synthetic fixture\n")
        for name in ("uv.lock", "pyproject.toml"):
            (root / name).write_text("same")
    (roots[1] / different).write_text("different")
    with pytest.raises(ValueError, match=different):
        validate_sources(*roots, 3)


def test_archive_identity_must_match_embedded_commit(tmp_path):
    sha = "a" * 40
    (tmp_path / ".gateway-timing-source-sha").write_text(sha + "\n")
    verify_source_identity(tmp_path, sha, archived=True)
    with pytest.raises(ValueError, match="archive commit identity"):
        verify_source_identity(tmp_path, "b" * 40, archived=True)


def test_checkout_identity_rejects_dirty_source(tmp_path, monkeypatch):
    responses = iter([str(tmp_path), "a" * 40, " M src/opensquilla/gateway/boot.py"])
    monkeypatch.setattr(
        "scripts.compare_gateway_timing.subprocess.check_output",
        lambda *args, **kwargs: next(responses),
    )
    with pytest.raises(ValueError, match="exact clean Git checkout"):
        verify_source_identity(tmp_path, "a" * 40)


@pytest.fixture
def comparison(tmp_path, monkeypatch):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    for root in (baseline, candidate):
        module = root / "src/opensquilla/gateway/boot.py"
        module.parent.mkdir(parents=True)
        module.write_text("# synthetic fixture\n", encoding="utf-8")
        for name in ("uv.lock", "pyproject.toml"):
            (root / name).write_text(root.name + name, encoding="utf-8")
    baseline_python = tmp_path / "baseline-env/bin/python"
    identities = {
        str(executable): {
            "version": [3, 12, 14],
            "implementation": "cpython",
            "machine": "x86_64",
            "prefix": str(prefix),
        }
        for executable, prefix in (
            (baseline_python, tmp_path / "baseline-env"),
            (sys.executable, tmp_path / "candidate-env"),
        )
    }
    monkeypatch.setattr(
        timing, "_interpreter_identity", lambda executable: dict(identities[executable])
    )
    verified, calls = [], []
    monkeypatch.setattr(
        timing,
        "verify_source_identity",
        lambda root, sha, **options: verified.append((root, sha, options)),
    )

    def sample(command, **options):
        calls.append((command, options))
        scale = 1 if options["cwd"] == baseline else 2
        row = {
            "startup_to_health_ms": 100 * scale,
            "default_first_turn_ms": 20 * scale,
            "provider_calls": 1,
            "terminal_events": 1,
            "goals": 0,
        }
        return SimpleNamespace(returncode=0, stdout="fixture log\n" + json.dumps(row), stderr="")

    monkeypatch.setattr(timing.subprocess, "run", sample)
    return SimpleNamespace(
        baseline=baseline,
        candidate=candidate,
        baseline_python=baseline_python,
        identities=identities,
        verified=verified,
        calls=calls,
        arguments={
            "baseline": baseline,
            "candidate": candidate,
            "baseline_sha": "a" * 40,
            "candidate_sha": "b" * 40,
            "samples": 3,
            "baseline_python": baseline_python,
        },
    )


def test_compare_default_keeps_dependency_guard(comparison):
    arguments = {
        key: value for key, value in comparison.arguments.items() if key != "baseline_python"
    }
    with pytest.raises(ValueError, match="identical uv.lock"):
        timing.compare(**arguments)
    assert comparison.calls == []


def test_separate_environments_alternate_correct_interpreters_and_record_evidence(comparison):
    e = comparison
    report = timing.compare(**e.arguments)
    expected_order = ["baseline", "candidate", "candidate", "baseline", "baseline", "candidate"]
    assert [options["cwd"].name for _, options in e.calls] == expected_order
    harness = str(e.candidate / "tests/functional/test_gateway_silent_reply_process_e2e.py")
    profiles = []
    for label, (command, options) in zip(expected_order, e.calls, strict=True):
        source = getattr(e, label)
        assert command[0] == (str(e.baseline_python) if label == "baseline" else sys.executable)
        assert command[1] == harness
        assert command[-2:] == ["--source-root", str(source)]
        assert options["env"]["PYTHONPATH"] == str(source / "src")
        profiles.append(options["env"]["OPENSQUILLA_HOME"])
    assert len(set(profiles)) == 6
    assert e.verified == [
        (e.baseline, "a" * 40, {"archived": True}),
        (e.candidate, "b" * 40, {}),
    ]
    assert report["dependency_mode"] == "per-source-environments"
    for label in ("baseline", "candidate"):
        assert report["dependency_inputs"][label] == {
            name: hashlib.sha256((getattr(e, label) / name).read_bytes()).hexdigest()
            for name in ("uv.lock", "pyproject.toml")
        }
        assert report["interpreters"][label] == {
            "version": [3, 12, 14],
            "implementation": "cpython",
            "machine": "x86_64",
        }
        assert len(report["samples"][label]) == 3
    assert report["uv_lock_sha256"] == report["dependency_inputs"]["candidate"]["uv.lock"]
    assert report["pyproject_sha256"] == report["dependency_inputs"]["candidate"]["pyproject.toml"]
    assert report["candidate_over_baseline"] == {
        "startup_to_health_ms": 2.0,
        "default_first_turn_ms": 2.0,
    }
    assert str(e.baseline.parent) not in json.dumps(report)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", [3, 13, 14]),
        ("implementation", "pypy"),
        ("machine", "aarch64"),
    ],
)
def test_comparison_rejects_incomparable_interpreters(comparison, field, value):
    comparison.identities[str(comparison.baseline_python)][field] = value
    with pytest.raises(ValueError, match=f"identical Python {field}"):
        timing.compare(**comparison.arguments)
    assert comparison.calls == []


@pytest.mark.parametrize("path_alias", [False, True])
def test_different_dependencies_reject_same_environment(comparison, path_alias):
    prefix = Path(comparison.identities[sys.executable]["prefix"])
    if path_alias:
        prefix = prefix / ".." / prefix.name
    comparison.identities[str(comparison.baseline_python)]["prefix"] = str(prefix)
    with pytest.raises(ValueError, match="separate Python environments"):
        timing.compare(**comparison.arguments)
    assert comparison.calls == []


def test_separate_environment_failure_is_not_ignored(comparison, monkeypatch, capsys):
    monkeypatch.setattr(
        timing.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="synthetic baseline launch failure",
        ),
    )
    with pytest.raises(RuntimeError, match="baseline timing sample 1 failed"):
        timing.compare(**comparison.arguments)
    assert "synthetic baseline launch failure" in capsys.readouterr().err


def test_interpreter_identity_uses_real_isolated_python(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "unrelated-source")
    identity = timing._interpreter_identity(sys.executable)
    assert identity["version"] == list(sys.version_info[:3])
    assert identity["implementation"] == sys.implementation.name
    assert identity["prefix"] == sys.prefix
