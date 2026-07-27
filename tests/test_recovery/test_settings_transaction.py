from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from opensquilla.recovery import inspect_profile
from opensquilla.recovery.atomic import _native_io_path
from opensquilla.recovery.errors import AtomicStateUnknownError, RecoveryError
from opensquilla.recovery.settings_transaction import (
    apply_desktop_settings,
    recover_desktop_settings,
    settings_transaction_exists,
    settings_transaction_journal,
)


class SimulatedProcessCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def _isolated_profile_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")


def _profile(tmp_path: Path) -> tuple[Path, Path, str, str]:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    workspace = home / "workspace"
    state = home / "state"
    workspace.mkdir(parents=True)
    state.mkdir()
    (workspace / "SOUL.md").write_text("synthetic identity\n", encoding="utf-8")
    config = (
        f"state_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n"
        'search_provider = "duckduckgo"\n\n'
        '[llm]\nprovider = "ollama"\nmodel = "old-model"\n'
    )
    credential = json.dumps(
        {
            "provider": "ollama",
            "model": "old-model",
            "encryptedApiKey": "synthetic-old-ciphertext",
        },
        sort_keys=True,
    )
    # The settings protocol performs a byte-exact CAS. Keep fixture bytes
    # platform-independent instead of letting Windows text I/O inject CRLF.
    (home / "config.toml").write_bytes(config.encode())
    (user_data / "desktop-credential.json").write_bytes(credential.encode())
    return home, user_data / "desktop-credential.json", config, credential


def _new_pair(home: Path) -> tuple[str, str]:
    old_config = (home / "config.toml").read_text(encoding="utf-8")
    config = old_config.replace('model = "old-model"', 'model = "new-model"')
    credential = json.dumps(
        {
            "provider": "ollama",
            "model": "new-model",
            "encryptedApiKey": "synthetic-new-ciphertext",
        },
        sort_keys=True,
    )
    return config, credential


def _apply(
    home: Path,
    *,
    old_config: str | None,
    old_credential: str | None,
    new_config: str,
    new_credential: str,
    failpoint=None,
):
    report = inspect_profile(home, profile_kind="desktop-primary")
    return apply_desktop_settings(
        home,
        transaction_id=report.transaction_id,
        expected_revision=report.revision,
        payload={
            "expected_config": old_config,
            "config": new_config,
            "expected_credential": old_credential,
            "credential": new_credential,
        },
        _failpoint=failpoint,
    )


def _imported_credential(transaction_id: str) -> str:
    return json.dumps(
        {
            "provider": "ollama",
            "model": "old-model",
            "baseUrl": "http://127.0.0.1:11434/v1",
            "apiKeyEnv": "",
            "encryptedApiKey": "synthetic-imported-ciphertext",
            "configAuthority": "profile",
            "importTransactionId": transaction_id,
        },
        sort_keys=True,
    )


def test_settings_pair_is_committed_without_secret_bearing_journal(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)

    report = _apply(
        home,
        old_config=old_config,
        old_credential=old_credential,
        new_config=new_config,
        new_credential=new_credential,
    )

    assert report.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == new_config
    assert credential_path.read_text(encoding="utf-8") == new_credential
    assert not settings_transaction_journal(home).exists()
    assert not list(home.glob(".config.toml.*"))
    assert not list(credential_path.parent.glob(".desktop-credential.json.*"))


