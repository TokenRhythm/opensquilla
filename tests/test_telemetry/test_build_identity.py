from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import __version__
from opensquilla.telemetry import build_identity

COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clear_source_commit_cache() -> None:
    build_identity.current_source_commit_id.cache_clear()
    yield
    build_identity.current_source_commit_id.cache_clear()


def _source_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    package = checkout / "src" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    monkeypatch.setattr(
        build_identity,
        "_install_receipt_path",
        lambda: tmp_path / "missing-receipt",
    )
    monkeypatch.setattr(build_identity, "_legacy_install_receipt_path", lambda: None)
    distribution = SimpleNamespace(
        read_text=lambda name: (
            json.dumps({"url": "file:///redacted", "dir_info": {"editable": True}})
            if name == "direct_url.json"
            else None
        )
    )
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    return checkout


def test_source_version_reads_detached_head_without_invoking_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    git_dir = checkout / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(f"{COMMIT}\n", encoding="utf-8")

    assert build_identity.reliability_app_version("0.5.4") == f"0.5.4+source.{COMMIT}"


def test_source_version_resolves_loose_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    git_dir = checkout / ".git"
    branch_ref = git_dir / "refs" / "heads" / "main"
    branch_ref.parent.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    branch_ref.write_text(f"{COMMIT}\n", encoding="utf-8")

    assert build_identity.current_source_commit_id() == COMMIT


@pytest.mark.parametrize("relative_git_dir", [False, True])
def test_source_version_resolves_linked_worktree_packed_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative_git_dir: bool,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    common_dir = tmp_path / "common.git"
    worktree_git_dir = common_dir / "worktrees" / "checkout"
    worktree_git_dir.mkdir(parents=True)
    git_dir_value = (
        Path("..") / "common.git" / "worktrees" / "checkout"
        if relative_git_dir
        else worktree_git_dir
    )
    (checkout / ".git").write_text(f"gitdir: {git_dir_value}\n", encoding="utf-8")
    (worktree_git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (common_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled\n{COMMIT} refs/heads/main\n",
        encoding="utf-8",
    )

    assert build_identity.current_source_commit_id() == COMMIT


def test_non_editable_distribution_does_not_report_surrounding_git_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    git_dir = checkout / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(COMMIT, encoding="utf-8")
    distribution = SimpleNamespace(
        read_text=lambda _name: json.dumps(
            {"url": "file:///redacted", "dir_info": {"editable": False}}
        )
    )
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )

    assert build_identity.current_source_commit_id() is None
    assert build_identity.reliability_app_version("0.5.4") == "0.5.4"


