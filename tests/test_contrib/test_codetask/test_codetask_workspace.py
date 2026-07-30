"""Unit tests for opensquilla.contrib.codetask.workspace (real git, local)."""

import subprocess

import pytest

from opensquilla.contrib.codetask import workspace


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("def f():\n    return 1\n")
    _git(["init", "-q"], src)
    _git(["config", "user.email", "t@t"], src)
    _git(["config", "user.name", "t"], src)
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "init"], src)
    return src


@pytest.fixture
def empty_source_repo(tmp_path):
    """An initialized git repo with NO commits (unborn HEAD) — the
    build-from-scratch case where the agent scaffolds the whole app."""
    src = tmp_path / "empty_src"
    src.mkdir()
    _git(["init", "-q"], src)
    _git(["config", "user.email", "t@t"], src)
    _git(["config", "user.name", "t"], src)
    return src


def test_prepare_clones_and_branches(monkeypatch, tmp_path, source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("run1", str(source_repo), slug="fix-it")
    assert prepared.path.is_dir()
    assert prepared.branch == "task/fix-it"
    assert prepared.base_commit
    # Source repo is never mutated (we cloned).
    assert (source_repo / "mod.py").exists()


def test_build_artifacts_excluded_from_change(monkeypatch, tmp_path, source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("run2", str(source_repo), slug="fix-it")
    repo = prepared.path

    # Simulate an agent making a real edit AND leaving build junk behind.
    (repo / "mod.py").write_text("def f():\n    return 2\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00junk")
    egg = repo / "pkg.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Metadata-Version: 2.1")

    # A legitimate new source file under a nested "build" dir must NOT be
    # excluded — only the repo-root build output is (anchored patterns).
    nested = repo / "src" / "build"
    nested.mkdir(parents=True)
    (nested / "real_source.py").write_text("VALUE = 1\n")

    files_changed, diffstat, patch = workspace.collect_change(repo, prepared.base_commit)
    # Real source edits captured; build/cache junk excluded.
    assert "mod.py" in patch
    assert "src/build/real_source.py" in patch
    assert "pyc" not in patch
    assert "egg-info" not in patch
    assert files_changed == 2  # mod.py + src/build/real_source.py


def test_exclude_file_written(monkeypatch, tmp_path, source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("run3", str(source_repo), slug="x")
    exclude = prepared.path / ".git" / "info" / "exclude"
    body = exclude.read_text()
    assert "__pycache__/" in body
    assert "*.egg-info/" in body
    assert "!*/build/" in body
    assert "!*/build/**" in body


# --- empty/unborn source repo: build mode scaffolding from scratch ---


def test_prepare_empty_repo_has_no_base_commit(monkeypatch, tmp_path, empty_source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e1", str(empty_source_repo), slug="scaffold")
    assert prepared.path.is_dir()
    assert prepared.base_commit == ""  # unborn HEAD -> no base commit
    assert prepared.branch == "task/scaffold"


def test_collect_change_empty_base_no_commit(monkeypatch, tmp_path, empty_source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e2", str(empty_source_repo), slug="s")
    repo = prepared.path
    (repo / "package.json").write_text('{"name":"x"}\n')
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1\n")

    files_changed, diffstat, patch = workspace.collect_change(repo, prepared.base_commit)
    assert files_changed == 2
    assert "package.json" in patch and "src/index.ts" in patch


def test_collect_change_empty_base_after_agent_commit(
    monkeypatch, tmp_path, empty_source_repo
):
    """The agent committed its scaffold mid-run; the whole tree is still
    captured. A bare `git diff --cached` (vs the new HEAD) would miss this."""
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e3", str(empty_source_repo), slug="s")
    repo = prepared.path
    (repo / "package.json").write_text('{"name":"x"}\n')
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    files_changed, diffstat, patch = workspace.collect_change(repo, prepared.base_commit)
    assert files_changed == 2
    assert "package.json" in patch and "src/index.ts" in patch


def test_collect_change_invalid_base_raises(monkeypatch, tmp_path, source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e4", str(source_repo), slug="s")
    repo = prepared.path
    (repo / "mod.py").write_text("def f():\n    return 2\n")
    with pytest.raises(workspace.WorkspaceError):
        workspace.collect_change(repo, "deadbeef" * 5)  # nonexistent ref


def test_count_commits_empty_base_no_head(monkeypatch, tmp_path, empty_source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e5", str(empty_source_repo), slug="s")
    assert workspace.count_commits(prepared.path, "") == 0


def test_count_commits_empty_base_with_head(monkeypatch, tmp_path, empty_source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    prepared = workspace.prepare_repo("e6", str(empty_source_repo), slug="s")
    repo = prepared.path
    (repo / "f.txt").write_text("x\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    assert workspace.count_commits(repo, "") == 1


def test_prepare_empty_repo_with_base_raises(monkeypatch, tmp_path, empty_source_repo):
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(tmp_path / "runs"))
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare_repo("e7", str(empty_source_repo), base_ref="main", slug="s")
