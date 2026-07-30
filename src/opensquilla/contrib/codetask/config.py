"""Configuration and path helpers for the code-task harness.

Host mode only in v1. Paths default under the OpenSquilla home and are
overridable with ``OPENSQUILLA_CODETASK_*`` environment variables.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from opensquilla.paths import default_opensquilla_home

# ---------------------------------------------------------------------------
# Agent defaults (shared profile with the swebench mode's tuned values)
# ---------------------------------------------------------------------------
DEFAULT_MODEL = ""  # empty = router / config decides
DEFAULT_THINKING = ""
DEFAULT_AGENT_TIMEOUT = 5400  # seconds (90 min); heavy repos spend ~15 min on clone+install
DEFAULT_MAX_ITERATIONS = 300
DEFAULT_ITERATION_TIMEOUT = 600
DEFAULT_MAX_PROVIDER_RETRIES = 5

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
# Filename the agent writes its machine-readable acceptance manifest to,
# relative to the scratch dir (NOT the repo — must not pollute the diff).
VERIFICATION_MANIFEST_NAME = "verification.json"
DEFAULT_ACCEPTANCE_TIMEOUT = 600  # seconds per acceptance command
DEFAULT_REGRESSION_TIMEOUT = 1800  # seconds for the existing suite

# ---------------------------------------------------------------------------
# Git identity for task-branch commits
# ---------------------------------------------------------------------------
GIT_USER_EMAIL = "opensquilla-codetask@local"
GIT_USER_NAME = "OpenSquilla Code-Task"
TASK_BRANCH_PREFIX = "task/"

# Universal build/cache artifacts to keep OUT of the collected change. These
# are written to the clone's .git/info/exclude (repo-local, leaves no tracked
# .gitignore) so dependency install / test runs do not pollute the diff or PR.
# Only never-legitimate junk — real source edits (incl. pyproject.toml) are
# untouched.
BUILD_ARTIFACT_EXCLUDES = [
    # Unambiguous junk anywhere in the tree (safe to leave unanchored).
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.egg-info/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "*.so",
    "*.o",
    "*.class",
    "node_modules/",
    ".coverage",
    # Common build-output dir names that a project might legitimately use as
    # a source path at depth (e.g. src/build/). Anchor to the repo root so we
    # only ignore the top-level build outputs, never new source files
    # (codex review: unanchored patterns dropped legitimate src/build files).
    "/target/",
    "/dist/",
    "/out/",
    "/build/",
    "!*/build/",
    "!*/build/**",
    "/htmlcov/",
]

# Package data shipped with the wheel (prompt template).
_DATA_DIR = Path(__file__).resolve().parent / "data"


def runs_root() -> Path:
    """Root dir holding per-run working trees and artifacts."""
    override = os.environ.get("OPENSQUILLA_CODETASK_RUNS_DIR")
    if override:
        return Path(override).expanduser()
    return default_opensquilla_home() / "code-task"


def build_workspace_dir() -> Path:
    """Durable home for from-scratch app builds (verification_mode=build, no repo).

    The verified app persists here so a follow-up edit can ``--repo <path>`` it.
    Overridable with ``OPENSQUILLA_CODETASK_WORKSPACE_DIR``.
    """
    override = os.environ.get("OPENSQUILLA_CODETASK_WORKSPACE_DIR")
    if override:
        return Path(override).expanduser()
    return default_opensquilla_home() / "workspace"


def run_dir(run_id: str) -> Path:
    """Per-run dir: <runs_root>/<run_id>/."""
    return runs_root() / run_id


def repo_dir(run_id: str) -> Path:
    """Where the target repo is cloned for a run."""
    return run_dir(run_id) / "repo"


def scratch_dir(run_id: str) -> Path:
    """Agent scratch dir (reproducers, manifest, debug) — outside the repo.

    MUST live somewhere the agent's sandbox can write. The default runs_root
    is under the OpenSquilla home (e.g. /root/.opensquilla when running as
    root), and the agent's sensitive-path guard HARD-BLOCKS writes under
    /root — so the scratch dir is placed under the system temp dir instead,
    which is sandbox-writable. The (non-sandboxed) runner copies the manifest
    back into the run dir for the permanent record.
    """
    override = os.environ.get("OPENSQUILLA_CODETASK_SCRATCH_DIR")
    if override:
        return Path(override).expanduser() / run_id / "scratch"
    return Path(tempfile.gettempdir()) / "opensquilla-codetask" / run_id / "scratch"


def artifact_path(run_id: str, name: str) -> Path:
    """A named artifact under the run dir (task.md, result.json, logs...)."""
    return run_dir(run_id) / name


def prompt_template_path(verification_mode: str = "red-green", is_edit: bool = False) -> Path:
    """Prompt template rendered for each task.

    Build mode (app/from-scratch generation) uses a build-oriented template;
    the default is the red->green->regression template.
    """
    override = os.environ.get("OPENSQUILLA_CODETASK_PROMPT_TEMPLATE")
    if override:
        return Path(override).expanduser()
    if verification_mode == "build":
        name = "app_edit.txt" if is_edit else "app_build.txt"
    elif verification_mode == "scratch":
        name = "scratch.txt"
    else:
        name = "default.txt"
    return _DATA_DIR / "prompts" / name


AGENT_CONFIG_OVERRIDE_ENV = "OPENSQUILLA_CODETASK_AGENT_CONFIG"


def agent_config_override() -> Path | None:
    """The operator's fully-authoritative agent-config override, if set.

    A single decoder of the ``OPENSQUILLA_CODETASK_AGENT_CONFIG`` contract so
    the escape hatch and the inheritance gate never disagree — a blank value
    (e.g. a quoted empty shell export) is treated as unset, not as a path.
    """
    raw = os.environ.get(AGENT_CONFIG_OVERRIDE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def agent_config_path() -> Path:
    """Run-policy template the per-run subagent config is assembled from.

    Gives the agent a focused tool set — coding-irrelevant tools (memory,
    sessions, sub-agents, cron, messaging, image/media) are denied, but
    network stays ON (code-task legitimately fetches docs and installs deps).
    Provider/model/router settings are NOT pinned here: agent_config.py
    carries the operator's own provider sections into the per-run config.
    When OPENSQUILLA_CODETASK_AGENT_CONFIG points at a custom config, that
    file is fully authoritative and no provider inheritance is applied.
    """
    return agent_config_override() or (_DATA_DIR / "agent_config" / "config.toml")


def agent_python() -> str:
    """Interpreter used to launch the host agent subprocess.

    Defaults to the interpreter running the runner so the subprocess always
    matches the venv that has opensquilla installed; override with
    OPENSQUILLA_CODETASK_AGENT_PYTHON for unusual installs.
    """
    return os.environ.get("OPENSQUILLA_CODETASK_AGENT_PYTHON", sys.executable)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    """Make a filesystem/branch-safe slug from free text."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "task"
