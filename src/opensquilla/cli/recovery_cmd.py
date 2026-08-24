"""Offline, machine-readable Desktop profile recovery commands."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import typer

from opensquilla.cli.session_schema import prepare_session_schema
from opensquilla.recovery import (
    ConsolidationResult,
    RecoveryError,
    RecoveryReport,
    acknowledge_profile_credential,
    choose_workspace,
    consolidate_recovery_profiles,
    inspect_profile,
    reconcile_profile,
)
from opensquilla.recovery.cleanup import (
    CleanupItem,
    CleanupReport,
    abandon_cleanup_transaction,
    cleanup_apply,
    cleanup_inspect,
    cleanup_scope_fingerprint,
)
from opensquilla.recovery.settings_transaction import (
    MAX_SETTINGS_INPUT_BYTES,
    apply_desktop_settings,
    recover_desktop_settings,
)

recovery_app = typer.Typer(
    help="Inspect and repair Desktop profiles without starting the runtime.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_MAX_CLEANUP_APPROVAL_BYTES = 512 * 1024

# Mutating commands fail closed with profile_lock_busy the instant another
# writer holds the profile locks. Desktop startup passes a small bound here so
# a transient writer (an exiting gateway, a cron tick) resolves on its own
# instead of stranding the user on the manual recovery page.
_LOCK_TIMEOUT_OPTION = typer.Option(
    0.0,
    "--lock-timeout",
    min=0.0,
    help="Seconds to wait for a busy profile writer before failing with profile_lock_busy.",
)


@recovery_app.command("sandbox-upgrade-status")
def sandbox_upgrade_status(
    home: Path = typer.Option(..., "--home", exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect the retained sandbox-upgrade journal without changing it."""
    # Keep the ordinary recovery process below the runtime import boundary.
    # ``opensquilla.sandbox`` exposes a broad public surface at package import
    # time; only this diagnostic command needs the migration implementation.
    from opensquilla.sandbox.upgrade_migration import inspect_sandbox_upgrade

    report = inspect_sandbox_upgrade(home)
    if json_output:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(f"{report.status}: {'ready' if report.ok else 'recovery required'}")
        typer.echo(f"journal: {report.journal_path}")
        typer.echo(f"snapshot: {report.snapshot_path or '-'}")
        if report.error:
            typer.echo(report.error, err=True)
    if not report.ok:
        raise typer.Exit(code=2)


def _emit(report: RecoveryReport, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"{report.outcome}: {report.stable_code}")
    typer.echo(f"home: {report.primary_home}")
    typer.echo(f"workspace: {report.effective_workspace or '-'}")


def _desktop_profile_kind(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"desktop-primary", "desktop-recovery"}:
        raise typer.BadParameter("use desktop-primary or desktop-recovery")
    return normalized


def _failure_report(
    home: Path,
    error: RecoveryError,
    *,
    profile_kind: str | None = None,
) -> RecoveryReport:
    try:
        base = inspect_profile(home, profile_kind=profile_kind)
    except Exception:
        # Inspection is intentionally resilient, but a damaged/unreadable home
        # can still fail below pathlib itself. Keep stdout protocol-valid.
        from opensquilla.recovery.models import RecoveryReport as Report

        home_path = home.expanduser().absolute()
        return Report(
            outcome="recovery_required",
            stable_code=error.stable_code,
            primary_home=home_path,
            effective_workspace=None,
            candidates=(),
            allowed_actions=("copy-diagnostics",),
            transaction_id="",
            revision=0,
            detail=str(error).replace(str(home_path), "<HOME>"),
        )
    return replace(
        base,
        outcome="recovery_required",
        stable_code=error.stable_code,
        detail=str(error).replace(str(base.primary_home), "<HOME>"),
    )


def _run(
    operation: Callable[[], RecoveryReport],
    *,
    home: Path,
    json_output: bool,
    profile_kind: str | None = None,
) -> None:
    try:
        report = operation()
    except RecoveryError as exc:
        _emit(
            _failure_report(home, exc, profile_kind=profile_kind),
            json_output=json_output,
        )
        if not json_output:
            typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    _emit(report, json_output=json_output)


