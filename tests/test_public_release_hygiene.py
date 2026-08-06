from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from opensquilla.gateway.config import GatewayConfig

# Real secret-shape detectors. Each pattern matches a shipped vendor key
# format so an accidentally-committed credential is caught at CI time
# before the public tree is published.
SECRET_PATTERNS = {
    "openrouter": re.compile(r"sk-or-v1-[A-Za-z0-9_-]{32,}"),
    # Real TokenRhythm keys carry a 40+ char base62 tail; the floor keeps
    # short synthetic fixtures (sk_tr_FAKE...) legal.
    "tokenrhythm": re.compile(r"\bsk_tr_[A-Za-z0-9]{32,}\b"),
    "brave": re.compile(r"\bBSA[A-Za-z0-9_-]{20,}\b"),
    "github_pat": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
}

# Generic developer-machine path detectors. Matches any Windows
# ``C:\Users\<name>`` and any ``D:\<top-level>`` path so a hard-coded
# local checkout location cannot survive into the public tree, without
# embedding any specific contributor's username.
LOCAL_PATH_PATTERNS = {
    "windows_user_path": re.compile(r"C:\\Users\\[A-Za-z0-9_.-]+"),
    "windows_drive_d_repo": re.compile(r"\bD:\\[A-Za-z0-9_.-]+\\"),
    "linux_user_home_path": re.compile(r"/home/(?!node/)[A-Za-z0-9_.-]{2,}/"),
    "posix_user_home_path": re.compile(r"/Users/[A-Za-z0-9_.-]{2,}/"),
}

# Generic placeholder names that the wheelhouse build script's release
# denylist must reference. These names are intentionally generic so the
# public denylist reads as a forward-looking forbidden-content policy
# rather than a confession of historical artifact names.
PUBLIC_RELEASE_MARKER_NAMES = (
    "INTERNAL_RELEASE_NOTE.md",
    ".internal/evidence",
    "LOCAL_AGENT_NOTES.md",
)

# These tests intentionally contain fake local paths to exercise path-policy
# behavior. They are public fixtures, not leaked developer-machine paths.
PATH_POLICY_FIXTURE_FILES = {
    "tests/test_artifacts.py",
    "tests/test_migration/fixtures/homes/cli-0.1/config.toml",
    "tests/test_migration/fixtures/homes/cli-0.2/config.toml",
    "tests/test_migration/fixtures/homes/cli-0.3/config.toml",
    "tests/test_migration/fixtures/homes/cli-0.4/config.toml",
    "tests/test_migration/fixtures/homes/cli-0.5/config.toml",
    "tests/test_migration/fixtures/homes/desktop-0.4/config.toml",
    "tests/test_migration/fixtures/homes/desktop-0.5rc/config.toml",
    "tests/test_provider_image_generation.py",
    "tests/test_sandbox/test_operation_profile.py",
    "tests/test_sandbox/test_file_policy.py",
    "tests/test_sandbox/test_path_access.py",
    "tests/test_sandbox/test_permission_profiles.py",
    "tests/test_sandbox/test_platform_permissions.py",
    "tests/test_sandbox/test_windows_default_cache.py",
    "tests/test_tools/test_apply_patch_gates.py",
    "tests/test_tools/test_filesystem_read_workspace.py",
    "tests/test_tools/test_git_workdir_policy.py",
    "tests/test_tools/test_path_policy.py",
    "tests/test_tools/test_shell_sensitive.py",
    "tests/test_tools/test_web_http_request.py",
    "tests/test_observability/test_decision_log_contract.py",
    "opensquilla-webui/src/components/SupportDiagnosticsMenu.test.ts",
    "opensquilla-webui/src/composables/chat/useChatShareExport.test.ts",
    "opensquilla-webui/src/utils/chat/activityToolDetails.test.ts",
    "opensquilla-webui/src/utils/overviewDiagnostics.test.ts",
    "opensquilla-webui/src/views/OverviewView.diagnostics.test.ts",
}


def _tracked_text_files() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files"], text=True)
    paths: list[Path] = []
    for rel in raw.splitlines():
        path = Path(rel)
        if path.suffix.lower() in {
            ".bin",
            ".onnx",
            ".pkl",
            ".joblib",
            ".woff2",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".ico",
        }:
            continue
        if not path.exists():
            continue
        paths.append(path)
    return paths


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_tracked_public_files_do_not_contain_real_secret_shapes_or_local_paths() -> None:
    violations: list[str] = []
    self_path = Path(__file__).relative_to(Path.cwd()).as_posix()
    for path in _tracked_text_files():
        # Skip this file so the regexes themselves do not trigger the test.
        if path.as_posix() == self_path:
            continue
        if path.as_posix() in PATH_POLICY_FIXTURE_FILES:
            continue
        text = _read_text(path)
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{path}: {name}")
        for name, pattern in LOCAL_PATH_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{path}: {name}")

    assert violations == []


def test_release_text_marker_policy_uses_public_internal_fixture_names() -> None:
    script = Path("scripts/build_wheelhouse_zip.py").read_text(encoding="utf-8")

    for marker in PUBLIC_RELEASE_MARKER_NAMES:
        assert marker in script