def test_imported_settings_retain_exact_owner_only_credential_backup(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    import_transaction_id = str(uuid.uuid4())
    new_credential = _imported_credential(import_transaction_id)

    report = _apply(
        home,
        old_config=old_config,
        old_credential=old_credential,
        new_config=old_config,
        new_credential=new_credential,
    )

    backup = credential_path.with_name(
        f"desktop-credential.import-backup.{import_transaction_id}.json"
    )
    assert report.outcome == "ready"
    assert credential_path.read_text(encoding="utf-8") == new_credential
    assert backup.read_text(encoding="utf-8") == old_credential
    if callable(getattr(os, "fchmod", None)):
        assert backup.stat().st_mode & 0o777 == 0o600
    assert not settings_transaction_journal(home).exists()


def test_imported_settings_backup_survives_failpoint_recovery(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    import_transaction_id = str(uuid.uuid4())
    new_credential = _imported_credential(import_transaction_id)

    def crash_after_parking(phase: str) -> None:
        if phase == "prepared":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=old_config,
            new_credential=new_credential,
            failpoint=crash_after_parking,
        )

    backup = credential_path.with_name(
        f"desktop-credential.import-backup.{import_transaction_id}.json"
    )
    assert backup.read_text(encoding="utf-8") == old_credential
    recovered = recover_desktop_settings(home)
    assert recovered.outcome == "ready"
    assert credential_path.read_text(encoding="utf-8") == new_credential
    assert backup.read_text(encoding="utf-8") == old_credential
    if callable(getattr(os, "fchmod", None)):
        assert backup.stat().st_mode & 0o777 == 0o600
    assert not settings_transaction_journal(home).exists()


def test_imported_settings_reject_preexisting_backup_before_journal(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    import_transaction_id = str(uuid.uuid4())
    new_credential = _imported_credential(import_transaction_id)
    backup = credential_path.with_name(
        f"desktop-credential.import-backup.{import_transaction_id}.json"
    )
    backup.write_text("synthetic-existing-backup\n", encoding="utf-8")

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=old_config,
            new_credential=new_credential,
        )

    assert caught.value.stable_code == "settings_credential_backup_exists"
    assert (home / "config.toml").read_text(encoding="utf-8") == old_config
    assert credential_path.read_text(encoding="utf-8") == old_credential
    assert backup.read_text(encoding="utf-8") == "synthetic-existing-backup\n"
    assert not settings_transaction_journal(home).exists()


def test_imported_settings_preserve_windows_acl_when_fchmod_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.settings_transaction as transaction

    home, credential_path, old_config, old_credential = _profile(tmp_path)
    credential_path.chmod(0o666)
    import_transaction_id = str(uuid.uuid4())
    monkeypatch.setattr(transaction.os, "fchmod", None, raising=False)

    _apply(
        home,
        old_config=old_config,
        old_credential=old_credential,
        new_config=old_config,
        new_credential=_imported_credential(import_transaction_id),
    )

    backup = credential_path.with_name(
        f"desktop-credential.import-backup.{import_transaction_id}.json"
    )
    assert backup.read_text(encoding="utf-8") == old_credential
    assert backup.stat().st_mode & 0o777 == 0o666


def test_credential_hardening_does_not_fsync_read_only_windows_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.settings_transaction as transaction

    _, credential_path, _, old_credential = _profile(tmp_path)
    credential_path.chmod(0o666)
    monkeypatch.setattr(transaction.os, "fchmod", None, raising=False)
    monkeypatch.setattr(
        transaction.os,
        "fsync",
        lambda _fd: pytest.fail("Windows-like credential hardening must not fsync O_RDONLY"),
    )

    transaction._restrict_existing_credential_to_owner(credential_path)

    assert credential_path.read_text(encoding="utf-8") == old_credential
    assert credential_path.stat().st_mode & 0o777 == 0o666


@pytest.mark.parametrize(
    "credential_payload",
    [
        {"configAuthority": "profile", "importTransactionId": ""},
        {"configAuthority": "profile", "importTransactionId": "not-a-uuid"},
        {"configAuthority": "generated", "importTransactionId": str(uuid.uuid4())},
    ],
)
def test_settings_reject_invalid_credential_authority_binding(
    tmp_path: Path,
    credential_payload: dict[str, str],
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    credential_payload.update(
        {
            "provider": "ollama",
            "model": "synthetic",
            "encryptedApiKey": "synthetic-ciphertext",
        }
    )

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=old_config,
            new_credential=json.dumps(credential_payload, sort_keys=True),
        )

    assert caught.value.stable_code == "settings_credential_invalid"
    assert credential_path.read_text(encoding="utf-8") == old_credential
    assert not settings_transaction_journal(home).exists()


def test_imported_credential_must_match_the_exact_candidate_config(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    import_transaction_id = str(uuid.uuid4())
    mismatched = json.loads(_imported_credential(import_transaction_id))
    mismatched["provider"] = "openai"

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=old_config,
            new_credential=json.dumps(mismatched, sort_keys=True),
        )

    assert caught.value.stable_code == "settings_credential_config_mismatch"
    assert (home / "config.toml").read_text(encoding="utf-8") == old_config
    assert credential_path.read_text(encoding="utf-8") == old_credential
    assert not settings_transaction_journal(home).exists()


@pytest.mark.parametrize("crash_phase", ["prepared", "credential_published", "config_published"])
def test_crash_is_detected_and_identity_proven_pair_is_recovered(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)

    def crash(phase: str) -> None:
        if phase == crash_phase:
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
            failpoint=crash,
        )

    journal = settings_transaction_journal(home)
    journal_text = journal.read_text(encoding="utf-8")
    assert "synthetic-new-ciphertext" not in journal_text
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    assert blocked.outcome == "recovery_required"
    assert blocked.stable_code == "settings_transaction_incomplete"
    assert "recover-settings" in blocked.allowed_actions
    recovered = recover_desktop_settings(home)

    assert recovered.outcome == "ready"
    assert (home / "config.toml").read_text(encoding="utf-8") == new_config
    assert credential_path.read_text(encoding="utf-8") == new_credential
    assert not journal.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_settings_transaction_recovers_silently(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    index = 0
    while len(str(home)) < 275:
        user_data /= f"user-data-segment-{index:02d}-0123456789"
        home = user_data / "opensquilla"
        index += 1
    workspace = home / "workspace"
    state = home / "state"
    os.makedirs(_native_io_path(workspace))
    os.makedirs(_native_io_path(state))
    with open(_native_io_path(workspace / "SOUL.md"), "w", encoding="utf-8") as handle:
        handle.write("synthetic identity\n")
    old_config = (
        f"state_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n"
        'search_provider = "duckduckgo"\n\n'
        '[llm]\nprovider = "ollama"\nmodel = "old-model"\n'
    )
    old_credential = json.dumps(
        {
            "provider": "ollama",
            "model": "old-model",
            "encryptedApiKey": "synthetic-old-ciphertext",
        },
        sort_keys=True,
    )
    with open(_native_io_path(home / "config.toml"), "wb") as handle:
        handle.write(old_config.encode())
    credential_path = user_data / "desktop-credential.json"
    with open(_native_io_path(credential_path), "wb") as handle:
        handle.write(old_credential.encode())
    new_config = old_config.replace('model = "old-model"', 'model = "new-model"')
    new_credential = json.dumps(
        {
            "provider": "ollama",
            "model": "new-model",
            "encryptedApiKey": "synthetic-new-ciphertext",
        },
        sort_keys=True,
    )

    before = inspect_profile(home, profile_kind="desktop-primary")
    assert before.outcome == "ready", before

    def crash(phase: str) -> None:
        if phase == "credential_published":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
            failpoint=crash,
        )

    journal = settings_transaction_journal(home)
    assert settings_transaction_exists(home)
    assert os.path.lexists(_native_io_path(journal))
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    assert blocked.stable_code == "settings_transaction_incomplete"

    recovered = recover_desktop_settings(home)

    assert recovered.outcome == "ready", recovered
    with open(_native_io_path(home / "config.toml"), encoding="utf-8") as handle:
        assert handle.read() == new_config
    with open(_native_io_path(credential_path), encoding="utf-8") as handle:
        assert handle.read() == new_credential
    assert not settings_transaction_exists(home)