def _emit_cleanup(report: CleanupReport, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"{report.outcome}: {report.stable_code}")
    for item in report.items:
        state = "present" if item.exists else "missing"
        typer.echo(f"{state}  {item.kind}  {item.path}")


def _cleanup_exit(report: CleanupReport, *, json_output: bool) -> None:
    _emit_cleanup(report, json_output=json_output)
    if report.outcome == "partial":
        raise typer.Exit(code=1)
    if report.outcome == "blocked":
        raise typer.Exit(code=2)


def _emit_consolidation(
    report: ConsolidationResult,
    *,
    json_output: bool,
) -> None:
    if json_output:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(f"{report.outcome}: {report.stable_code}")
        typer.echo(f"primary: {report.primary_home}")
        typer.echo(f"backup: {report.backup_path or '-'}")
    if report.outcome == "blocked":
        if not json_output:
            for error in report.errors:
                typer.echo(error, err=True)
        raise typer.Exit(code=2)


def _cleanup_scope_pair(item: CleanupItem) -> tuple[str, str]:
    path = os.path.normcase(os.path.normpath(os.path.abspath(str(item.path))))
    return item.kind, path


def _read_parent_cleanup_approval(
    *,
    user_data: Path,
    expected_fingerprint: str,
) -> frozenset[tuple[str, str]]:
    """Read the approved scope only after the parent pipe reaches EOF."""

    raw = sys.stdin.buffer.read(_MAX_CLEANUP_APPROVAL_BYTES + 1)
    if not raw or len(raw) > _MAX_CLEANUP_APPROVAL_BYTES:
        raise typer.BadParameter("parent cleanup approval is missing or too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise typer.BadParameter("parent cleanup approval is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "scope_fingerprint",
        "items",
    }:
        raise typer.BadParameter("parent cleanup approval schema is invalid")
    if (
        payload["schema_version"] != 1
        or payload["scope_fingerprint"] != expected_fingerprint
        or not isinstance(payload["items"], list)
    ):
        raise typer.BadParameter("parent cleanup approval does not match the request")

    root = os.path.normcase(os.path.normpath(os.path.abspath(str(user_data))))
    approved_items: list[CleanupItem] = []
    approved_pairs: set[tuple[str, str]] = set()
    for raw_item in payload["items"]:
        if not isinstance(raw_item, dict) or set(raw_item) != {"kind", "path"}:
            raise typer.BadParameter("parent cleanup approval item is invalid")
        kind = raw_item["kind"]
        path_raw = raw_item["path"]
        if (
            not isinstance(kind, str)
            or not kind
            or len(kind) > 256
            or "\0" in kind
            or not isinstance(path_raw, str)
            or not path_raw
            or len(path_raw) > 32768
            or "\0" in path_raw
        ):
            raise typer.BadParameter("parent cleanup approval item is invalid")
        path = Path(os.path.abspath(os.path.expanduser(path_raw)))
        path_key = os.path.normcase(os.path.normpath(str(path)))
        try:
            contained = os.path.commonpath((root, path_key)) == root
        except ValueError:
            contained = False
        if not contained or path_key == root:
            raise typer.BadParameter("parent cleanup approval path is outside userData")
        item = CleanupItem(kind=kind, path=path, exists=False, identity=None)
        pair = _cleanup_scope_pair(item)
        if pair in approved_pairs:
            raise typer.BadParameter("parent cleanup approval contains a duplicate item")
        approved_pairs.add(pair)
        approved_items.append(item)

    if cleanup_scope_fingerprint("delete-all-user-data", approved_items) != expected_fingerprint:
        raise typer.BadParameter("parent cleanup approval fingerprint is invalid")
    return frozenset(approved_pairs)


def _settings_payload_from_stdin() -> object:
    raw = sys.stdin.buffer.read(MAX_SETTINGS_INPUT_BYTES + 1)
    if len(raw) > MAX_SETTINGS_INPUT_BYTES:
        raise RecoveryError(
            "Desktop settings input is too large",
            stable_code="settings_input_too_large",
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "Desktop settings input is invalid",
            stable_code="settings_input_invalid",
        ) from exc


