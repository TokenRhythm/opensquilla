#!/usr/bin/env python3
"""Direct shell wrapper around ``git diff`` — meta-skill entrypoint.

Returns the diff text on stdout, the literal ``NO_DIFF`` when the
diff is empty, and exits non-zero with the git error on stderr when
git itself fails (not a repo, missing binary, etc.).

Used by workflows that need repository diffs while skipping a full
sub-Agent loop just to call ``git diff``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_VALID_MODES = {
    "cached_fallback_worktree",
    "cached",
    "worktree",
    "staged_files",
}


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 — argv is constructed from a static allowlist
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _diff_for_mode(mode: str, cwd: Path) -> tuple[int, str, str]:
    if mode == "cached_fallback_worktree":
        rc, out, err = _run_git(["diff", "--cached", "HEAD"], cwd)
        if rc != 0:
            return rc, out, err
        if out.strip():
            return 0, out, err
        return _run_git(["diff", "HEAD"], cwd)
    if mode == "cached":
        return _run_git(["diff", "--cached", "HEAD"], cwd)
    if mode == "worktree":
        return _run_git(["diff", "HEAD"], cwd)
    if mode == "staged_files":
        return _run_git(["diff", "--cached", "--name-only"], cwd)
    raise ValueError(f"unsupported mode {mode!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="cached_fallback_worktree")
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args(argv)

    if args.mode not in _VALID_MODES:
        print(
            f"unsupported mode {args.mode!r}; valid: {sorted(_VALID_MODES)!r}",
            file=sys.stderr,
        )
        return 2

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        print(f"cwd does not exist: {cwd}", file=sys.stderr)
        return 2

    try:
        rc, out, err = _diff_for_mode(args.mode, cwd)
    except FileNotFoundError as exc:
        print(f"git binary not found: {exc}", file=sys.stderr)
        return 1

    if rc != 0:
        sys.stderr.write(err)
        return rc

    sys.stdout.write(out if out.strip() else "NO_DIFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