def test_enospc_during_publication_rolls_back_both_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.settings_transaction as transaction

    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)
    original_publish = transaction._publish
    calls = 0

    def fail_second_publish(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "synthetic disk full during config publication")
        original_publish(*args, **kwargs)

    monkeypatch.setattr(transaction, "_publish", fail_second_publish)

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
        )

    assert caught.value.stable_code == "settings_apply_failed"
    assert (home / "config.toml").read_text(encoding="utf-8") == old_config
    assert credential_path.read_text(encoding="utf-8") == old_credential
    assert not settings_transaction_journal(home).exists()


def test_stale_electron_preflight_never_overwrites_new_gateway_config(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)
    report = inspect_profile(home, profile_kind="desktop-primary")
    gateway_config = old_config + "\nlog_level = \"debug\"\n"
    (home / "config.toml").write_text(gateway_config, encoding="utf-8")

    with pytest.raises(RecoveryError) as caught:
        apply_desktop_settings(
            home,
            transaction_id=report.transaction_id,
            expected_revision=report.revision,
            payload={
                "expected_config": old_config,
                "config": new_config,
                "expected_credential": old_credential,
                "credential": new_credential,
            },
        )

    assert caught.value.stable_code == "stale_recovery_transaction"
    assert (home / "config.toml").read_text(encoding="utf-8") == gateway_config
    assert credential_path.read_text(encoding="utf-8") == old_credential


