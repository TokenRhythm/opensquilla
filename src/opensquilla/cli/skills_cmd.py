"""CLI commands for skill management."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opensquilla.cli.gateway_rpc import (
    default_gateway_token,
    default_gateway_url,
    rpc_error_exit_code,
    run_gateway_sync,
)
from opensquilla.cli.output import emit_error, print_json
from opensquilla.cli.ui import ACCENT, console
from opensquilla.skills.hub.router import search_router_with_diagnostics

skills_app = typer.Typer(help="Skill management - list, search, install, uninstall.")


def _install_result_payload(result: Any) -> dict[str, Any]:
    serializer = getattr(result, "to_dict", None)
    if callable(serializer):
        payload = dict(serializer())
    else:
        payload = dict(result) if isinstance(result, dict) else asdict(result)
    scan = payload.get("scan")
    if scan is None:
        payload.pop("scan", None)
    return payload


def _risk_confirmation_token(payload: dict[str, Any]) -> str:
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list):
        return ""
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("code") != "SCAN_CONFIRMATION_REQUIRED":
            continue
        details = diagnostic.get("details")
        if not isinstance(details, dict):
            return ""
        token = details.get("confirmationToken")
        return token.strip() if isinstance(token, str) else ""
    return ""


def _print_risk_confirmation_hint(payload: dict[str, Any], *, name: str = "") -> None:
    token = _risk_confirmation_token(payload)
    if not token:
        return
    target = f" for {name!r}" if name else ""
    console.print(
        "[yellow]Review the scanner findings, then retry"
        f"{target} with --force --risk-confirmation {token}[/]"
    )


async def _try_gateway_skill_mutation(
    method: str,
    params: dict[str, Any],
    *,
    json_output: bool,
) -> dict[str, Any] | None:
    """Use the running gateway when available; return None only for connect failures."""

    from opensquilla.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        await client.connect(
            default_gateway_url(),
            token=default_gateway_token(),
        )
    except SystemExit as exc:
        await client.close()
        message = str(exc)
        if message.startswith("Cannot connect to OpenSquilla gateway at "):
            return None
        # A malformed/auth-rejected handshake proves that something is
        # listening. Never race that Gateway with an offline profile writer.
        emit_error(message, json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    except (ConnectionError, OSError):
        await client.close()
        return None

    try:
        payload = await client.call(method, params)
    except gateway_client_module.GatewayRPCError as exc:
        if method == "skills.doctor" and str(exc.code or "").upper() == "METHOD_NOT_FOUND":
            message = (
                "The running Gateway does not support Skill Doctor. Upgrade or restart "
                "the Gateway with this OpenSquilla version, then retry."
            )
            emit_error(
                message,
                json_output=json_output,
                code="GATEWAY_UPGRADE_REQUIRED",
                details={
                    "method": method,
                    "gatewayCode": "METHOD_NOT_FOUND",
                    "hint": (
                        "Restart the Gateway from the same upgraded OpenSquilla "
                        "installation, then run skills doctor again."
                    ),
                },
            )
            # A reachable Gateway owns the live profile. Even though Doctor is
            # read-only, do not race it with an offline catalog observation.
            raise typer.Exit(1) from exc
        emit_error(
            exc.message,
            json_output=json_output,
            code=exc.code,
            details=exc.data,
        )
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(str(exc), json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    finally:
        await client.close()

    return payload if isinstance(payload, dict) else {"result": payload}


def _emit_skill_mutation_result(
    payload: dict[str, Any],
    *,
    json_output: bool,
    success_label: str,
    fallback_name: str,
) -> None:
    success = bool(payload.get("success", False))
    if json_output:
        print_json(payload)
        if not success:
            raise typer.Exit(1)
        return

    name = str(payload.get("name") or fallback_name)
    message = str(payload.get("message") or "")
    if success:
        path = payload.get("path")
        suffix = f" -> {path}" if path else ""
        console.print(f"[green]{success_label}:[/] {name}{suffix}")
        if message:
            console.print(message)
        return

    console.print(f"[red]Failed:[/] {message or name}")
    _print_risk_confirmation_hint(payload, name=name)
    raise typer.Exit(1)


async def _reload_running_skill_catalog(*, json_output: bool) -> dict[str, Any]:
    """Call the running Gateway without an offline fallback."""
    from opensquilla.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        await client.connect(default_gateway_url(), token=default_gateway_token())
        payload = await client.call("skills.reload", {})
    except SystemExit as exc:
        emit_error(
            f"Gateway unavailable: {exc}. The running Skill catalog was not refreshed.",
            json_output=json_output,
            code="GATEWAY_UNAVAILABLE",
        )
        raise typer.Exit(1) from exc
    except gateway_client_module.GatewayRPCError as exc:
        emit_error(
            f"{exc.message} The running Skill catalog was not refreshed.",
            json_output=json_output,
            code=exc.code,
            details=exc.data,
        )
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(
            f"Gateway unavailable: {exc}. The running Skill catalog was not refreshed.",
            json_output=json_output,
            code="GATEWAY_UNAVAILABLE",
        )
        raise typer.Exit(1) from exc
    finally:
        await client.close()

    return payload if isinstance(payload, dict) else {
        "success": False,
        "changed": False,
        "partial": False,
        "generation": 0,
        "added": [],
        "removed": [],
        "modified": [],
        "errors": [
            {
                "name": "",
                "path": "",
                "message": "Gateway returned an invalid skills.reload response",
                "kept_previous": False,
            }
        ],
    }


def _build_offline_skill_loader() -> tuple[Any, Any]:
    import os
    from pathlib import Path

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.paths import resolve_skill_layer_dirs

    config = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    workspace_root = Path(config.workspace_dir) if config.workspace_dir else None
    workspace_override = Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
    layer_dirs = resolve_skill_layer_dirs(
        allow_bundled=config.skills.allow_bundled,
        workspace_root=workspace_root,
        workspace_override=workspace_override,
        managed_override=config.skills.managed_dir,
        extra_dirs=[Path(d) for d in config.skills.extra_dirs],
    )
    loader = SkillLoader(
        bundled_dir=layer_dirs.bundled_dir,
        workspace_dir=layer_dirs.workspace_dir,
        managed_dir=layer_dirs.managed_dir,
        personal_agents_dir=layer_dirs.personal_agents_dir,
        project_agents_dir=layer_dirs.project_agents_dir,
        extra_dirs=layer_dirs.extra_dirs,
    )
    return config, loader


def _load_skill_rows(
    *,
    config: Any | None = None,
    loader: Any | None = None,
) -> list[dict[str, Any]]:
    """Build legacy CLI rows from a lock-protected offline catalog scan.

    Offline eligibility only describes locally observable requirements.  It
    must not be presented as evidence that a running Gateway published the
    candidate, so the additive runtime fields remain explicitly inactive.
    """

    from opensquilla.skills.eligibility import (
        check_eligibility,
        eligibility_context_for_skills_config,
    )

    if config is None or loader is None:
        config, loader = _build_offline_skill_loader()
    ctx = eligibility_context_for_skills_config(config.skills)
    rows: list[dict[str, Any]] = []
    for skill in sorted(loader.get_user_invocable(), key=lambda x: x.name):
        provenance = getattr(skill, "provenance", None)
        rows.append(
            {
                "name": skill.name,
                "layer": skill.layer.value,
                "eligible": check_eligibility(skill, ctx),
                "description": skill.description,
                "always": skill.always,
                "triggers": list(skill.triggers),
                "path": str(skill.path) if skill.path is not None else "",
                "filePath": skill.file_path,
                "baseDir": skill.base_dir,
                "homepage": skill.homepage,
                "userInvocable": skill.user_invocable,
                "disableModelInvocation": skill.disable_model_invocation,
                "active": False,
                "available": False,
                "catalogState": "validated_offline",
                "effectiveFrom": "next_start",
                "provenance": {
                    "origin": provenance.origin if provenance else "unknown",
                    "license": provenance.license if provenance else "unknown",
                    "upstreamUrl": provenance.upstream_url if provenance else "",
                    "maintainedBy": provenance.maintained_by
                    if provenance
                    else "OpenSquilla",
                },
            }
        )
    return rows


def _gateway_skill_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt ``skills.list`` to the long-standing CLI JSON list shape."""

    raw_rows = payload.get("skills")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise ValueError("Gateway returned an invalid skills.list response")

    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        row = dict(raw_row)
        file_path = str(row.get("filePath") or row.get("file_path") or "")
        base_dir = str(row.get("baseDir") or row.get("base_dir") or "")
        if not base_dir and file_path:
            base_dir = _wire_parent_path(file_path)
        provenance_raw = row.get("provenance")
        provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
        eligible = bool(row.get("eligible", False))
        rows.append(
            {
                "name": str(row.get("name") or ""),
                "layer": str(row.get("layer") or ""),
                "eligible": eligible,
                "description": str(row.get("description") or ""),
                "always": bool(row.get("always", False)),
                "triggers": list(row.get("triggers") or []),
                "path": str(row.get("path") or base_dir),
                "filePath": file_path,
                "baseDir": base_dir,
                "homepage": str(row.get("homepage") or ""),
                "userInvocable": bool(
                    row.get("userInvocable", row.get("user_invocable", True))
                ),
                "disableModelInvocation": bool(
                    row.get("disableModelInvocation")
                    or row.get("disable_model_invocation", False)
                ),
                "active": True,
                "available": eligible,
                "catalogState": "live",
                "provenance": {
                    "origin": str(provenance.get("origin") or "unknown"),
                    "license": str(provenance.get("license") or "unknown"),
                    "upstreamUrl": str(
                        provenance.get("upstreamUrl") or provenance.get("upstream_url") or ""
                    ),
                    "maintainedBy": str(
                        provenance.get("maintainedBy")
                        or provenance.get("maintained_by")
                        or "OpenSquilla"
                    ),
                },
            }
        )
    return rows


