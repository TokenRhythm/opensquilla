"""Destructive-command intent extraction for sensitive-path detection.

Pulls the delete/remove targets out of a shell or Python command so
:mod:`opensquilla.sandbox.sensitive_paths` can check each target against the
sensitive-path deny list. This is pure parsing — it holds no state and makes no
approval decision. (It was previously bundled with a session-scoped approval
cache; that cache was an unused no-op and has been removed.)

Scope: only *delete* intents for now, since that is the bulk of user-observed
pain. Extend :func:`_extract_intents` if other classes (write-outside-workspace,
network egress) need target extraction.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def _norm_path(raw: str, *, base_dir: str | Path | None = None) -> str:
    """Best-effort absolute-path normalization.

    Leaves non-path tokens alone (so ``*`` or variable references don't get
    expanded into something wrong).
    """
    if not raw or raw.startswith(("$", "`")) or raw in {"*", "-"}:
        return raw
    try:
        path = Path(raw).expanduser()
        if base_dir is not None and not path.is_absolute():
            path = Path(base_dir).expanduser() / path
        return str(path.resolve(strict=False))
    except (OSError, ValueError):
        return raw


# Regex-based single-capture extractors for Python-flavoured deletes. Each
# regex uses ``finditer`` so ``shutil.rmtree("a"); os.remove("b")`` yields
# both paths.
_PY_DELETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bos\.(?:remove|unlink|rmdir|removedirs)\s*\(\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\bshutil\.rmtree\s*\(\s*[\"']([^\"']+)[\"']"),
    re.compile(
        r"\b(?:pathlib\.)?Path\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*\.(?:unlink|rmdir)\s*\("
    ),
)

# Shell command separators that terminate a single ``rm`` invocation.
_SHELL_SEPARATORS = (";", "&&", "||", "|", "&")


def _extract_rm_targets(command: str) -> list[str]:
    """Pull every non-flag argument out of an ``rm`` invocation.

    Handles ``rm a b c``, ``rm -rf /a /b``, quoted paths, and stops at shell
    separators. Does not try to be a full shell parser — falls back to
    whitespace split on shlex errors (unbalanced quotes).
    """
    match = re.search(r"\brm\b([^\n]*)", command)
    if not match:
        return []
    tail = match.group(1)

    # Cut at the first shell separator so ``rm foo; ls bar`` doesn't pick ``ls``/``bar``.
    cut = len(tail)
    for sep in _SHELL_SEPARATORS:
        idx = tail.find(sep)
        if idx != -1 and idx < cut:
            cut = idx
    tail = tail[:cut].strip()
    if not tail:
        return []

    token_sets: list[list[str]] = []
    try:
        token_sets.append(shlex.split(tail))
    except ValueError:
        token_sets.append(tail.split())
    if "\\" in tail and (os.name == "nt" or re.search(r"(?:^|\s)\\[^\s]", tail)):
        try:
            token_sets.append(shlex.split(tail, posix=False))
        except ValueError:
            token_sets.append(tail.split())

    targets: list[str] = []
    seen: set[str] = set()
    for tokens in token_sets:
        for token in tokens:
            if not token or token.startswith("-") or token in seen:
                continue
            seen.add(token)
            targets.append(token)
    return targets


def _extract_intents(
    command: str,
    *,
    base_dir: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Return every recognized destructive intent, deduped and normalized.

    ``rm /a /b /c`` -> three tuples; ``shutil.rmtree('a'); os.remove('b')`` ->
    two tuples; a plain echo returns an empty list.
    """
    if not command:
        return []
    paths: list[str] = []
    paths.extend(_extract_rm_targets(command))
    for pattern in _PY_DELETE_PATTERNS:
        paths.extend(m.group(1) for m in pattern.finditer(command))

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in paths:
        intent = ("delete", _norm_path(raw, base_dir=base_dir))
        if intent in seen:
            continue
        seen.add(intent)
        result.append(intent)
    return result


def _extract_intent(command: str) -> tuple[str, str] | None:
    """First extracted intent, or None. Convenience for single-target callers."""
    intents = _extract_intents(command)
    return intents[0] if intents else None