def test_settings_publication_preserves_a_mutation_during_old_file_parking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.settings_transaction as transaction

    home, credential_path, old_config, old_credential = _profile(tmp_path)
    config_path = home / "config.toml"
    new_config, new_credential = _new_pair(home)
    concurrent_config = old_config + "\nexternal_edit = true\n"
    original_move = transaction._durable_move_no_replace

    def mutate_during_park(source: Path, destination: Path) -> None:
        if source == config_path and destination.name.endswith(".old"):
            source.write_text(concurrent_config, encoding="utf-8")
        original_move(source, destination)

    monkeypatch.setattr(transaction, "_durable_move_no_replace", mutate_during_park)

    with pytest.raises(AtomicStateUnknownError):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
        )

    journal = settings_transaction_journal(home)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    config_backup = Path(payload["paths"]["config_backup"])
    credential_backup = Path(payload["paths"]["credential_backup"])
    assert not config_path.exists()
    assert config_backup.read_text(encoding="utf-8") == concurrent_config
    assert not credential_path.exists()
    assert credential_backup.read_text(encoding="utf-8") == old_credential
    assert inspect_profile(home).stable_code == "settings_transaction_incomplete"


def test_settings_save_cannot_redirect_workspace_or_chat_state(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)
    redirected = new_config.replace(
        json.dumps(str(home / "state")),
        json.dumps(str(tmp_path / "other-state")),
    )
    assert redirected != new_config

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=redirected,
            new_credential=new_credential,
        )

    assert caught.value.stable_code == "settings_data_root_changed"
    assert (home / "config.toml").read_text(encoding="utf-8") == old_config
    assert credential_path.read_text(encoding="utf-8") == old_credential


def test_settings_save_cannot_redirect_attachment_media_root(tmp_path: Path) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    media_root = home / "media"
    protected_config = (
        old_config
        + "\n[attachments]\n"
        + f"media_root = {json.dumps(str(media_root))}\n"
    )
    (home / "config.toml").write_bytes(protected_config.encode())
    new_config, new_credential = _new_pair(home)
    redirected = new_config.replace(
        json.dumps(str(media_root)),
        json.dumps(str(tmp_path / "other-media")),
    )
    assert redirected != new_config

    with pytest.raises(RecoveryError) as caught:
        _apply(
            home,
            old_config=protected_config,
            old_credential=old_credential,
            new_config=redirected,
            new_credential=new_credential,
        )

    assert caught.value.stable_code == "settings_data_root_changed"
    assert (home / "config.toml").read_text(encoding="utf-8") == protected_config
    assert credential_path.read_text(encoding="utf-8") == old_credential


def test_unknown_settings_journal_phase_is_preserved_for_manual_recovery(
    tmp_path: Path,
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)

    def crash_after_prepare(phase: str) -> None:
        if phase == "prepared":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
            failpoint=crash_after_prepare,
        )

    journal = settings_transaction_journal(home)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["phase"] = "future-phase"
    journal.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    artifacts_before = sorted(path.name for path in home.parent.glob(".*-transaction.json"))

    with pytest.raises(RecoveryError) as caught:
        recover_desktop_settings(home)

    assert caught.value.stable_code == "settings_transaction_invalid"
    assert journal.exists()
    assert sorted(path.name for path in home.parent.glob(".*-transaction.json")) == artifacts_before
    assert not (home / "config.toml").exists()
    assert not credential_path.exists()
    assert Path(payload["paths"]["config_backup"]).read_text(encoding="utf-8") == old_config
    assert (
        Path(payload["paths"]["credential_backup"]).read_text(encoding="utf-8")
        == old_credential
    )


def test_settings_recovery_rejects_a_phase_impossible_publication_order(
    tmp_path: Path,
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)

    def crash_after_prepare(phase: str) -> None:
        if phase == "prepared":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=old_config,
            old_credential=old_credential,
            new_config=new_config,
            new_credential=new_credential,
            failpoint=crash_after_prepare,
        )

    journal = settings_transaction_journal(home)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    config_new = Path(payload["paths"]["config_new"])
    os.replace(config_new, home / "config.toml")

    with pytest.raises(AtomicStateUnknownError):
        recover_desktop_settings(home)

    assert journal.exists()
    assert not credential_path.exists()
    assert (
        Path(payload["paths"]["credential_backup"]).read_text(encoding="utf-8")
        == old_credential
    )
    assert (home / "config.toml").read_text(encoding="utf-8") == new_config