@recovery_app.command("inspect")
def recovery_inspect(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    profile_kind: str = typer.Option(
        "desktop-primary",
        "--profile-kind",
        help="desktop-primary (default) or desktop-recovery.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
) -> None:
    """Read profile safety state without creating or modifying any path."""
    kind = _desktop_profile_kind(profile_kind)
    _run(
        lambda: inspect_profile(home, profile_kind=kind),
        home=home,
        json_output=json_output,
        profile_kind=kind,
    )


@recovery_app.command("reconcile")
def recovery_reconcile(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    profile_kind: str = typer.Option(
        "desktop-primary",
        "--profile-kind",
        help="desktop-primary (default) or desktop-recovery.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
    lock_timeout: float = _LOCK_TIMEOUT_OPTION,
) -> None:
    """Apply only a proven no-conflict legacy layout repair."""
    kind = _desktop_profile_kind(profile_kind)
    _run(
        lambda: reconcile_profile(home, profile_kind=kind, lock_timeout=lock_timeout),
        home=home,
        json_output=json_output,
        profile_kind=kind,
    )


@recovery_app.command("choose-workspace")
def recovery_choose_workspace(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    profile_kind: str = typer.Option(
        "desktop-primary",
        "--profile-kind",
        help="desktop-primary (default) or desktop-recovery.",
    ),
    transaction_id: str = typer.Option(..., "--transaction-id", help="Inspection transaction id."),
    expected_revision: int = typer.Option(
        ...,
        "--expected-revision",
        min=0,
        help="Inspection revision used for compare-and-swap.",
    ),
    workspace: Path = typer.Option(..., "--workspace", help="User-confirmed workspace path."),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
) -> None:
    """Persist a user-confirmed workspace with transaction and config CAS."""
    kind = _desktop_profile_kind(profile_kind)
    _run(
        lambda: choose_workspace(
            home,
            transaction_id=transaction_id,
            expected_revision=expected_revision,
            workspace=workspace,
            profile_kind=kind,
        ),
        home=home,
        json_output=json_output,
        profile_kind=kind,
    )


@recovery_app.command("apply-settings")
def recovery_apply_settings(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    transaction_id: str = typer.Option(..., "--transaction-id", help="Inspection transaction id."),
    expected_revision: int = typer.Option(
        ...,
        "--expected-revision",
        min=0,
        help="Inspection revision used for compare-and-swap.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
) -> None:
    """Read secret-bearing candidates from stdin and publish them as one pair."""

    _run(
        lambda: apply_desktop_settings(
            home,
            transaction_id=transaction_id,
            expected_revision=expected_revision,
            payload=_settings_payload_from_stdin(),
        ),
        home=home,
        json_output=json_output,
        profile_kind="desktop-primary",
    )


@recovery_app.command("recover-settings")
def recovery_recover_settings(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
    lock_timeout: float = _LOCK_TIMEOUT_OPTION,
) -> None:
    """Finish an identity-proven interrupted Desktop settings transaction."""

    _run(
        lambda: recover_desktop_settings(home, lock_timeout=lock_timeout),
        home=home,
        json_output=json_output,
        profile_kind="desktop-primary",
    )


@recovery_app.command("recover-config")
def recovery_recover_config(
    home: Path = typer.Option(..., "--home", help="Desktop profile root H."),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
    lock_timeout: float = _LOCK_TIMEOUT_OPTION,
) -> None:
    """Replace a corrupt config.toml from its newest valid sibling backup.

    The corrupt file is always preserved next to itself as
    ``config.toml.corrupt.<stamp>`` first; without a usable backup a minimal
    default config is written so startup can proceed.
    """

    from opensquilla.recovery.config_recovery import recover_config

    _run(
        lambda: recover_config(home, lock_timeout=lock_timeout),
        home=home,
        json_output=json_output,
        profile_kind="desktop-primary",
    )


@recovery_app.command("restore-profile")
def recovery_restore_profile(
    backup: Path = typer.Option(..., "--backup", help="Exact recorded profile backup path."),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
) -> None:
    """Restore a recorded sibling backup after backing up the current target."""
    from opensquilla.recovery.restore import (
        recorded_backup_target,
        restore_profile,
    )

    backup_path = backup.expanduser().absolute()
    try:
        target = recorded_backup_target(backup_path)
    except RecoveryError:
        # Keep failure responses protocol-valid without guessing a successful
        # target. ``restore_profile`` repeats the exact history validation and
        # supplies the authoritative stable code through ``_run``.
        target = backup_path.parent / "opensquilla"
    _run(
        lambda: restore_profile(backup_path),
        home=target,
        json_output=json_output,
        profile_kind="desktop-primary",
    )


@recovery_app.command("recover-transaction")
def recovery_recover_transaction(
    home: Path = typer.Option(..., "--home", help="Desktop primary profile root H."),
    transaction_id: str = typer.Option(..., "--transaction-id", help="Inspection transaction id."),
    expected_revision: int = typer.Option(
        ...,
        "--expected-revision",
        min=0,
        help="Inspection revision used for compare-and-swap.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
    lock_timeout: float = _LOCK_TIMEOUT_OPTION,
) -> None:
    """Safely rollback or finalize one typed interrupted profile transaction."""

    from opensquilla.migration.opensquilla_home import recover_interrupted_profile_import
    from opensquilla.recovery.transaction import recover_profile_transaction

    _run(
        lambda: recover_profile_transaction(
            home,
            transaction_id=transaction_id,
            expected_revision=expected_revision,
            import_recoverer=recover_interrupted_profile_import,
            lock_timeout=lock_timeout,
        ),
        home=home,
        json_output=json_output,
        profile_kind="desktop-primary",
    )


@recovery_app.command("consolidate-profiles")
def recovery_consolidate_profiles(
    user_data: Path = typer.Option(..., "--user-data", help="Electron userData root A."),
    primary_home: Path = typer.Option(
        ...,
        "--primary-home",
        help="Canonical Desktop primary profile root H.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the fixed profile-consolidation protocol.",
    ),
) -> None:
    """Merge every legacy recovery profile into the primary profile."""

    _emit_consolidation(
        consolidate_recovery_profiles(
            user_data,
            primary_home,
            prepare_session_schema=prepare_session_schema,
        ),
        json_output=json_output,
    )


@recovery_app.command("acknowledge-profile-credential")
def recovery_acknowledge_profile_credential(
    user_data: Path = typer.Option(..., "--user-data", help="Electron userData root A."),
    primary_home: Path = typer.Option(
        ...,
        "--primary-home",
        help="Canonical Desktop primary profile root H.",
    ),
    transaction_id: str = typer.Option(
        ...,
        "--transaction-id",
        help="Profile consolidation transaction id.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the fixed profile-consolidation protocol.",
    ),
) -> None:
    """Acknowledge terminal handling of one archived Desktop credential."""

    _emit_consolidation(
        acknowledge_profile_credential(
            user_data,
            primary_home,
            transaction_id,
        ),
        json_output=json_output,
    )


@recovery_app.command("abandon-cleanup")
def recovery_abandon_cleanup(
    user_data: Path = typer.Option(..., "--user-data", help="Electron userData root A."),
    home: Path = typer.Option(..., "--home", help="Active Desktop profile root H."),
    profile_kind: str = typer.Option(
        "desktop-primary",
        "--profile-kind",
        help="desktop-primary (default) or desktop-recovery.",
    ),
    transaction_id: str = typer.Option(..., "--transaction-id", help="Inspection id."),
    expected_revision: int = typer.Option(
        ...,
        "--expected-revision",
        min=0,
        help="Inspection revision used for compare-and-swap.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
    lock_timeout: float = _LOCK_TIMEOUT_OPTION,
) -> None:
    """Preserve a partial cleanup and archive only its exact journal."""

    kind = _desktop_profile_kind(profile_kind)

    def operation() -> RecoveryReport:
        abandon_cleanup_transaction(
            user_data,
            home=home,
            profile_kind=kind,
            transaction_id=transaction_id,
            expected_revision=expected_revision,
            lock_timeout=lock_timeout,
        )
        return inspect_profile(home, profile_kind=kind)

    _run(
        operation,
        home=home,
        json_output=json_output,
        profile_kind=kind,
    )


@recovery_app.command("cleanup-inspect")
def recovery_cleanup_inspect(
    user_data: Path = typer.Option(..., "--user-data", help="Electron userData root A."),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="reset-current-settings, delete-current-profile, or delete-all-user-data.",
    ),
    profile_kind: str = typer.Option(..., "--profile-kind", help="primary or recovery."),
    recovery_id: str | None = typer.Option(None, "--recovery-id", help="Selected recovery UUID."),
    json_output: bool = typer.Option(False, "--json", help="Emit the cleanup protocol."),
) -> None:
    """Read the complete Desktop cleanup inventory without writing anything."""

    report = cleanup_inspect(
        user_data,
        mode=mode,
        profile_kind=profile_kind,
        recovery_id=recovery_id,
    )
    _cleanup_exit(report, json_output=json_output)


@recovery_app.command("cleanup-apply")
def recovery_cleanup_apply(
    user_data: Path = typer.Option(..., "--user-data", help="Electron userData root A."),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="reset-current-settings, delete-current-profile, or delete-all-user-data.",
    ),
    profile_kind: str = typer.Option(..., "--profile-kind", help="primary or recovery."),
    transaction_id: str = typer.Option(..., "--transaction-id", help="Cleanup inspection id."),
    expected_revision: int = typer.Option(
        ...,
        "--expected-revision",
        min=0,
        help="Cleanup inspection revision used for compare-and-swap.",
    ),
    confirm_user_data: Path = typer.Option(
        ...,
        "--confirm-user-data",
        help="Exact normalized Electron userData root shown by inspection.",
    ),
    recovery_id: str | None = typer.Option(None, "--recovery-id", help="Selected recovery UUID."),
    after_parent_exit: bool = typer.Option(
        False,
        "--after-parent-exit",
        hidden=True,
        help="Wait for the Desktop parent pipe to close, then inspect again before delete-all.",
    ),
    expected_scope_fingerprint: str | None = typer.Option(
        None,
        "--expected-scope-fingerprint",
        hidden=True,
        help="Exact post-stop cleanup kind/path scope approved by Desktop.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the cleanup protocol."),
) -> None:
    """Apply a previously inspected cleanup after exact path confirmation."""

    if after_parent_exit:
        if mode != "delete-all-user-data" or os.environ.get("OPENSQUILLA_RECOVERY_OFFLINE") != "1":
            raise typer.BadParameter(
                "--after-parent-exit is reserved for offline Desktop delete-all"
            )
        if (
            expected_scope_fingerprint is None
            or len(expected_scope_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in expected_scope_fingerprint)
        ):
            raise typer.BadParameter(
                "--after-parent-exit requires the approved cleanup scope fingerprint"
            )
        # Electron writes the exact user-approved kind/path scope but keeps the
        # pipe open. Reading the complete bounded payload therefore blocks until
        # parent exit has released Chromium/userData handles and delivered EOF.
        approved_scope = _read_parent_cleanup_approval(
            user_data=user_data,
            expected_fingerprint=expected_scope_fingerprint,
        )
        refreshed = cleanup_inspect(
            user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
        )
        if refreshed.outcome != "ready":
            _cleanup_exit(refreshed, json_output=json_output)
            return
        refreshed_scope = frozenset(_cleanup_scope_pair(item) for item in refreshed.items)
        if not refreshed_scope.issubset(approved_scope):
            _cleanup_exit(
                replace(
                    refreshed,
                    outcome="blocked",
                    stable_code="cleanup_scope_changed",
                ),
                json_output=json_output,
            )
            return
        transaction_id = refreshed.transaction_id
        expected_revision = refreshed.revision
    elif expected_scope_fingerprint is not None:
        raise typer.BadParameter(
            "--expected-scope-fingerprint is reserved for post-exit Desktop cleanup"
        )

    report = cleanup_apply(
        user_data,
        mode=mode,
        profile_kind=profile_kind,
        transaction_id=transaction_id,
        expected_revision=expected_revision,
        confirm_user_data=confirm_user_data,
        recovery_id=recovery_id,
    )
    _cleanup_exit(report, json_output=json_output)


__all__ = ["recovery_app"]
