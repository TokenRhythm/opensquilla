from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.cli.tui.opentui import completion
from opensquilla.cli.tui.opentui.completion import enumerate_workspace_files


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_enumerate_workspace_files_fallback_filters_ignored_hidden_and_sensitive_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\nbuild/\n*.tmp\n", encoding="utf-8")
    _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "src" / "compact.py")
    _touch(tmp_path / "docs" / "reset.md")
    _touch(tmp_path / "ignored.txt")
    _touch(tmp_path / "build" / "artifact.log")
    _touch(tmp_path / ".hidden" / "shadow.py")
    _touch(tmp_path / "__pycache__" / "cached.pyc")
    _touch(tmp_path / "scratch.tmp")
    _touch(tmp_path / ".env")

    assert enumerate_workspace_files(tmp_path) == [
        ".gitignore",
        "docs/reset.md",
        "src/compact.py",
        "src/main.py",
    ]


def test_enumerate_workspace_files_fallback_honors_gitignore_negations(tmp_path: Path) -> None:
    # git semantics: "!keep.log" AFTER "*.log" re-includes the file.
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
    _touch(tmp_path / "keep.log")
    _touch(tmp_path / "drop.log")
    _touch(tmp_path / "main.py")

    assert enumerate_workspace_files(tmp_path) == [".gitignore", "keep.log", "main.py"]


def test_enumerate_workspace_files_fallback_negation_last_match_wins(tmp_path: Path) -> None:
    # A later exclude overrides an earlier re-include, mirroring git's
    # last-match-wins rule ordering.
    (tmp_path / ".gitignore").write_text("!keep.log\n*.log\n", encoding="utf-8")
    _touch(tmp_path / "keep.log")

    assert enumerate_workspace_files(tmp_path) == [".gitignore"]


def test_enumerate_workspace_files_applies_query_and_max_results(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "compact.py")
    _touch(tmp_path / "src" / "component.py")
    _touch(tmp_path / "docs" / "commands.md")
    _touch(tmp_path / "docs" / "reset.md")

    assert enumerate_workspace_files(tmp_path, query="cmp", max_results=2) == [
        "src/compact.py",
        "src/component.py",
    ]


def test_enumerate_workspace_files_uses_git_ls_files_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    _touch(tmp_path / "src" / "compact.py")
    _touch(tmp_path / "src" / ".env")
    _touch(tmp_path / "docs" / "reset.md")
    calls: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        calls["cmd"] = cmd
        return SimpleNamespace(
            ok=True,
            stdout=b"src/compact.py\0src/.env\0docs/reset.md\0",
        )

    monkeypatch.setattr(completion, "run_git", fake_run)

    assert enumerate_workspace_files(tmp_path, query="cmp") == ["src/compact.py"]
    # NUL-separated output keeps non-ASCII paths verbatim (no core.quotePath
    # C-quoting), so -z is load-bearing.
    assert "-z" in calls["cmd"]


def test_git_files_probe_repository_from_a_subdirectory_without_dot_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subdirectory = tmp_path / "src"
    subdirectory.mkdir()
    calls: list[Path] = []

    def fake_run(_args: object, **kwargs: object) -> SimpleNamespace:
        calls.append(Path(str(kwargs["cwd"])))
        return SimpleNamespace(ok=True, stdout=b"module.py\0")

    monkeypatch.setattr(completion, "run_git", fake_run)

    assert completion._git_files(subdirectory) == ["module.py"]
    assert calls == [subdirectory]


def test_git_files_returns_non_ascii_paths_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    _touch(tmp_path / "café.md")
    _touch(tmp_path / "日本語.txt")
    stdout = "café.md".encode() + b"\0" + "日本語.txt".encode() + b"\0"
    monkeypatch.setattr(
        completion,
        "run_git",
        lambda *args, **kwargs: SimpleNamespace(ok=True, stdout=stdout),
    )

    assert enumerate_workspace_files(tmp_path) == ["café.md", "日本語.txt"]


def test_git_files_tolerates_undecodable_path_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-UTF-8 filename byte (e.g. latin-1 0xE9) must never crash completion;
    # it decodes through the filesystem rules (surrogateescape) instead.
    (tmp_path / ".git").mkdir()
    # Windows uses surrogatepass for filesystem decoding, which still raises
    # for this malformed UTF-8 byte. Simulate that handler on every platform so
    # the fallback remains covered outside the Windows CI job too.
    monkeypatch.setattr(
        completion.os,
        "fsdecode",
        lambda entry: entry.decode("utf-8", errors="surrogatepass"),
    )
    monkeypatch.setattr(
        completion,
        "run_git",
        lambda *args, **kwargs: SimpleNamespace(
            ok=True, stdout=b"caf\xe9.md\0plain.txt\0"
        ),
    )

    names = enumerate_workspace_files(tmp_path)
    assert "plain.txt" in names
    expected = b"caf\xe9.md".decode(
        sys.getfilesystemencoding(), errors="surrogateescape"
    )
    assert expected in names


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_enumerate_workspace_files_real_git_does_not_quote_non_ascii_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate from user/system git config so the default core.quotePath=true
    # applies — the exact setting that C-quotes non-ASCII names without -z.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    _touch(tmp_path / "café.md")

    names = enumerate_workspace_files(tmp_path)

    assert all(not name.startswith('"') for name in names)
    assert any(
        unicodedata.normalize("NFC", name) == "café.md" for name in names
    ), f"expected café.md in {names!r}"
