"""Offline parameter and isolation checks for the opt-in Gateway comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

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