def test_fresh_onboarding_initializes_only_canonical_roots(tmp_path: Path) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    home = user_data / "opensquilla"
    config = (
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        'search_provider = "duckduckgo"\n\n'
        '[llm]\nprovider = "ollama"\nmodel = "synthetic"\n'
    )
    credential = json.dumps({"provider": "ollama", "model": "synthetic"})

    report = _apply(
        home,
        old_config=None,
        old_credential=None,
        new_config=config,
        new_credential=credential,
    )

    assert report.outcome == "ready"
    assert (home / "workspace").is_dir()
    assert (home / "state").is_dir()
    assert (home / "config.toml").read_text(encoding="utf-8") == config
    assert (user_data / "desktop-credential.json").read_text(encoding="utf-8") == credential


def test_crashed_fresh_onboarding_recovers_canonical_roots_and_pair(tmp_path: Path) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    home = user_data / "opensquilla"
    config = (
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        'search_provider = "duckduckgo"\n\n'
        '[llm]\nprovider = "ollama"\nmodel = "synthetic"\n'
    )
    credential = json.dumps({"provider": "ollama", "model": "synthetic"})

    def crash_after_prepare(phase: str) -> None:
        if phase == "prepared":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _apply(
            home,
            old_config=None,
            old_credential=None,
            new_config=config,
            new_credential=credential,
            failpoint=crash_after_prepare,
        )
    assert inspect_profile(home).stable_code == "settings_transaction_incomplete"

    recovered = recover_desktop_settings(home)

    assert recovered.outcome == "ready"
    assert (home / "workspace").is_dir()
    assert (home / "state").is_dir()
    assert (home / "config.toml").read_text(encoding="utf-8") == config
    assert (user_data / "desktop-credential.json").read_text(encoding="utf-8") == credential


def test_settings_writer_refuses_cross_process_profile_lock(
    tmp_path: Path,
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)
    report = inspect_profile(home, profile_kind="desktop-primary")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from opensquilla.recovery.locking import ProfileOperationLock\n"
                "with ProfileOperationLock(sys.argv[1]):\n"
                " print('locked', flush=True)\n"
                " sys.stdin.readline()\n"
            ),
            str(home),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **dict(os.environ),
            "OPENSQUILLA_USER_STATE_DIR": str(tmp_path / "user-state"),
            "OPENSQUILLA_TEST": "1",
        },
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(RecoveryError) as caught:
            apply_desktop_settings(
                home,
                transaction_id=report.transaction_id,
                expected_revision=report.revision,
                payload={
                    "expected_config": old_config,
                    "config": new_config,
                    "expected_credential": old_credential,
                    "credential": new_credential,
                },
            )
        assert caught.value.stable_code == "profile_lock_busy"
        assert (home / "config.toml").read_text(encoding="utf-8") == old_config
        assert credential_path.read_text(encoding="utf-8") == old_credential
    finally:
        if child.stdin is not None:
            child.stdin.write("done\n")
            child.stdin.flush()
        child.communicate(timeout=10)


def test_settings_transaction_never_mutates_an_ordinary_cli_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home, credential_path, old_config, old_credential = _profile(tmp_path)
    new_config, new_credential = _new_pair(home)
    report = inspect_profile(home, profile_kind="desktop-primary")
    monkeypatch.delenv("OPENSQUILLA_PROFILE_KIND", raising=False)

    with pytest.raises(RecoveryError) as caught:
        apply_desktop_settings(
            home,
            transaction_id=report.transaction_id,
            expected_revision=report.revision,
            payload={
                "expected_config": old_config,
                "config": new_config,
                "expected_credential": old_credential,
                "credential": new_credential,
            },
        )

    assert caught.value.stable_code == "settings_profile_kind_invalid"
    assert (home / "config.toml").read_text(encoding="utf-8") == old_config
    assert credential_path.read_text(encoding="utf-8") == old_credential


def test_windows_settings_moves_delegate_to_hardened_no_replace_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.recovery.settings_transaction as transaction

    calls: list[tuple[Path, Path]] = []
    source = Path("C:/synthetic/source")
    destination = Path("C:/synthetic/destination")
    monkeypatch.setattr(transaction.os, "name", "nt")
    monkeypatch.setattr(
        transaction,
        "native_move_no_replace",
        lambda source, destination: calls.append((source, destination)),
    )

    transaction._durable_move_no_replace(
        source,
        destination,
    )

    assert calls == [(source, destination)]
