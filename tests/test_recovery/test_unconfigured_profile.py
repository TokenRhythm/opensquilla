from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli.recovery_cmd import recovery_app
from opensquilla.recovery import inspect_profile
from opensquilla.recovery.errors import RecoveryError
from opensquilla.recovery.locking import ProfileOperationLock
from opensquilla.recovery.unconfigured_profile import initialize_unconfigured_profile

SEED = '''[llm]
provider = ""
model = ""
api_key = ""
api_key_env = ""
base_url = ""
[squilla_router]
enabled = false
[llm_ensemble]
enabled = false
[control_ui]
enabled = true
base_path = "/control"
default_locale = "zh-CN"
[sandbox]
run_mode = "full"
'''

INITIAL_CONSENT = {
    "schema_version": 1,
    "reliability": {
        "enabled": None, "notice_version": None,
        "consented_at_utc": None, "forced_off": True,
    },
    "growth": {
        "enabled": None, "notice_version": None,
        "consented_at_utc": None, "forced_off": True,
    },
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    for name in ("OPENSQUILLA_STATE_DIR", "OPENSQUILLA_WORKSPACE", "OPENSQUILLA_WORKSPACE_DIR"):
        monkeypatch.delenv(name, raising=False)
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    return user_data / "opensquilla"


def initialize(home: Path, **kwargs):
    report = inspect_profile(home)
    return initialize_unconfigured_profile(
        home, transaction_id=report.transaction_id,
        expected_revision=report.revision, payload={"config": SEED}, **kwargs,
    )


def test_fresh_profile_bootstraps_without_credentials(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-inherited-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-inherited-secret")
    assert not home.exists()
    result = initialize(home)
    assert result.outcome == "ready"
    assert result.stable_code == "unconfigured_profile_initialized"
    assert (home / "config.toml").read_bytes() == SEED.encode()
    assert (home / "config.toml").stat().st_nlink == 1
    assert (home / "workspace").is_dir()
    assert (home / "state").is_dir()
    assert not (home.parent / "desktop-credential.json").exists()
    assert not list(home.parent.glob("*.tmp"))
    from opensquilla.gateway.config import GatewayConfig

    loaded = GatewayConfig.load(home / "config.toml", read_only=True)
    assert loaded.llm.provider == ""
    assert loaded.llm.model == ""
    assert loaded.llm.api_key == ""
    assert loaded.llm.api_key_env == ""
    assert loaded.squilla_router.enabled is False
    assert loaded.llm_ensemble.enabled is False


def test_existing_config_is_retained_even_after_stale_preflight(home: Path) -> None:
    before = inspect_profile(home)
    initialize(home)
    config = home / "config.toml"
    original = b'[llm]\nprovider = "ollama"\nmodel = "my-local-model"\n'
    config.write_bytes(original)
    metadata = config.stat()
    initialize_unconfigured_profile(
        home, transaction_id=before.transaction_id,
        expected_revision=before.revision, payload={"config": SEED},
    )
    assert config.read_bytes() == original
    assert config.stat().st_mtime_ns == metadata.st_mtime_ns
    assert not (home.parent / "desktop-credential.json").exists()


def test_rejects_provider_or_external_paths_without_creating_home(home: Path) -> None:
    before = inspect_profile(home)
    for candidate in (
        SEED.replace('provider = ""', 'provider = "openai"'),
        'state_dir = "/elsewhere"\n' + SEED,
        SEED.replace('api_key = ""', 'api_key = "synthetic-secret"'),
    ):
        with pytest.raises(RecoveryError, match="must not configure"):
            initialize_unconfigured_profile(
                home, transaction_id=before.transaction_id,
                expected_revision=before.revision, payload={"config": candidate},
            )
    assert not home.exists()


def test_existing_credentials_are_never_replaced_or_used(home: Path) -> None:
    credential = home.parent / "desktop-credential.json"
    credential.write_bytes(b'{"provider":"openai","encryptedApiKey":"synthetic"}')
    with pytest.raises(RecoveryError) as raised:
        initialize(home)
    assert raised.value.stable_code == "profile_not_fresh"
    assert not home.exists()
    assert credential.read_bytes() == b'{"provider":"openai","encryptedApiKey":"synthetic"}'


@pytest.mark.parametrize("phase", ["prepared", "published"])
def test_interrupted_initialization_can_be_retried(home: Path, phase: str) -> None:
    def interrupt(current: str) -> None:
        if current == phase:
            raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        initialize(home, _failpoint=interrupt)
    result = initialize(home)
    assert result.outcome == "ready"
    assert (home / "config.toml").read_bytes() == SEED.encode()
    assert (home / "config.toml").stat().st_nlink == 1
    assert not (home.parent / "desktop-credential.json").exists()


def test_competing_config_publication_is_not_overwritten(home: Path, monkeypatch) -> None:
    from opensquilla.recovery import unconfigured_profile

    move = unconfigured_profile._durable_move_no_replace
    competing = b'[llm]\nprovider = "ollama"\nmodel = "mine"\n'

    def race(source: Path, destination: Path) -> None:
        destination.write_bytes(competing)
        move(source, destination)

    monkeypatch.setattr(unconfigured_profile, "_durable_move_no_replace", race)
    initialize(home)
    assert (home / "config.toml").read_bytes() == competing
    assert not list(home.parent.glob("*.tmp"))


def test_symlinked_config_is_not_followed(home: Path, tmp_path: Path) -> None:
    home.mkdir()
    target = tmp_path / "external-config"
    target.write_bytes(b"external bytes")
    try:
        (home / "config.toml").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(RecoveryError):
        initialize(home)
    assert target.read_bytes() == b"external bytes"


def test_cli_reads_stdin_and_returns_recovery_protocol(home: Path) -> None:
    before = inspect_profile(home)
    result = CliRunner().invoke(recovery_app, [
        "initialize-unconfigured", "--home", str(home),
        "--transaction-id", before.transaction_id,
        "--expected-revision", str(before.revision), "--json",
    ], input=json.dumps({"config": SEED}))
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["stable_code"] == "unconfigured_profile_initialized"
    assert report["outcome"] == "ready"
    assert not (home.parent / "desktop-credential.json").exists()


def test_initialization_respects_another_profile_writer(home: Path) -> None:
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with ProfileOperationLock(home):
            acquired.set()
            release.wait(5)

    writer = threading.Thread(target=hold_lock)
    writer.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(RecoveryError) as raised:
            initialize(home)
        assert raised.value.stable_code == "profile_lock_busy"
        assert not home.exists()
    finally:
        release.set()
        writer.join(5)


def test_changed_profile_refuses_stale_initialization(home: Path) -> None:
    before = inspect_profile(home)
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "SOUL.md").write_text("existing identity\n", encoding="utf-8")
    with pytest.raises(RecoveryError) as raised:
        initialize_unconfigured_profile(
            home, transaction_id=before.transaction_id,
            expected_revision=before.revision, payload={"config": SEED},
        )
    assert raised.value.stable_code == "stale_recovery_transaction"
    assert not (home / "config.toml").exists()


def test_electron_preboot_consent_mirror_does_not_block_initialization(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # openOrResumeDesktopApp synchronizes this mirror after inspection, before
    # prepareDesktopStartupConnection asks to initialize the empty config.
    mirror = home / "state" / "telemetry" / "desktop-consent-mirror.json"
    mirror.parent.mkdir(parents=True)
    content = (json.dumps(INITIAL_CONSENT, separators=(",", ":")) + "\n").encode()
    mirror.write_bytes(content)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    assert inspect_profile(home).stable_code == "effective_workspace_missing"

    result = initialize(home)

    assert result.outcome == "ready"
    assert result.stable_code == "unconfigured_profile_initialized"
    assert (home / "config.toml").read_bytes() == SEED.encode()
    assert mirror.read_bytes() == content
    assert not (home.parent / "desktop-credential.json").exists()


@pytest.mark.parametrize("artifact", ["sessions.db", "telemetry/unknown.json"])
def test_unrecognized_startup_data_is_not_treated_as_fresh(home: Path, artifact: str) -> None:
    path = home / "state" / artifact
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing data")
    with pytest.raises(RecoveryError):
        initialize(home)
    assert path.read_bytes() == b"existing data"
    assert not (home / "config.toml").exists()


def test_existing_consent_grant_is_not_treated_as_first_boot(home: Path) -> None:
    mirror = home / "state" / "telemetry" / "desktop-consent-mirror.json"
    mirror.parent.mkdir(parents=True)
    previous = {**INITIAL_CONSENT, "growth": {**INITIAL_CONSENT["growth"], "enabled": True}}
    mirror.write_text(json.dumps(previous), encoding="utf-8")
    with pytest.raises(RecoveryError):
        initialize(home)
    assert json.loads(mirror.read_text()) == previous
    assert not (home / "config.toml").exists()


def test_preboot_mirror_symlink_is_not_followed(home: Path, tmp_path: Path) -> None:
    mirror = home / "state" / "telemetry" / "desktop-consent-mirror.json"
    mirror.parent.mkdir(parents=True)
    target = tmp_path / "mirror.json"
    target.write_text(json.dumps(INITIAL_CONSENT), encoding="utf-8")
    try:
        mirror.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(RecoveryError):
        initialize(home)
    assert not (home / "config.toml").exists()