def test_source_installer_receipt_freezes_commit_after_checkout_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "venv" / "site-packages" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    distribution = SimpleNamespace(
        read_text=lambda _name: json.dumps(
            {"url": "file:///redacted", "dir_info": {"editable": False}}
        )
    )
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    receipt = tmp_path / "install-receipt.json"
    receipt.write_text(
        json.dumps({"version": 1, "install_method": "uv-tool", "source_commit_id": COMMIT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_identity, "_install_receipt_path", lambda: receipt)
    monkeypatch.setattr(build_identity, "_legacy_install_receipt_path", lambda: None)

    assert build_identity.current_source_commit_id() == COMMIT


def test_profile_runtime_falls_back_to_legacy_source_installer_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "venv" / "site-packages" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    distribution = SimpleNamespace(
        read_text=lambda _name: json.dumps(
            {"url": "file:///redacted", "dir_info": {"editable": False}}
        )
    )
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    legacy_receipt = tmp_path / "legacy-install-receipt.json"
    legacy_receipt.write_text(
        json.dumps({"version": 1, "source_commit_id": COMMIT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        build_identity,
        "_install_receipt_path",
        lambda: tmp_path / "selected-profile" / "install-receipt.json",
    )
    monkeypatch.setattr(
        build_identity,
        "_legacy_install_receipt_path",
        lambda: legacy_receipt,
    )

    assert build_identity.current_source_commit_id() == COMMIT


def test_legacy_receipt_fallback_is_limited_to_profile_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "work")

    assert build_identity._legacy_install_receipt_path() == (
        tmp_path / ".opensquilla" / "install-receipt.json"
    )

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "custom"))
    assert build_identity._legacy_install_receipt_path() is None


@pytest.mark.parametrize(
    "receipt_payload",
    [
        "{not-json",
        json.dumps({"version": 1, "source_commit_id": COMMIT.upper()}),
        json.dumps({"version": 1, "source_commit_id": "a" * 64}),
        json.dumps({"version": 1, "source_commit_id": f"{COMMIT}-dirty"}),
    ],
)
def test_malformed_source_receipt_does_not_fall_back_or_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_payload: str,
) -> None:
    package = tmp_path / "venv" / "site-packages" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    distribution = SimpleNamespace(read_text=lambda _name: None)
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    selected_receipt = tmp_path / "selected-receipt.json"
    selected_receipt.write_text(receipt_payload, encoding="utf-8")
    legacy_receipt = tmp_path / "legacy-receipt.json"
    legacy_receipt.write_text(
        json.dumps({"version": 1, "source_commit_id": COMMIT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_identity, "_install_receipt_path", lambda: selected_receipt)
    monkeypatch.setattr(
        build_identity,
        "_legacy_install_receipt_path",
        lambda: legacy_receipt,
    )

    assert build_identity.current_source_commit_id() is None


def test_ordinary_release_without_source_receipt_keeps_base_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "venv" / "site-packages" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    distribution = SimpleNamespace(read_text=lambda _name: None)
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    monkeypatch.setattr(
        build_identity,
        "_install_receipt_path",
        lambda: tmp_path / "missing-receipt",
    )
    monkeypatch.setattr(build_identity, "_legacy_install_receipt_path", lambda: None)

    assert build_identity.reliability_app_version("0.5.4") == "0.5.4"


def test_ordinary_release_ignores_stale_source_install_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "venv" / "site-packages" / "opensquilla"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    module_file.touch()
    monkeypatch.setattr(build_identity.opensquilla, "__file__", str(module_file))
    distribution = SimpleNamespace(read_text=lambda _name: None)
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )
    stale_receipt = tmp_path / "install-receipt.json"
    stale_receipt.write_text(
        json.dumps({"version": 1, "source_commit_id": COMMIT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_identity, "_install_receipt_path", lambda: stale_receipt)
    monkeypatch.setattr(build_identity, "_legacy_install_receipt_path", lambda: None)

    assert build_identity.current_source_commit_id() is None
    assert build_identity.reliability_app_version("0.5.4") == "0.5.4"


def test_pep610_git_install_uses_immutable_vcs_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    (checkout / ".git").mkdir()
    distribution = SimpleNamespace(
        read_text=lambda _name: json.dumps(
            {
                "url": "https://example.invalid/repository",
                "vcs_info": {"vcs": "git", "commit_id": COMMIT},
            }
        )
    )
    monkeypatch.setattr(
        build_identity.importlib_metadata,
        "distribution",
        lambda _name: distribution,
    )

    assert build_identity.current_source_commit_id() == COMMIT


@pytest.mark.parametrize(
    "head",
    [
        "abcdef0",
        "a" * 39,
        "b" * 41,
        "c" * 64,
        COMMIT.upper(),
        "refs/heads/main",
        "d" * 40 + "-dirty",
    ],
)
def test_invalid_or_non_sha1_head_safely_falls_back_to_base_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    head: str,
) -> None:
    checkout = _source_package(monkeypatch, tmp_path)
    git_dir = checkout / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(head, encoding="utf-8")

    assert build_identity.reliability_app_version("0.5.4") == "0.5.4"


def test_source_lookup_failure_is_cached_and_never_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail() -> Path:
        nonlocal calls
        calls += 1
        raise OSError("synthetic")

    monkeypatch.setattr(build_identity, "_source_distribution_details", fail)

    assert build_identity.current_source_commit_id() is None
    assert build_identity.current_source_commit_id() is None
    assert calls == 1


def test_source_identity_does_not_overflow_app_version_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as context:
        context.setattr(build_identity, "current_source_commit_id", lambda: COMMIT)
        base_version = "v" * 17

        assert build_identity.reliability_app_version(base_version) == base_version


def test_current_package_version_leaves_room_for_full_source_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as context:
        context.setattr(build_identity, "current_source_commit_id", lambda: COMMIT)

        encoded = build_identity.reliability_app_version(__version__)

        assert encoded == f"{__version__}+source.{COMMIT}"
        assert len(encoded) <= 64