def _wire_parent_path(file_path: str) -> str:
    """Derive a parent without rewriting Gateway path separators for this host."""

    separator_index = max(file_path.rfind("/"), file_path.rfind("\\"))
    if separator_index < 0:
        return "."
    if separator_index == 0:
        return file_path[0]
    if separator_index == 2 and file_path[1:2] == ":":
        return file_path[:3]
    return file_path[:separator_index]


class _OfflineSkillListBlockedError(RuntimeError):
    """Stable local failure raised before an unsafe offline catalog scan."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _pending_skill_journal(config: Any, loader: Any) -> Path | None:
    """Return the selected transaction journal when any entry occupies it."""

    from opensquilla.skills.hub.transaction import journal_path_for_state, path_is_occupied

    managed_dir = getattr(loader, "managed_dir", None)
    if managed_dir is None:
        return None
    configured_state = str(getattr(config, "state_dir", "") or "").strip()
    state_root = Path(configured_state) if configured_state else None
    journal_path = journal_path_for_state(Path(managed_dir), state_root)
    try:
        return journal_path if path_is_occupied(journal_path) else None
    except OSError as exc:
        raise _OfflineSkillListBlockedError(
            "The managed Skill transaction journal could not be inspected safely",
            code="SKILL_RECOVERY_REQUIRED",
            details={"journal": str(journal_path)},
        ) from exc


def _offline_management_service() -> tuple[Any, Any]:
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.hub.defaults import build_default_skill_installer
    from opensquilla.skills.hub.transaction import journal_path_for_state

    config, loader = _build_offline_skill_loader()
    managed_dir = loader.managed_dir
    if managed_dir is None:
        raise RuntimeError("No managed Skill directory is configured")
    configured_state = str(getattr(config, "state_dir", "") or "").strip()
    state_root = Path(configured_state) if configured_state else None
    service = build_default_skill_installer(
        managed_dir=managed_dir,
        loader=None,
        journal_path=journal_path_for_state(managed_dir, state_root),
        offline=True,
    )
    return service, default_opensquilla_home()


def _recover_offline_management_service(service: Any) -> None:
    """Run filesystem-only recovery after the CLI has acquired the profile lease."""

    recover = getattr(service, "recover_offline_store", None)
    if callable(recover):
        recover()


def inspect_compiled_dag(*, name: str, bundled_dir: Path | None = None) -> str:
    """Return the compiled composition for a meta-skill as YAML text.

    Helper used by both the CLI command and tests; isolating the logic
    keeps the Typer command body minimal and verifiable.
    """

    import yaml as _yaml

    from opensquilla.skills.loader import SkillLoader

    if bundled_dir is not None:
        # Explicit bundled_dir keeps the test-friendly single-layer path.
        loader = SkillLoader(bundled_dir=bundled_dir)
    else:
        # Resolve every skill layer so managed/workspace meta-skills (e.g.
        # user-installed ones under ~/.opensquilla/skills) are inspectable,
        # matching `skills list`.
        import os as _os

        from opensquilla.gateway.config import GatewayConfig
        from opensquilla.skills.paths import resolve_skill_layer_dirs

        config = GatewayConfig.load(_os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
        workspace_root = Path(config.workspace_dir) if config.workspace_dir else None
        workspace_override = (
            Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
        )
        layer_dirs = resolve_skill_layer_dirs(
            allow_bundled=config.skills.allow_bundled,
            workspace_root=workspace_root,
            workspace_override=workspace_override,
            managed_override=config.skills.managed_dir,
            extra_dirs=[Path(d) for d in config.skills.extra_dirs],
        )
        loader = SkillLoader(
            bundled_dir=layer_dirs.bundled_dir,
            workspace_dir=layer_dirs.workspace_dir,
            managed_dir=layer_dirs.managed_dir,
            personal_agents_dir=layer_dirs.personal_agents_dir,
            project_agents_dir=layer_dirs.project_agents_dir,
            extra_dirs=layer_dirs.extra_dirs,
        )
    loader.invalidate_cache()
    loader.load_all()
    spec = loader.get_by_name(name)
    if spec is None:
        return f"skill {name!r} not loaded"
    if spec.composition_raw is None:
        return f"skill {name!r} has no composition (not a meta skill)"
    return str(_yaml.safe_dump(spec.composition_raw, sort_keys=False))


@skills_app.command("list")
def skills_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List the live Gateway catalog, or validate local Skills offline."""

    async def _list() -> tuple[list[dict[str, Any]], bool]:
        payload = await _try_gateway_skill_mutation(
            "skills.list",
            {},
            json_output=json_output,
        )
        if payload is not None:
            try:
                return _gateway_skill_rows(payload), True
            except ValueError as exc:
                raise _OfflineSkillListBlockedError(
                    str(exc),
                    code="GATEWAY_INVALID_RESPONSE",
                ) from exc

        # The connection probe above established that the configured Gateway is
        # unreachable. Bind the complete local observation to the profile writer
        # lease so a starting Gateway or offline transaction cannot publish a
        # different tree while this process scans it.
        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import ProfileOperationLock
        from opensquilla.recovery.errors import ProfileLockBusyError

        try:
            with ProfileOperationLock(default_opensquilla_home()):
                config, loader = _build_offline_skill_loader()
                journal_path = _pending_skill_journal(config, loader)
                if journal_path is not None:
                    raise _OfflineSkillListBlockedError(
                        "Managed Skill transaction recovery is pending; "
                        "refusing an offline catalog scan",
                        code="SKILL_RECOVERY_REQUIRED",
                        details={"journal": str(journal_path)},
                    )
                return _load_skill_rows(config=config, loader=loader), False
        except ProfileLockBusyError as exc:
            raise _OfflineSkillListBlockedError(
                "The Gateway is unreachable, but another process is using this profile; "
                "refusing an offline catalog scan",
                code="PROFILE_IN_USE",
            ) from exc

    try:
        rows, live_catalog = asyncio.run(_list())
    except _OfflineSkillListBlockedError as exc:
        emit_error(
            str(exc),
            json_output=json_output,
            code=exc.code,
            details=exc.details or None,
        )
        raise typer.Exit(1) from exc

    if json_output:
        print_json(rows)
        return

    if not live_catalog:
        console.print(
            "[yellow]Validated offline only:[/] no running Gateway catalog was inspected; "
            "these Skills are not reported as active or available until the next start."
        )
    table = Table(
        title=(
            f"Skills ({len(rows)})"
            if live_catalog
            else f"Skills ({len(rows)}, validated offline)"
        )
    )
    table.add_column("Name", style=ACCENT)
    table.add_column("Layer")
    table.add_column("Eligible")
    table.add_column("Description")

    for row in rows:
        table.add_row(
            row["name"],
            row["layer"],
            (
                "[green]yes[/]"
                if live_catalog and row["eligible"]
                else "[dim]no[/]"
                if live_catalog
                else "[green]ready (offline)[/]"
                if row["eligible"]
                else "[dim]needs setup (offline)[/]"
            ),
            (
                row["description"][:60] + "..."
                if len(row["description"]) > 60
                else row["description"]
            ),
        )
    console.print(table)


