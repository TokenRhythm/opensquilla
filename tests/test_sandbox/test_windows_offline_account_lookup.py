"""Read-only account queries; no test creates or changes a Windows account."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opensquilla.sandbox.backend import windows_default_setup as setup


@pytest.mark.skipif(sys.platform != "win32", reason="requires inbox Windows PowerShell")
def test_missing_local_account_is_unconfigured_on_native_windows(monkeypatch):
    # Stay within Windows' 20-character account-name limit, without creating
    # an account or depending on whether this host already uses Windows Safe.
    monkeypatch.setattr(setup, "OFFLINE_USERNAME", "OSq" + uuid4().hex[:17])

    assert setup._query_offline_account() is None


@pytest.mark.skipif(sys.platform != "win32", reason="requires inbox Windows PowerShell")
@pytest.mark.parametrize("case", ["present", "denied", "module_unavailable"])
def test_native_query_preserves_accounts_and_rejects_lookup_errors(monkeypatch, case):
    real_run = setup.subprocess.run
    results = []
    prefixes = {
        "present": (
            "Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop; "
            "function Get-LocalUser { "
            "[pscustomobject]@{SID=@{Value='S-1-5-21-123'}; Description='other owner'} }; "
        ),
        "denied": (
            "Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop; "
            "function Get-LocalUser { "
            "throw [System.UnauthorizedAccessException]::new('lookup denied') }; "
        ),
        "module_unavailable": "$PSModuleAutoLoadingPreference='None'; ",
    }

    def run(command, **kwargs):
        # Execute the production query in the real shell, with read-only
        # fixtures for outcomes that cannot safely be forced on this host.
        command = [*command[:-1], prefixes[case] + command[-1]]
        result = real_run(command, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(setup.subprocess, "run", run)
    if case == "present":
        assert setup._query_offline_account() == {
            "sid": "S-1-5-21-123",
            "description": "other owner",
        }
        assert results[0].returncode == 0
    else:
        with pytest.raises(OSError, match="offline_identity_lookup_failed"):
            setup._query_offline_account()
        assert results[0].returncode != 0
        if case == "denied":
            assert "UnauthorizedAccessException" in results[0].stderr


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, ""),
        (1, '{"sid":"S-1-5-21-123","description":"other owner"}'),
        (0, "invalid JSON"),
        (0, "null"),
        (0, '{"sid":null}'),
    ],
)
def test_failed_or_malformed_query_never_means_account_absent(monkeypatch, returncode, stdout):
    monkeypatch.setattr(setup, "_trusted_windows_powershell_path", lambda: "powershell.exe")
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
    )

    with pytest.raises(OSError, match="offline_identity_lookup_failed"):
        setup._query_offline_account()
