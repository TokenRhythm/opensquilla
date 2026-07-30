from __future__ import annotations

import shlex
from pathlib import Path

from opensquilla.cli.tui.source_checkout import resolve_tui_source_checkout_hint


def _write_checkout(tmp_path: Path, *, git_marker: bool = True) -> Path:
    root = tmp_path / "checkout"
    module = root / "src" / "opensquilla" / "cli" / "tui" / "source_checkout.py"
    package = root / "src" / "opensquilla" / "cli" / "tui" / "opentui" / "package"
    module.parent.mkdir(parents=True)
    (package / "src").mkdir(parents=True)
    module.write_text("# test\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (package / "package.json").write_text("{}\n", encoding="utf-8")
    (package / "bun.lock").write_text("{}\n", encoding="utf-8")
    (package / "src" / "main.mjs").write_text("// test\n", encoding="utf-8")
    if git_marker:
        (root / ".git").write_text("gitdir: /tmp/test\n", encoding="utf-8")
    return module


def test_source_checkout_hint_accepts_git_worktree_file(tmp_path: Path) -> None:
    module = _write_checkout(tmp_path)

    hint = resolve_tui_source_checkout_hint(
        module_file=module,
        platform_name="darwin",
        arch="arm64",
    )

    assert hint is not None
    assert "bun install --frozen-lockfile --cwd" in hint.install_command
    assert "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1" in hint.launch_command
    assert f"uv --directory {shlex.quote(str(tmp_path / 'checkout'))}" in hint.launch_command
    assert hint.launch_command.endswith("run opensquilla chat --ui tui")


def test_source_archive_without_git_marker_stays_quiet(tmp_path: Path) -> None:
    module = _write_checkout(tmp_path, git_marker=False)

    assert (
        resolve_tui_source_checkout_hint(
            module_file=module,
            platform_name="linux",
            arch="x86_64",
        )
        is None
    )


def test_installed_layout_inside_unrelated_git_repo_stays_quiet(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    module = root / ".venv" / "site-packages" / "opensquilla" / "cli" / "tui" / "source_checkout.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed\n", encoding="utf-8")
    (root / ".git").mkdir(parents=True)

    assert (
        resolve_tui_source_checkout_hint(
            module_file=module,
            platform_name="darwin",
            arch="arm64",
        )
        is None
    )


def test_source_hint_stays_quiet_on_windows(tmp_path: Path) -> None:
    module = _write_checkout(tmp_path)

    assert (
        resolve_tui_source_checkout_hint(
            module_file=module,
            platform_name="win32",
            arch="x86_64",
        )
        is None
    )


def test_source_hint_stays_quiet_on_unsupported_platform_or_arch(tmp_path: Path) -> None:
    module = _write_checkout(tmp_path)

    assert (
        resolve_tui_source_checkout_hint(
            module_file=module,
            platform_name="freebsd",
            arch="x86_64",
        )
        is None
    )
    assert (
        resolve_tui_source_checkout_hint(
            module_file=module,
            platform_name="linux",
            arch="riscv64",
        )
        is None
    )


def test_source_hint_rejects_terminal_control_characters_in_path(tmp_path: Path) -> None:
    module = _write_checkout(tmp_path)
    unsafe = Path(f"{module}\x1b[31m")

    assert (
        resolve_tui_source_checkout_hint(
            module_file=unsafe,
            platform_name="darwin",
            arch="arm64",
        )
        is None
    )