@skills_app.command("search")
def skills_search(
    query: str = typer.Argument(..., help="Search query"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    include_diagnostics: bool = typer.Option(
        False,
        "--include-diagnostics",
        help="With --json, wrap results and source diagnostics in an object",
    ),
) -> None:
    """Search for skills across Community sources."""

    if include_diagnostics and not json_output:
        raise typer.BadParameter("--include-diagnostics requires --json")

    async def _search() -> None:
        from opensquilla.skills.hub.defaults import get_default_skill_router

        router = get_default_skill_router()
        report = await search_router_with_diagnostics(router, query, limit=20)
        results = report.results

        if json_output:
            rows = [asdict(result) for result in results]
            if not include_diagnostics:
                print_json(rows)
                return
            print_json(
                {
                    "results": rows,
                    "diagnostics": [item.to_dict() for item in report.diagnostics],
                    "partial": report.partial,
                    "allSourcesUnavailable": report.all_sources_unavailable,
                }
            )
            return

        if not results and not report.diagnostics:
            console.print(f"[dim]No results for '{query}'[/]")
            return

        if results:
            table = Table(title=f"Search: {query}")
            table.add_column("Name", style=ACCENT)
            table.add_column("Source")
            table.add_column("Trust")
            table.add_column("Description")

            for r in results:
                table.add_row(r.name, r.source_id, r.trust_level, r.description[:60])
            console.print(table)

        for diagnostic in report.diagnostics:
            line = Text()
            line.append(f"{diagnostic.code}: ", style="yellow" if results else "red")
            line.append(diagnostic.message)
            retry_after = diagnostic.details.get("retryAfter")
            if isinstance(retry_after, (str, int)) and str(retry_after):
                line.append(f" Retry after: {str(retry_after)[:80]}.")
            console.print(line)
            if diagnostic.hint:
                console.print(Text(diagnostic.hint, style="dim"))

    asyncio.run(_search())


@skills_app.command("view")
def skills_view(
    name: str = typer.Argument(..., help="Skill name"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect a single skill from the running gateway."""

    async def _run(client):
        return await client.call("skills.get", {"name": name})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Skill: {payload.get('name', name)}")
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for key in (
        "name",
        "layer",
        "eligible",
        "description",
        "file_path",
        "base_dir",
        "homepage",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            table.add_row(key, str(value))
    console.print(table)
    content = str(payload.get("content") or "")
    if content:
        preview = content if len(content) <= 1200 else content[:1200] + "\n..."
        console.print(Panel(preview, title="Content", expand=False))


@skills_app.command("update")
def skills_update(
    name: str | None = typer.Argument(None, help="Skill name to update"),
    install_id: str = typer.Option(
        "",
        "--install-id",
        help="Exact managed install identity to update",
    ),
    all_skills: bool = typer.Option(False, "--all", help="Update all managed skills"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Accept a dangerous scanner verdict for this update only",
    ),
    risk_confirmation: str = typer.Option(
        "",
        "--risk-confirmation",
        help="Exact confirmationToken returned for the reviewed update artifact",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Update one managed skill, or all managed skills."""
    if sum((bool(name), bool(install_id), all_skills)) != 1:
        raise typer.BadParameter("provide exactly one of NAME, --install-id, or --all")
    if risk_confirmation and not force:
        raise typer.BadParameter("--risk-confirmation requires --force")

    async def _update() -> dict[str, Any]:
        params: dict[str, Any] = (
            {}
            if all_skills
            else {"installId": install_id}
            if install_id
            else {"name": name}
        )
        if force:
            params["force"] = True
        if risk_confirmation:
            params["riskConfirmation"] = risk_confirmation
        payload = await _try_gateway_skill_mutation(
            "skills.update",
            params,
            json_output=json_output,
        )
        if payload is not None:
            return payload

        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import ProfileOperationLock
        from opensquilla.skills.hub.installer import (
            supported_keyword_arguments,
            supports_keyword_argument,
            unsupported_installer_result,
        )

        profile_home = default_opensquilla_home()
        with ProfileOperationLock(profile_home):
            service, _ = _offline_management_service()
            _recover_offline_management_service(service)
            update = service.update
            if install_id and not supports_keyword_argument(update, "install_id"):
                unsupported = unsupported_installer_result(
                    operation="update",
                    capability="installId",
                    name=str(name or ""),
                    install_id=install_id,
                )
                return {**_install_result_payload(unsupported), "results": []}
            if force and not supports_keyword_argument(update, "force"):
                unsupported = unsupported_installer_result(
                    operation="update",
                    capability="force",
                    name=str(name or ""),
                    install_id=install_id,
                )
                return {**_install_result_payload(unsupported), "results": []}
            if force and not supports_keyword_argument(update, "risk_confirmation"):
                unsupported = unsupported_installer_result(
                    operation="update",
                    capability="riskConfirmation",
                    name=str(name or ""),
                    install_id=install_id,
                )
                return {**_install_result_payload(unsupported), "results": []}
            update_kwargs = supported_keyword_arguments(
                update,
                {
                    "install_id": install_id,
                    "force": force,
                    "risk_confirmation": risk_confirmation,
                },
            )
            offline_results = await update(
                None if all_skills or install_id else name,
                **update_kwargs,
            )
        return {
            "results": [_install_result_payload(result) for result in offline_results],
        }

    payload = asyncio.run(_update())
    results = payload.get("results", []) if isinstance(payload, dict) else []
    failures = [
        r
        for r in results
        if isinstance(r, dict)
        and not r.get("success", False)
        and not r.get("unchanged", False)
    ]
    top_level_failure = isinstance(payload, dict) and payload.get("success") is False
    if json_output:
        print_json(payload)
    else:
        table = Table(title="Skill updates")
        table.add_column("Name", style=ACCENT)
        table.add_column("Status")
        table.add_column("Message")
        for row in results:
            if not isinstance(row, dict):
                continue
            unchanged = bool(row.get("unchanged", False))
            ok = bool(row.get("success", False)) or unchanged
            table.add_row(
                str(row.get("name") or ""),
                "[dim]unchanged[/]"
                if unchanged
                else "[green]ok[/]"
                if ok
                else "[red]failed[/]",
                str(row.get("message") or ""),
            )
        console.print(table)
        message = payload.get("message") if isinstance(payload, dict) else None
        if message:
            console.print(str(message))
        for row in results:
            if isinstance(row, dict):
                _print_risk_confirmation_hint(
                    row,
                    name=str(row.get("name") or name or install_id),
                )
    if failures or top_level_failure:
        raise typer.Exit(1)


@skills_app.command("reload")
def skills_reload(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Force the running Gateway to reload its Skill catalog."""
    payload = asyncio.run(_reload_running_skill_catalog(json_output=json_output))

    if json_output:
        print_json(payload)
        if payload.get("success") is not True:
            raise typer.Exit(1)
        return

    generation = int(payload.get("generation") or 0)
    if payload.get("success") is not True:
        typer.echo(
            f"Skill catalog reload failed; generation {generation} remains active.",
            err=True,
        )
        for error in payload.get("errors") or []:
            if isinstance(error, dict) and error.get("message"):
                typer.echo(f"- {error['message']}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Skill catalog generation {generation} is active.")
    changes = [
        ("Added", payload.get("added") or []),
        ("Removed", payload.get("removed") or []),
        ("Modified", payload.get("modified") or []),
    ]
    if payload.get("changed"):
        for label, names in changes:
            if names:
                typer.echo(f"{label}: {', '.join(str(name) for name in names)}")
    else:
        typer.echo("No Skill catalog changes detected.")

    errors = payload.get("errors") or []
    if payload.get("partial"):
        typer.echo(f"Warning: reload completed with {len(errors)} Skill error(s).", err=True)
        for error in errors:
            if not isinstance(error, dict):
                continue
            name = str(error.get("name") or error.get("path") or "Skill")
            typer.echo(f"- {name}: {error.get('message', 'load failed')}", err=True)


@skills_app.command("install")
def skills_install(
    identifier: str = typer.Argument(..., help="Skill name or identifier"),
    source: str = typer.Option(
        "clawhub",
        "--source",
        "-s",
        help=(
            "Source (clawhub, github). GitHub accepts owner/repo, "
            "owner/repo@ref:path, or GitHub URLs."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Accept a dangerous scanner verdict; path, schema, and postflight checks still apply",
    ),
    risk_confirmation: str = typer.Option(
        "",
        "--risk-confirmation",
        help="Exact confirmationToken returned for the reviewed install artifact",
    ),
    replace_source: bool = typer.Option(
        False,
        "--replace-source",
        help="Explicitly replace a same-name install tracked from another source",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Install a skill from a Community source."""

    if risk_confirmation and not force:
        raise typer.BadParameter("--risk-confirmation requires --force")

    async def _install() -> None:
        rpc_params: dict[str, Any] = {
            "identifier": identifier,
            "source": source,
            "force": force,
        }
        if replace_source:
            rpc_params["replaceSource"] = True
        if risk_confirmation:
            rpc_params["riskConfirmation"] = risk_confirmation
        payload = await _try_gateway_skill_mutation(
            "skills.install",
            rpc_params,
            json_output=json_output,
        )
        if payload is not None:
            _emit_skill_mutation_result(
                payload,
                json_output=json_output,
                success_label="Installed",
                fallback_name=identifier,
            )
            return

        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import ProfileOperationLock
        from opensquilla.skills.hub.installer import (
            supported_keyword_arguments,
            supports_keyword_argument,
            unsupported_installer_result,
        )

        profile_home = default_opensquilla_home()

        if not json_output:
            console.print(f"Installing '{identifier}' from {source}...")
        with ProfileOperationLock(profile_home):
            installer, _ = _offline_management_service()
            _recover_offline_management_service(installer)
            install = installer.install
            if replace_source and not supports_keyword_argument(install, "replace_source"):
                result = unsupported_installer_result(
                    operation="install",
                    capability="replaceSource",
                    name=identifier,
                )
            elif force and not supports_keyword_argument(install, "force"):
                result = unsupported_installer_result(
                    operation="install",
                    capability="force",
                    name=identifier,
                )
            elif force and not supports_keyword_argument(install, "risk_confirmation"):
                result = unsupported_installer_result(
                    operation="install",
                    capability="riskConfirmation",
                    name=identifier,
                )
            else:
                install_kwargs = supported_keyword_arguments(
                    install,
                    {
                        "force": force,
                        "replace_source": replace_source,
                        "risk_confirmation": risk_confirmation,
                    },
                )
                result = await install(
                    identifier,
                    source,
                    **install_kwargs,
                )

        if json_output:
            print_json(_install_result_payload(result))
            if not result.success:
                raise typer.Exit(1)
            return

        if result.success:
            console.print(f"[green]Installed:[/] {result.name} → {result.path}")
            if result.message:
                console.print(result.message)
            if result.scan and result.scan.verdict != "safe":
                scan = result.scan
                console.print(
                    f"[yellow]Security: {scan.verdict} ({len(scan.findings)} findings)[/]"
                )
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            _print_risk_confirmation_hint(
                _install_result_payload(result),
                name=identifier,
            )
            raise typer.Exit(1)

    asyncio.run(_install())


@skills_app.command("uninstall")
def skills_uninstall(
    name: str | None = typer.Argument(None, help="Skill name to remove"),
    install_id: str = typer.Option(
        "",
        "--install-id",
        help="Exact managed install identity to remove",
    ),
    allow_drift: bool = typer.Option(
        False,
        "--allow-drift",
        help="Confirm removal of a tracked Skill with local file changes",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Uninstall a managed skill."""
    if bool(name) == bool(install_id):
        raise typer.BadParameter("provide exactly one of NAME or --install-id")

    async def _uninstall() -> None:
        rpc_params: dict[str, Any] = (
            {"installId": install_id} if install_id else {"name": name}
        )
        if allow_drift:
            rpc_params["allowDrift"] = True
        payload = await _try_gateway_skill_mutation(
            "skills.uninstall",
            rpc_params,
            json_output=json_output,
        )
        if payload is not None:
            _emit_skill_mutation_result(
                payload,
                json_output=json_output,
                success_label="Uninstalled",
                fallback_name=name or install_id,
            )
            return

        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import ProfileOperationLock
        from opensquilla.skills.hub.installer import (
            supported_keyword_arguments,
            supports_keyword_argument,
            unsupported_installer_result,
        )

        profile_home = default_opensquilla_home()
        with ProfileOperationLock(profile_home):
            installer, _ = _offline_management_service()
            _recover_offline_management_service(installer)
            uninstall = installer.uninstall
            if install_id and not supports_keyword_argument(uninstall, "install_id"):
                result = unsupported_installer_result(
                    operation="uninstall",
                    capability="installId",
                    name=name or "",
                    install_id=install_id,
                )
            elif allow_drift and not supports_keyword_argument(uninstall, "allow_drift"):
                result = unsupported_installer_result(
                    operation="uninstall",
                    capability="allowDrift",
                    name=name or "",
                    install_id=install_id,
                )
            else:
                uninstall_kwargs = supported_keyword_arguments(
                    uninstall,
                    {"install_id": install_id, "allow_drift": allow_drift},
                )
                uninstall_name = "" if install_id else name or ""
                result = await uninstall(uninstall_name, **uninstall_kwargs)

        if json_output:
            print_json(_install_result_payload(result))
            if not result.success:
                raise typer.Exit(1)
            return

        if result.success:
            console.print(f"[green]Uninstalled:[/] {result.name}")
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            raise typer.Exit(1)

    asyncio.run(_uninstall())


@skills_app.command("doctor")
def skills_doctor(
    name: str | None = typer.Argument(None, help="Skill name or install id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Diagnose local Community Skill install, load, and readiness state."""

    async def _doctor() -> dict[str, Any]:
        params = {"name": name} if name else {}
        payload = await _try_gateway_skill_mutation(
            "skills.doctor",
            params,
            json_output=json_output,
        )
        if payload is not None:
            return payload

        from opensquilla.paths import default_opensquilla_home
        from opensquilla.profile_operation_lock import ProfileOperationLock
        from opensquilla.recovery.errors import ProfileLockBusyError
        from opensquilla.skills.eligibility import eligibility_context_for_skills_config
        from opensquilla.skills.hub.doctor import SkillDoctor
        from opensquilla.skills.hub.transaction import journal_path_for_state

        try:
            # Bind config resolution, loader construction, lockfile/journal
            # inspection, and the complete tree scan to one profile generation.
            with ProfileOperationLock(default_opensquilla_home()):
                config, loader = _build_offline_skill_loader()
                if loader.managed_dir is None:
                    return {
                        "ok": False,
                        "skills": [],
                        "diagnostics": [
                            {
                                "code": "MANAGED_ROOT_UNAVAILABLE",
                                "severity": "error",
                                "phase": "store",
                                "blocking": True,
                                "message": "No managed Skill directory is configured",
                                "hint": (
                                    "Configure the managed Skill directory before installing."
                                ),
                                "details": {},
                            }
                        ],
                    }
                configured_state = str(getattr(config, "state_dir", "") or "").strip()
                state_root = Path(configured_state) if configured_state else None
                return SkillDoctor(
                    managed_dir=loader.managed_dir,
                    lockfile_path=default_opensquilla_home() / "skills-lock.json",
                    loader=None,
                    journal_path=journal_path_for_state(loader.managed_dir, state_root),
                    eligibility_context=eligibility_context_for_skills_config(config.skills),
                ).doctor(name).to_dict()
        except ProfileLockBusyError as exc:
            raise _OfflineSkillListBlockedError(
                "The Gateway is unreachable, but another process is using this profile; "
                "refusing an offline Doctor scan",
                code="PROFILE_IN_USE",
            ) from exc

    try:
        payload = asyncio.run(_doctor())
    except _OfflineSkillListBlockedError as exc:
        emit_error(
            str(exc),
            json_output=json_output,
            code=exc.code,
            details=exc.details or None,
        )
        raise typer.Exit(1) from exc
    if json_output:
        print_json(payload)
    else:
        summary = payload.get("summary") if isinstance(payload, dict) else {}
        table = Table(title="Skill doctor")
        table.add_column("Name", style=ACCENT)
        table.add_column("Install")
        table.add_column("Load")
        table.add_column("Selection")
        table.add_column("Compatibility")
        table.add_column("Readiness")
        for row in payload.get("skills", []):
            if not isinstance(row, dict):
                continue
            lifecycle = row.get("lifecycle", {})
            table.add_row(
                str(row.get("name") or ""),
                str(lifecycle.get("install_state") or ""),
                str(lifecycle.get("load_state") or ""),
                str(lifecycle.get("selection_state") or ""),
                str(lifecycle.get("compatibility_state") or ""),
                str(lifecycle.get("readiness_state") or ""),
            )
        console.print(table)
        if isinstance(summary, dict):
            console.print(
                f"Checked {summary.get('checked', 0)} Skill(s); "
                f"{summary.get('blocking', 0)} blocking diagnostic(s)."
            )
        for diagnostic in payload.get("diagnostics", []):
            if isinstance(diagnostic, dict):
                console.print(
                    f"[{diagnostic.get('severity', 'info')}] "
                    f"{diagnostic.get('code', 'DIAGNOSTIC')}: "
                    f"{diagnostic.get('message', '')}"
                )
    if payload.get("ok") is False:
        raise typer.Exit(1)


# ── Meta-skill sub-commands ───────────────────────────────────────────────

from opensquilla.cli.skills_meta_cmd import meta_app  # noqa: E402

skills_app.add_typer(meta_app, name="meta")


# ── Tap sub-commands ──────────────────────────────────────────────────────

tap_app = typer.Typer(help="Manage custom skill source repositories (taps).")
skills_app.add_typer(tap_app, name="tap")


@tap_app.command("add")
def tap_add(owner_repo: str = typer.Argument(..., help="GitHub owner/repo")) -> None:
    """Add a custom skill source tap."""
    from opensquilla.skills.hub.taps import TapsManager

    try:
        mgr = TapsManager()
        tap = mgr.add(owner_repo)
        console.print(f"[green]Added tap:[/] {tap.full_name} ({tap.url})")
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")


@tap_app.command("list")
def tap_list() -> None:
    """List registered taps."""
    from opensquilla.skills.hub.taps import TapsManager

    mgr = TapsManager()
    taps = mgr.list()
    if not taps:
        console.print("[dim]No taps registered.[/]")
        return
    for t in taps:
        console.print(f"  {t.full_name}  {t.url}  (added {t.added_at})")


@tap_app.command("remove")
def tap_remove(owner_repo: str = typer.Argument(..., help="GitHub owner/repo")) -> None:
    """Remove a tap."""
    from opensquilla.skills.hub.taps import TapsManager

    mgr = TapsManager()
    if mgr.remove(owner_repo):
        console.print(f"[green]Removed:[/] {owner_repo}")
    else:
        console.print(f"[yellow]Not found:[/] {owner_repo}")


# ── Publish command ───────────────────────────────────────────────────────


@skills_app.command("publish")
def skills_publish(
    skill_dir: str = typer.Argument(..., help="Path to skill directory"),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Target repo (owner/repo) for PR"),
) -> None:
    """Validate and publish a skill to a repository."""
    from pathlib import Path

    async def _publish() -> None:
        from opensquilla.skills.hub.publisher import publish_skill

        result = await publish_skill(Path(skill_dir), target_repo=repo)
        if result.success:
            console.print(f"[green]OK:[/] {result.message}")
        else:
            console.print(f"[red]Failed:[/] {result.message}")

    asyncio.run(_publish())


@skills_app.command("inspect")
def cli_inspect(
    name: str = typer.Argument(..., help="Meta-skill name to inspect"),
) -> None:
    """Print the compiled composition.steps for a meta-skill."""

    typer.echo(inspect_compiled_dag(name=name))