def test_private_test_suites_are_not_tracked_in_public_repository() -> None:
    tracked = {path.as_posix() for path in _tracked_text_files()}

    assert not any(path.startswith("tests/_private/") for path in tracked)
    assert not any(path.startswith(".omx/private-golden/") for path in tracked)


def test_gitignore_keeps_private_live_artifacts_out_of_public_tree() -> None:
    ignore_text = Path(".gitignore").read_text(encoding="utf-8")

    assert "/tests/_private/" in ignore_text
    assert "tests/functional/reports/" in ignore_text
    assert ".omx/" in ignore_text


def test_pytest_default_collection_excludes_private_agent_artifacts() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = config["tool"]["pytest"]["ini_options"]
    excluded = set(pytest_options.get("norecursedirs", []))

    assert {"tests/_private", "tests/fixtures", ".omx", ".codex", ".claude"} <= excluded


def test_public_testing_guidance_documents_the_private_boundary() -> None:
    required_docs = [
        Path("CONTRIBUTING.md"),
        Path(".github/pull_request_template.md"),
    ]

    for path in required_docs:
        assert path.is_file(), f"missing testing guidance: {path}"

    combined = "\n".join(path.read_text(encoding="utf-8") for path in required_docs)
    required_phrases = [
        "offline",
        "credential-free",
        "tests/_private/",
        "must not be committed",
        "Live Release E2E",
    ]

    for phrase in required_phrases:
        assert phrase in combined


def test_pull_request_template_uses_structured_governance_fields() -> None:
    text = Path(".github/pull_request_template.md").read_text(encoding="utf-8")
    required_phrases = [
        "Scope boundary",
        "Non-goals",
        "Base branch: main | staging/collaboration",
        ("Target exception: N/A | release | hotfix | staging/collaboration | maintainer-approved"),
        "Linked issue: Fixes #... | Refs #... | None",
        "If None, reason",
        "Release note: NONE |",
        "Ruff:",
        "Pytest:",
        "Build:",
        "Maintainer live check",
        "contributors are not expected to provide secrets",
        "Third-party origin",
    ]

    for phrase in required_phrases:
        assert phrase in text

    assert text.count("[ ]") <= 6


def test_public_docs_do_not_use_key_shaped_placeholders() -> None:
    violations: list[str] = []
    placeholder_pattern = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+")
    for path in [Path("README.md"), Path("CONTRIBUTING.md")]:
        text = _read_text(path)
        if placeholder_pattern.search(text):
            violations.append(path.as_posix())

    assert violations == []


def test_release_sop_documents_github_only_validation_boundary() -> None:
    text = Path("RELEASES.md").read_text(encoding="utf-8")

    required_phrases = [
        "GitHub-only release checks",
        "0.5.x releases (previews and the stable) publish Electron desktop installers",
        "must not be marked as Latest",
        "do not publish Windows portable zips",
        "portable latest aliases",
        "filenames must remain versioned",
        "SHA256SUMS",
        "post-publish tag URL checks",
        "curl --fail --head --location",
        "public wheelhouse zips, or separately branded macOS or Linux portable bundles",
        "Mark-of-the-Web",
        "SmartScreen",
        "Smart App Control",
    ]

    for phrase in required_phrases:
        assert phrase in text

    assert ".sha256" not in text


def test_readme_documents_quick_and_manual_terminal_install_commands() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "uv tool install --python 3.12" in text
    assert '"opensquilla[recommended] @ https://github.com' in text
    assert "https://opensquilla.ai/install.sh" not in text
    assert "https://opensquilla.ai/install.ps1" not in text
    assert "Starting with Preview 5" not in text
    assert "predates the packaged TUI companion" not in text
    assert "opensquilla_tui_host-0.5.0rc4" not in text
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in text
    assert '. "$HOME/.local/bin/env"' in text
    assert 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' in text
    assert "$env:Path" in text
    assert "bash scripts/install_source.sh" in text
    assert "./scripts/install_source.ps1" in text


def test_readme_uses_gateway_default_port() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    expected_port = GatewayConfig.model_fields["port"].default

    assert f"127.0.0.1:{expected_port}" in text
    assert "18790" not in text


def test_tracked_sources_do_not_keep_agent_revision_markers() -> None:
    forbidden_markers = ("codex-revised", "claude-revised")
    violations: list[str] = []
    self_path = Path(__file__).relative_to(Path.cwd()).as_posix()
    for path in _tracked_text_files():
        if path.as_posix() == self_path:
            continue
        text = _read_text(path).lower()
        for marker in forbidden_markers:
            if marker in text:
                violations.append(f"{path}: {marker}")

    assert violations == []


def test_gitignore_does_not_hide_tracked_release_files() -> None:
    raw = subprocess.check_output(
        ["git", "ls-files", "-c", "-i", "--exclude-standard"],
        text=True,
    )
    ignored_tracked = [line for line in raw.splitlines() if line.strip()]

    assert ignored_tracked == []
