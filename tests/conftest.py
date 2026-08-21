from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

# Skill manifests fingerprint complete bundled trees.  Test imports can create
# derived ``__pycache__`` files between the loader's two integrity scans, so
# disable bytecode writes for the controller, xdist workers, and child tests.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

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

_XDIST_SCOPE_ENV = "OPENSQUILLA_PYTEST_XDIST_SCOPE"
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _safe_xdist_component(value: str) -> str:
    """Return a path-safe, stable xdist run or worker identifier."""

    safe = "".join(
        character if character.isalnum() or character in "-._" else "_"
        for character in value
    )
    component = safe[:80].rstrip(".") or "unknown"
    windows_stem = component.split(".", 1)[0].upper()
    if windows_stem in _WINDOWS_RESERVED_COMPONENTS:
        component = f"_{component}"
    return component


def _xdist_runtime_roots(
    *,
    state_root: Path,
    log_root: Path,
    user_state_root: Path,
    run_uid: str,
    worker_id: str,
) -> dict[str, Path]:
    """Derive disjoint runtime roots for one xdist worker."""

    relative_scope = (
        Path(".pytest-xdist")
        / _safe_xdist_component(run_uid)
        / _safe_xdist_component(worker_id)
    )
    return {
        "OPENSQUILLA_STATE_DIR": state_root / relative_scope,
        "OPENSQUILLA_LOG_DIR": log_root / relative_scope,
        "OPENSQUILLA_USER_STATE_DIR": user_state_root / relative_scope,
    }


def pytest_configure(config: pytest.Config) -> None:
    """Move shared runtime directories below a worker-specific xdist scope."""

    worker_input = getattr(config, "workerinput", None)
    if not isinstance(worker_input, dict):
        return
    worker_id = str(
        worker_input.get("workerid")
        or os.environ.get("PYTEST_XDIST_WORKER")
        or "worker"
    )
    run_uid = str(
        worker_input.get("testrunuid")
        or os.environ.get("PYTEST_XDIST_TESTRUNUID")
        or "session"
    )
    scope = f"{_safe_xdist_component(run_uid)}/{_safe_xdist_component(worker_id)}"
    if os.environ.get(_XDIST_SCOPE_ENV) == scope:
        return

    roots = _xdist_runtime_roots(
        state_root=Path(os.environ["OPENSQUILLA_STATE_DIR"]),
        log_root=Path(os.environ["OPENSQUILLA_LOG_DIR"]),
        user_state_root=Path(os.environ["OPENSQUILLA_USER_STATE_DIR"]),
        run_uid=run_uid,
        worker_id=worker_id,
    )
    for env_key, path in roots.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[env_key] = str(path)
    os.environ[_XDIST_SCOPE_ENV] = scope

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


@pytest.fixture
def unavailable_git_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Force Git unavailable and fail if a flow still tries to launch it."""
    from opensquilla import git_runtime
    from opensquilla.git_runtime import GitCapability, GitCapabilityState

    capability = GitCapability(
        state=GitCapabilityState.UNAVAILABLE,
        executable=None,
        source=None,
        reason="git_not_found",
    )
    resolution_calls: list[dict[str, object]] = []

    def resolve_unavailable(
        environment=None,
        run_mode=None,
        force_refresh: bool = False,
    ) -> GitCapability:
        resolution_calls.append(
            {
                "environment": environment,
                "run_mode": run_mode,
                "force_refresh": force_refresh,
            }
        )
        return capability

    original_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        raw_program = command[0] if isinstance(command, (list, tuple)) else command
        program = os.fsdecode(raw_program).strip().strip("\"'")
        program = program.replace("\\", "/").rsplit("/", 1)[-1]
        program = program.split(maxsplit=1)[0].casefold()
        if program in {"git", "git.exe", "xcode-select", "xcode-select.exe"}:
            raise AssertionError(f"unexpected Git process launch: {command!r}")
        return original_run(command, *args, **kwargs)

    git_runtime.clear_git_capability_cache()
    monkeypatch.setattr(git_runtime, "resolve_git_capability", resolve_unavailable)
    monkeypatch.setattr(subprocess, "run", guarded_run)
    return SimpleNamespace(
        capability=capability,
        resolution_calls=resolution_calls,
    )


@pytest.fixture(autouse=True)
def _isolate_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep default tests offline despite local credentials or live-pricing settings."""
    if any(request.node.get_closest_marker(marker) for marker in _LIVE_MARKERS):
        return
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
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
def _migrated_db_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Build one pristine latest-schema SQLite database per pytest session.

    This fixture is only for ordinary tests that require the current schema.
    Migration, rollback, schema-ahead, audit, and lock tests must continue to
    create their databases from scratch and call the migrator explicitly.
    """
    import hashlib
    import sqlite3

    from opensquilla.persistence.migrator import apply_pending

    template = tmp_path_factory.mktemp("migrated-db-template") / "template.db"
    applied = apply_pending(str(template), _REPO_ROOT / "migrations")
    assert applied, "fresh migrated test template did not apply any migrations"

    connection = sqlite3.connect(template)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        connection.close()

    sidecars = tuple(Path(f"{template}{suffix}") for suffix in ("-wal", "-shm"))
    assert not any(path.exists() for path in sidecars)
    pristine_digest = hashlib.sha256(template.read_bytes()).digest()

    yield template

    assert hashlib.sha256(template.read_bytes()).digest() == pristine_digest
    assert not any(path.exists() for path in sidecars)


@pytest.fixture
def migrated_db_factory(
    tmp_path: Path,
    _migrated_db_template: Path,
) -> Callable[[str | None], Path]:
    """Return a factory that copies the pristine schema into isolated test DBs."""
    import itertools
    import shutil

    sequence = itertools.count()

    def copy_template(filename: str | None = None) -> Path:
        destination = tmp_path / (filename or f"test-{next(sequence)}.db")
        shutil.copyfile(_migrated_db_template, destination)
        return destination

    return copy_template


@pytest.fixture
def migrated_db(migrated_db_factory: Callable[[str | None], Path]) -> Path:
    """Return an isolated latest-schema database for one test."""
    return migrated_db_factory(None)


_PREBUILT_CORE_WHEEL_ENV = "OPENSQUILLA_TEST_CORE_WHEEL"
_PREBUILT_CORE_WHEEL_SHA_ENV = "OPENSQUILLA_TEST_CORE_WHEEL_SHA256"


def _prebuilt_core_wheel_from_environment() -> Path | None:
    """Return and content-verify the controller-built wheel, when provided."""

    import hashlib

    raw_path = os.environ.get(_PREBUILT_CORE_WHEEL_ENV)
    if not raw_path:
        return None
    wheel = Path(raw_path).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise AssertionError(f"invalid prebuilt core wheel: {wheel}")

    expected_digest = os.environ.get(_PREBUILT_CORE_WHEEL_SHA_ENV)
    if not expected_digest:
        raise AssertionError("prebuilt core wheel is missing its SHA-256 contract")
    actual_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        raise AssertionError(
            "prebuilt core wheel SHA-256 mismatch "
            f"(expected {expected_digest}, got {actual_digest})"
        )
    return wheel


@pytest.fixture(scope="session")
def isolated_core_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Reuse the CI controller wheel or build one local session fallback."""

    import shutil

    prebuilt = _prebuilt_core_wheel_from_environment()
    if prebuilt is not None:
        return prebuilt
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    from scripts.build_test_core_wheel import build_isolated_core_wheel

    temp_root = tmp_path_factory.mktemp("isolated-core-wheel")
    return build_isolated_core_wheel(_REPO_ROOT, temp_root)
