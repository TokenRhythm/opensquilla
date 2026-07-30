from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PYTEST_STATE_ROOT = Path(tempfile.gettempdir()) / f"opensquilla-pytest-{os.getpid()}"

os.environ.setdefault("OPENSQUILLA_STATE_DIR", str(_PYTEST_STATE_ROOT / "state"))
os.environ.setdefault("OPENSQUILLA_LOG_DIR", str(_PYTEST_STATE_ROOT / "logs"))
os.environ.setdefault("OPENSQUILLA_TURN_CALL_LOG", "0")
os.environ.setdefault("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
os.environ.setdefault(
    "OPENSQUILLA_USER_STATE_DIR",
    str(_PYTEST_STATE_ROOT / "profile-lock-state"),
)

_PROVIDER_ENV_KEYS = (
    "AIHUBMIX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ARK_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "BAILIAN_API_KEY",
    "BOCHA_SEARCH_API_KEY",
    "BRAVE_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "BYTEPLUS_API_KEY",
    "CUSTOM_LLM_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "IQS_SEARCH_API_KEY",
    "KIMI_CODING_API_KEY",
    "LITELLM_API_KEY",
    "MIMO_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MINIMAX_CODING_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "QIANFAN_API_KEY",
    "SILICONFLOW_API_KEY",
    "TAVILY_API_KEY",
    "TENCENT_TOKEN_PLAN_API_KEY",
    "TENCENT_TOKENHUB_API_KEY",
    "TENCENT_TOKENHUB_INTL_API_KEY",
    "TOKENRHYTHM_API_KEY",
    "VOLCENGINE_API_KEY",
    "VOLC_ARK_API_KEY",
    "ZAI_API_KEY",
)

_LIVE_MARKERS = (
    "llm",
    "llm_smoke",
    "llm_costly",
    "llm_tools",
    "llm_embedding",
    "llm_reasoning",
    "llm_gateway",
    "llm_image",
    "llm_router_acc",
    "live_channel",
    "live_search",
)


@pytest.fixture(autouse=True)
def _isolate_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep default tests offline even when local env files have API keys."""
    if any(request.node.get_closest_marker(marker) for marker in _LIVE_MARKERS):
        return
    for env_key in _PROVIDER_ENV_KEYS:
        # An explicit empty value prevents build_services.load_env() from
        # rehydrating credentials from a repository or profile .env file.
        monkeypatch.setenv(env_key, "")


@pytest.fixture(autouse=True)
def _reset_channels_reconciler_singleton():
    """Clear the channels-reconcile bridge between tests.

    Gateway boot registers a live reconciler in a module-level singleton;
    without this reset, a boot-running test leaks its closure into later
    channel CRUD tests, which then reconcile against a foreign gateway.
    """
    from opensquilla.gateway.channels_bridge import reset_channels_reconciler

    reset_channels_reconciler()
    yield
    reset_channels_reconciler()


@pytest.fixture(autouse=True)
def _undo_leaked_cli_structlog_default():
    """Revert the CLI structlog default when a test leaves it behind.

    The CLI entry callback installs a process-wide structlog default (stderr
    output, WARNING+ filter; ``observability/cli_logging.py``). Tests that
    invoke the Typer app would otherwise leak that filter into later tests
    that capture info-level structlog events. Only the CLI default is
    reverted; any other configuration a test installs is left for that test's
    own teardown.
    """
    import structlog

    from opensquilla.observability.cli_logging import is_cli_default_active

    was_configured = structlog.is_configured()
    old_config = structlog.get_config()
    was_cli_default = is_cli_default_active()
    yield
    if is_cli_default_active() and not was_cli_default:
        if was_configured:
            structlog.configure(**old_config)
        else:
            structlog.reset_defaults()


@pytest.fixture(scope="session")
def isolated_core_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the core wheel from an isolated source tree with a synthetic UI.

    A source checkout intentionally has no generated Vue ``dist`` tree, while
    standard wheel builds fail closed without a verified artifact. Packaging
    contract tests share this minimal artifact so they continue to test the
    real Hatch wheel selection without requiring a frontend build or mutating
    the checkout under test.
    """

    import hashlib
    import json
    import shutil
    import subprocess

    from scripts.verify_webui_artifact import MANIFEST_NAME, source_fingerprint

    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    temp_root = tmp_path_factory.mktemp("isolated-core-wheel")
    build_root = temp_root / "source"
    build_root.mkdir()
    def ignored(source: str, names: list[str]) -> set[str]:
        source_path = Path(source).resolve()
        generated = {
            name
            for name in names
            if name in {"node_modules", "coverage", "test-results", "__pycache__"}
            or name.endswith(".pyc")
        }
        if source_path == (_REPO_ROOT / "src/opensquilla/gateway/static").resolve():
            generated.add("dist")
        if source_path == (_REPO_ROOT / "opensquilla-webui").resolve():
            generated.add("dist")
        return generated

    for directory in ("src", "migrations", "opensquilla-webui", "scripts"):
        shutil.copytree(_REPO_ROOT / directory, build_root / directory, ignore=ignored)
    for filename in (
        ".gitignore",
        "LICENSE",
        "README.md",
        "hatch_build.py",
        "pyproject.toml",
    ):
        shutil.copy2(_REPO_ROOT / filename, build_root / filename)

    dist = build_root / "src" / "opensquilla" / "gateway" / "static" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "packaging-probe.js").write_bytes(b"window.__opensquillaPackagingProbe = true;\n")
    (assets / "packaging-probe.css").write_bytes(b"body{}\n")
    (dist / "index.html").write_text(
        """<!doctype html>
<link rel="stylesheet" href="./assets/packaging-probe.css">
<script type="module" src="./assets/packaging-probe.js"></script>
""",
        encoding="utf-8",
    )

    records = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        content = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schemaVersion": 1,
        "sourceFingerprint": source_fingerprint(build_root / "opensquilla-webui"),
        "files": records,
    }
    (dist / MANIFEST_NAME).write_text(
        f"{json.dumps(manifest, indent=2)}\n",
        encoding="utf-8",
    )

    wheel_dir = temp_root / "wheel"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(wheel_dir)],
        cwd=build_root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"uv build failed: {result.stderr}"
    wheels = list(wheel_dir.glob("opensquilla-*.whl"))
    assert len(wheels) == 1, f"Expected 1 wheel, got {wheels}"
    return wheels[0]
