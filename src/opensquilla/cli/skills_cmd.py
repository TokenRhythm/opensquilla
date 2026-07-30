"""CLI commands for skill management."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table

from opensquilla.cli.gateway_rpc import (
    default_gateway_token,
    default_gateway_url,
    rpc_error_exit_code,
    run_gateway_sync,
)
from opensquilla.cli.output import emit_error, print_json
from opensquilla.cli.ui import ACCENT, console

skills_app = typer.Typer(help="Skill management - list, search, install, uninstall.")


def _install_result_payload(result: Any) -> dict[str, Any]:
    payload = dict(result) if isinstance(result, dict) else asdict(result)
    scan = payload.get("scan")
    if scan is None:
        payload.pop("scan", None)
    return payload


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
        await client.connect(default_gateway_url())
    except (SystemExit, ConnectionError, OSError):
        await client.close()
        return None

    try:
        payload = await client.call(method, params)
    except gateway_client_module.GatewayRPCError as exc:
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


def _load_skill_rows() -> list[dict[str, Any]]:
    import os
    from pathlib import Path

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
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
    ctx = EligibilityContext.auto()
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
    """List all installed/available skills."""
    rows = _load_skill_rows()
    if json_output:
        print_json(rows)
        return

    table = Table(title=f"Skills ({len(rows)})")
    table.add_column("Name", style=ACCENT)
    table.add_column("Layer")
    table.add_column("Eligible")
    table.add_column("Description")

    for row in rows:
        table.add_row(
            row["name"],
            row["layer"],
            "[green]yes[/]" if row["eligible"] else "[dim]no[/]",
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
) -> None:
    """Search for skills across Community sources."""

    async def _search() -> None:
        from opensquilla.skills.hub.defaults import get_default_skill_router

        router = get_default_skill_router()
        results = await router.search(query, limit=20)

        if json_output:
            print_json([asdict(result) for result in results])
            return

        if not results:
            console.print(f"[dim]No results for '{query}'[/]")
            return

        table = Table(title=f"Search: {query}")
        table.add_column("Name", style=ACCENT)
        table.add_column("Source")
        table.add_column("Trust")
        table.add_column("Description")

        for r in results:
            table.add_row(r.name, r.source_id, r.trust_level, r.description[:60])
        console.print(table)

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
    all_skills: bool = typer.Option(False, "--all", help="Update all managed skills"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Update one managed skill, or all managed skills."""
    if bool(name) == all_skills:
        raise typer.BadParameter("provide exactly one of NAME or --all")

    async def _run(client):
        params = {} if all_skills else {"name": name}
        return await client.call("skills.update", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    results = payload.get("results", []) if isinstance(payload, dict) else []
    failures = [r for r in results if isinstance(r, dict) and not r.get("success", False)]
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
            ok = bool(row.get("success", False))
            table.add_row(
                str(row.get("name") or ""),
                "[green]ok[/]" if ok else "[red]failed[/]",
                str(row.get("message") or ""),
            )
        console.print(table)
        message = payload.get("message") if isinstance(payload, dict) else None
        if message:
            console.print(str(message))
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
            "owner/repo:path, or GitHub URLs."
        ),
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Force install (skip security block)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Install a skill from a Community source."""

    async def _install() -> None:
        payload = await _try_gateway_skill_mutation(
            "skills.install",
            {"identifier": identifier, "source": source, "force": force},
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

        from opensquilla.skills.hub.defaults import build_default_skill_installer

        installer = build_default_skill_installer()

        if not json_output:
            console.print(f"Installing '{identifier}' from {source}...")
        result = await installer.install(identifier, source, force=force)

        if json_output:
            print_json(_install_result_payload(result))
            if not result.success:
                raise typer.Exit(1)
            return

        if result.success:
            console.print(f"[green]Installed:[/] {result.name} → {result.path}")
            if result.scan and result.scan.verdict != "safe":
                scan = result.scan
                console.print(
                    f"[yellow]Security: {scan.verdict} ({len(scan.findings)} findings)[/]"
                )
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            raise typer.Exit(1)

    asyncio.run(_install())


@skills_app.command("uninstall")
def skills_uninstall(
    name: str = typer.Argument(..., help="Skill name to remove"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Uninstall a managed skill."""

    async def _uninstall() -> None:
        payload = await _try_gateway_skill_mutation(
            "skills.uninstall",
            {"name": name},
            json_output=json_output,
        )
        if payload is not None:
            _emit_skill_mutation_result(
                payload,
                json_output=json_output,
                success_label="Uninstalled",
                fallback_name=name,
            )
            return

        from opensquilla.skills.hub.defaults import build_default_skill_installer

        installer = build_default_skill_installer()
        result = await installer.uninstall(name)

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
