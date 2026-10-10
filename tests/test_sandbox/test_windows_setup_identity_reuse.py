"""Explicit setup tests; all account, login and system mutation calls are fake."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import windows_default_identity as identity
from opensquilla.sandbox.backend import windows_default_setup as setup
from opensquilla.sandbox.backend.windows_default_network import WindowsNetworkSetup


@pytest.fixture
def profile(tmp_path, monkeypatch):
    from opensquilla.sandbox.backend import windows_setup_process

    monkeypatch.setattr(
        windows_setup_process, "_process_identity", lambda pid: f"created:{pid}",
    )
    monkeypatch.setattr(setup, "_current_windows_user_sid", lambda: "S-1-owner")
    monkeypatch.setattr(setup, "_query_offline_account", lambda: None)
    monkeypatch.setattr(identity, "validate_offline_identity", lambda _identity: False)
    monkeypatch.setattr(identity, "protect_password", lambda value: "protected:" + value)
    monkeypatch.setattr(identity, "unprotect_password", lambda value: "old-password")
    monkeypatch.setattr(setup, "_generate_offline_user_password", lambda: "new-password")
    return tmp_path


def network(password="protected:old", **changes):
    return replace(
        WindowsNetworkSetup(
            offline_user_sid="S-1-sandbox",
            offline_username=setup.OFFLINE_USERNAME,
            protected_password=password,
            allowed_proxy_ports=(48123,),
            allow_local_binding=False,
            firewall_rule_version=5,
            wfp_rule_version=2,
        ),
        **changes,
    )


def fail_if_called(*_args, **_kwargs):
    raise AssertionError("unexpected system operation")


def test_cli_identity_is_reused_read_only_by_desktop_even_with_stale_network(profile, monkeypatch):
    cli, desktop = setup._allowed_setup_marker_paths(profile)
    setup.write_setup_marker(cli, network=network(firewall_rule_version=1))
    original = cli.read_bytes()
    monkeypatch.setattr(
        identity, "validate_offline_identity", lambda value: value.sid == "S-1-sandbox"
    )
    monkeypatch.setattr(setup, "_query_offline_account", fail_if_called)
    monkeypatch.setattr(setup.subprocess, "run", fail_if_called)

    result = setup.ensure_offline_sandbox_user(desktop.parent, profile_path=profile)

    assert result["protectedPassword"] == "protected:old"
    assert result["sourceMarker"] == str(cli)
    assert cli.read_bytes() == original
    assert not desktop.exists()


def test_fresh_setup_check_does_not_use_cached_success(profile, monkeypatch):
    marker = setup.default_setup_marker_path(profile)
    setup.write_setup_marker(marker, network=network())
    monkeypatch.setattr(identity, "validate_offline_identity", lambda _identity: True)
    assert setup.setup_marker_identity_ready(marker)
    monkeypatch.setattr(identity, "validate_offline_identity", lambda _identity: False)
    assert setup.setup_marker_identity_ready(marker)  # ordinary poll remains cached
    assert not setup.setup_marker_identity_ready(marker, fresh=True)


def test_matching_rules_are_not_reinstalled_when_reusing_peer(profile, monkeypatch):
    from opensquilla.sandbox.backend import windows_default_firewall as firewall
    from opensquilla.sandbox.backend import windows_default_wfp as wfp

    cli, desktop = setup._allowed_setup_marker_paths(profile)
    setup.write_setup_marker(cli, network=network())
    monkeypatch.setattr(identity, "validate_offline_identity", lambda _identity: True)
    monkeypatch.setattr(firewall, "install_firewall_rules", fail_if_called)
    monkeypatch.setattr(wfp, "install_wfp_filters_for_user", fail_if_called)

    assert setup.establish_windows_network_setup(desktop, profile_path=profile) == network()


def test_new_credential_survives_network_install_failure(profile, monkeypatch):
    from opensquilla.sandbox.backend import windows_default_firewall as firewall

    marker = setup.default_setup_marker_path(profile)
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="S-1-sandbox", stderr=""),
    )

    def broken_firewall(_rules):
        pending = setup.read_setup_marker(marker)
        assert pending is not None and pending.setup_state == "pending"
        assert pending.network.protected_password == "protected:new-password"
        raise OSError("rules unavailable")

    monkeypatch.setattr(firewall, "install_firewall_rules", broken_firewall)
    with pytest.raises(OSError, match="rules unavailable"):
        setup.establish_windows_network_setup(marker, profile_path=profile)
    assert not setup.setup_marker_is_current(marker)


@pytest.mark.parametrize(
    "description",
    ["OpenSquilla sandbox owner=S-1-owner", "OpenSquilla offline sandbox network identity"],
)
def test_stale_identity_requires_explicit_repair(profile, monkeypatch, description):
    marker = setup.default_setup_marker_path(profile)
    setup.write_setup_marker(marker, network=network())
    monkeypatch.setattr(
        setup, "_query_offline_account", lambda: {"sid": "S-1-sandbox", "description": description}
    )
    monkeypatch.setattr(setup.subprocess, "run", fail_if_called)
    with pytest.raises(OSError, match="offline_identity_repair_required"):
        setup.ensure_offline_sandbox_user(marker.parent, profile_path=profile)


def test_explicit_legacy_repair_updates_only_two_known_owned_receipts(profile, monkeypatch):
    cli, desktop = setup._allowed_setup_marker_paths(profile)
    for marker in (cli, desktop):
        setup.write_setup_marker(marker, network=network())
    unrelated = profile / "unrelated" / "setup_marker.json"
    setup.write_setup_marker(unrelated, network=network())
    original = unrelated.read_bytes()
    monkeypatch.setattr(
        setup,
        "_query_offline_account",
        lambda: {
            "sid": "S-1-sandbox",
            "description": "OpenSquilla offline sandbox network identity",
        },
    )
    calls = []

    def run(*_args, **kwargs):
        calls.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="S-1-sandbox", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", run)
    setup.ensure_offline_sandbox_user(desktop.parent, profile_path=profile, repair_identity=True)
    assert len(calls) == 1
    assert calls[0]["OPENSQUILLA_SANDBOX_EXPECTED_SID"] == "S-1-sandbox"
    assert calls[0]["OPENSQUILLA_SANDBOX_DESCRIPTION"] == "OpenSquilla sandbox owner=S-1-owner"
    for marker in (cli, desktop):
        assert (
            setup.read_setup_marker(marker).network.protected_password == "protected:new-password"
        )
    assert unrelated.read_bytes() == original


@pytest.mark.parametrize("case", ["unknown_owner", "wrong_sid", "missing_receipt", "undecryptable"])
def test_explicit_repair_never_takes_over_unknown_identity(profile, monkeypatch, case):
    marker = setup.default_setup_marker_path(profile)
    if case != "missing_receipt":
        setup.write_setup_marker(marker, network=network())
    account = {"sid": "S-1-sandbox", "description": "OpenSquilla sandbox owner=S-1-owner"}
    if case == "unknown_owner":
        account["description"] = "Other software account"
    if case == "wrong_sid":
        account["sid"] = "S-1-another"
    if case == "undecryptable":
        monkeypatch.setattr(
            identity,
            "unprotect_password",
            lambda _password: (_ for _ in ()).throw(OSError("cannot decrypt")),
        )
    monkeypatch.setattr(setup, "_query_offline_account", lambda: account)
    monkeypatch.setattr(setup.subprocess, "run", fail_if_called)
    with pytest.raises(OSError, match="offline_identity_owner_unknown"):
        setup.ensure_offline_sandbox_user(marker.parent, profile_path=profile, repair_identity=True)


def test_valid_peer_wins_over_explicit_reset_request(profile, monkeypatch):
    cli, desktop = setup._allowed_setup_marker_paths(profile)
    setup.write_setup_marker(desktop, network=network("stale"))
    setup.write_setup_marker(cli, network=network("valid"))
    monkeypatch.setattr(
        identity, "validate_offline_identity", lambda value: value.protected_password == "valid"
    )
    monkeypatch.setattr(setup, "_query_offline_account", fail_if_called)
    result = setup.ensure_offline_sandbox_user(
        desktop.parent, profile_path=profile, repair_identity=True
    )
    assert result["protectedPassword"] == "valid"


def test_other_admin_can_repair_rules_without_reencrypting_credentials(profile, monkeypatch):
    cli, desktop = setup._allowed_setup_marker_paths(profile)
    setup.write_setup_marker(cli, network=network())
    monkeypatch.setattr(setup, "_current_windows_user_sid", lambda: "S-1-admin")
    monkeypatch.setattr(
        setup,
        "_query_offline_account",
        lambda: {"sid": "S-1-sandbox", "description": "OpenSquilla sandbox owner=S-1-owner"},
    )
    monkeypatch.setattr(identity, "protect_password", fail_if_called)
    monkeypatch.setattr(identity, "unprotect_password", fail_if_called)
    result = setup.ensure_offline_sandbox_user(
        desktop.parent, profile_path=profile, user_sid="S-1-owner"
    )
    assert result["protectedPassword"] == "protected:old"


def test_other_admin_cannot_create_credentials_bound_to_the_wrong_user(profile, monkeypatch):
    marker = setup.default_setup_marker_path(profile)
    monkeypatch.setattr(setup, "_current_windows_user_sid", lambda: "S-1-admin")
    monkeypatch.setattr(identity, "protect_password", fail_if_called)
    monkeypatch.setattr(setup.subprocess, "run", fail_if_called)
    with pytest.raises(OSError, match="credential_handoff_unavailable"):
        setup.ensure_offline_sandbox_user(marker.parent, profile_path=profile, user_sid="S-1-owner")


def test_token_sid_mismatch_is_rejected_and_handle_closed(monkeypatch):
    monkeypatch.setattr(identity, "logon_offline_identity", lambda _identity: 123)
    monkeypatch.setattr(identity, "_token_user_sid", lambda _token: "S-1-recreated")
    closed = []
    monkeypatch.setattr(identity, "_close_token", closed.append)
    assert not identity.validate_offline_identity(
        identity.OfflineSandboxIdentity("S-1-old", "fixed", "cipher")
    )
    assert closed == [123]


def test_invalid_target_is_rejected_before_uac_or_report_removal(profile, monkeypatch):
    marker = profile / "unsupported" / "setup_marker.json"
    marker.parent.mkdir()
    report = setup.setup_helper_report_path(marker)
    report.write_text("old report")
    monkeypatch.setattr(setup, "_windows_profile_path_for_sid", lambda _sid: profile)
    monkeypatch.setattr(setup, "_shell_execute_runas_and_wait", fail_if_called)
    with pytest.raises(OSError, match="marker_path_mismatch"):
        setup.run_elevated_setup_helper(marker)
    assert report.read_text() == "old report"


def test_shared_preparation_deadline_does_not_reset_between_commands(monkeypatch):
    token = setup._SETUP_DEADLINE.set(120)
    try:
        monkeypatch.setattr(setup.time, "monotonic", lambda: 110)
        assert setup.setup_command_timeout() == 10
        monkeypatch.setattr(setup.time, "monotonic", lambda: 121)
        with pytest.raises(OSError, match="deadline_exceeded"):
            setup.setup_command_timeout()
    finally:
        setup._SETUP_DEADLINE.reset(token)


def test_busy_machine_setup_does_not_overwrite_the_running_helpers_report(profile, monkeypatch):
    marker = setup.default_setup_marker_path(profile)
    setup.write_setup_helper_report(marker, state="running", detail="first helper")
    monkeypatch.setattr(setup, "_windows_profile_path_for_sid", lambda _sid: profile)

    @contextmanager
    def busy(_marker):
        raise OSError("windows_setup_busy")
        yield  # pragma: no cover

    monkeypatch.setattr(setup, "_windows_setup_process_lock", busy)
    monkeypatch.setattr(setup, "establish_windows_network_setup", fail_if_called)
    payload = setup._encode_setup_helper_payload(marker, user_sid="S-1-owner")
    assert setup.elevated_setup_helper_main(["--elevated-helper", payload]) == 75
    assert setup.read_setup_helper_report(marker) == {"state": "running", "detail": "first helper"}


def test_failed_sid_query_still_closes_login_handle(monkeypatch):
    monkeypatch.setattr(identity, "logon_offline_identity", lambda _identity: 123)
    monkeypatch.setattr(
        identity,
        "_token_user_sid",
        lambda _token: (_ for _ in ()).throw(OSError("sid lookup failed")),
    )
    closed = []
    monkeypatch.setattr(identity, "_close_token", closed.append)
    with pytest.raises(OSError, match="sid lookup failed"):
        identity.validate_offline_identity(
            identity.OfflineSandboxIdentity("S-1-old", "fixed", "cipher")
        )
    assert closed == [123]
