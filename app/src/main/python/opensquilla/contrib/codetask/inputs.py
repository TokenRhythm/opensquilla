"""Task input layer: normalize every entry point into one ``task.md``.

Entry points (mutually exclusive, exactly one required):
  - issue_number  -> ``gh issue view`` (title + body + comments)
  - task_text     -> inline free-form request
  - task_file     -> file holding a long request / non-GitHub platform export

The product is a single ``TaskSpec`` plus a rendered ``task.md`` with
frontmatter, so the rest of the pipeline never knows where the task came
from ("narrow waist").
"""

from __future__ import annotations

import json
import locale
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

GH_PREFLIGHT_HINT = (
    "GitHub issue mode needs the GitHub CLI. Install `gh` and run `gh auth login`, "
    'or pass the issue text another way: --task-file <path> or --task "<text>".'
)

# Cap on how much of the issue thread we inline before truncating comments.
MAX_TASK_CHARS = 24000


class InputError(RuntimeError):
    """Task input could not be resolved (bad args, gh missing, etc.)."""


@dataclass
class TaskSpec:
    """Normalized task description, independent of its source."""

    source: str  # github-issue | inline | file
    title: str
    body: str
    slug: str
    url: str = ""
    comments: list[str] = field(default_factory=list)
    truncated: bool = False


def gh_available() -> bool:
    """True if the GitHub CLI binary is on PATH."""
    return shutil.which("gh") is not None


def resolve_task(
    *,
    issue_number: int | None = None,
    task_text: str | None = None,
    task_file: str | None = None,
    repo_dir: Path | None = None,
    repo_slug_hint: str = "",
) -> TaskSpec:
    """Resolve exactly one task entry point into a TaskSpec.

    ``repo_dir`` (a local clone) lets ``gh`` resolve the issue without an
    explicit ``--repo`` when the clone has a GitHub remote.
    """
    from opensquilla.contrib.codetask.config import slugify

    given = [x for x in (issue_number, task_text, task_file) if x not in (None, "")]
    if len(given) == 0:
        raise InputError("No task given: pass one of --issue, --task, or --task-file.")
    if len(given) > 1:
        raise InputError("Pass only one of --issue, --task, or --task-file.")

    if issue_number is not None:
        return _resolve_issue(issue_number, repo_dir)
    if task_file is not None:
        path = Path(task_file).expanduser()
        if not path.is_file():
            raise InputError(f"Task file not found: {path}")
        # User-supplied task files are arbitrary text. Try UTF-8 first (the
        # cross-platform default); fall back to the host locale with
        # replacement so a GBK/ANSI file from a Windows editor still loads
        # without crashing.
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            body = path.read_text(
                encoding=locale.getpreferredencoding(False) or "utf-8",
                errors="replace",
            )
        title = body.strip().splitlines()[0][:80] if body.strip() else "code task"
        return TaskSpec(source="file", title=title, body=body, slug=slugify(title))

    # inline text
    text = (task_text or "").strip()
    if not text:
        raise InputError("--task text is empty.")
    title = text.splitlines()[0][:80]
    return TaskSpec(source="inline", title=title, body=text, slug=slugify(title))


def _resolve_issue(issue_number: int, repo_dir: Path | None) -> TaskSpec:
    from opensquilla.contrib.codetask.config import slugify

    if not gh_available():
        raise InputError(GH_PREFLIGHT_HINT)

    cmd = ["gh", "issue", "view", str(issue_number), "--json", "title,body,comments,url"]
    try:
        # gh resolves the repo from cwd, so run inside the clone when given.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
    except FileNotFoundError as exc:
        raise InputError(GH_PREFLIGHT_HINT) from exc
    except subprocess.TimeoutExpired as exc:
        raise InputError(f"`gh issue view {issue_number}` timed out.") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "not logged" in stderr.lower() or "authentication" in stderr.lower():
            raise InputError(f"GitHub CLI not authenticated: run `gh auth login`. ({stderr})")
        raise InputError(f"`gh issue view {issue_number}` failed: {stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InputError(f"Could not parse gh output for issue {issue_number}.") from exc

    title = (data.get("title") or f"issue {issue_number}").strip()
    body = (data.get("body") or "").strip()
    comments = [
        f"{(c.get('author') or {}).get('login', 'user')}: {(c.get('body') or '').strip()}"
        for c in (data.get("comments") or [])
        if (c.get("body") or "").strip()
    ]
    return TaskSpec(
        source="github-issue",
        title=title,
        body=body,
        slug=slugify(title),
        url=data.get("url") or "",
        comments=comments,
    )


def render_task_md(spec: TaskSpec, *, repo: str, base_ref: str, commit: str) -> str:
    """Render the canonical task.md (frontmatter + body + comments).

    Truncates the inlined comment thread to MAX_TASK_CHARS, keeping the body
    whole; sets ``spec.truncated`` when it has to cut.
    """
    stamp = datetime.now(UTC).isoformat()
    header = (
        "---\n"
        f"source: {spec.source}\n"
        f"url: {spec.url}\n"
        f"fetched_at: {stamp}\n"
        f"repo: {repo} @ {commit} ({base_ref})\n"
        "---\n\n"
        f"# {spec.title}\n\n"
        f"{spec.body}\n"
    )
    if not spec.comments:
        return header

    budget = MAX_TASK_CHARS - len(header)
    parts: list[str] = []
    used = 0
    for c in spec.comments:
        block = f"\n---\n\n{c}\n"
        if used + len(block) > budget:
            spec.truncated = True
            break
        parts.append(block)
        used += len(block)
    comments_section = f"\n## Comments ({len(spec.comments)})\n" + "".join(parts)
    if spec.truncated:
        comments_section += "\n\n_(comment thread truncated)_\n"
    return header + comments_section
