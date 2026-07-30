"""Regression tests for private, atomic Codex auth token persistence.

The implementation keeps writing through the descriptor returned by
``mkstemp`` instead of closing it and reopening the temporary path. On
POSIX it also reasserts mode 0o600 through that descriptor before any
secret bytes are written. Failure paths must close the descriptor and
remove the partial file.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from opensquilla.provider.codex_auth import _persist_refreshed_tokens


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only apply on Unix")
def test_persist_refreshed_tokens_writes_strict_0600(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}), encoding="utf-8")

    _persist_refreshed_tokens(auth, {"access_token": "tok-new"})

    mode = stat.S_IMODE(os.stat(auth).st_mode)
    assert mode == 0o600, f"expected 0o600 on persisted auth file, got 0o{mode:o}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only apply on Unix")
def test_persist_refreshed_tokens_tmp_file_is_never_broad(tmp_path: Path, monkeypatch) -> None:
    """The tmp file that lives between mkstemp and replace must be 0o600.

    Two observations together prove the file is never visible at
    broader perms:

    * ``os.fchmod`` is called with 0o600 on the still-open mkstemp fd
      before any secret bytes are written (fd-bound tightening — the
      close-and-reopen approach silently ignored the mode because
      ``os.open(..., O_CREAT, mode)`` does not apply ``mode`` to a
      pre-existing file);
    * at the moment ``os.replace`` renames the tmp file over
      ``auth.json`` — after the full payload was written — the tmp
      file still carries 0o600.
    """

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}), encoding="utf-8")

    fchmod_modes: list[int] = []
    real_fchmod = os.fchmod

    def spy_fchmod(fd, mode):  # type: ignore[no-untyped-def]
        fchmod_modes.append(mode)
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", spy_fchmod)

    replaced_modes: list[int] = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if ".auth-" in str(src):
            replaced_modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)

    _persist_refreshed_tokens(auth, {"access_token": "tok-new"})

    assert fchmod_modes, "expected fchmod on the open tmp fd before writing tokens"
    assert all((mode & 0o777) == 0o600 for mode in fchmod_modes), (
        f"tmp fd was tightened to non-0o600 modes: {[oct(m) for m in fchmod_modes]}"
    )
    assert replaced_modes, "expected os.replace to move a .auth- tmp file into place"
    assert all(mode == 0o600 for mode in replaced_modes), (
        f"tmp auth file was observable at non-0o600 modes: {[oct(m) for m in replaced_modes]}"
    )

    # The final persisted file is also 0o600.
    mode = stat.S_IMODE(os.stat(auth).st_mode)
    assert mode == 0o600


def test_persist_refreshed_tokens_preserves_payload(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"old": "v"}}), encoding="utf-8")

    _persist_refreshed_tokens(
        auth,
        {
            "access_token": "tok-new",
            "refresh_token": "refresh-new",
            "id_token": "id-new",
        },
    )

    payload = json.loads(auth.read_text(encoding="utf-8"))
    assert payload["tokens"]["access_token"] == "tok-new"
    assert payload["tokens"]["refresh_token"] == "refresh-new"
    assert payload["tokens"]["id_token"] == "id-new"
    assert payload["tokens"]["old"] == "v"  # existing fields preserved
    assert payload["last_refresh"].endswith("Z")


def test_persist_refreshed_tokens_cleans_tmp_on_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """If json.dump raises, the partial tmp file must be unlinked."""

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}), encoding="utf-8")

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated disk error")

    monkeypatch.setattr(json, "dump", boom)

    with pytest.raises(RuntimeError):
        _persist_refreshed_tokens(auth, {"access_token": "tok-new"})

    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name.startswith(".auth-") and p.is_file()
    ]
    assert leftovers == [], f"tmp auth file leaked: {leftovers}"
