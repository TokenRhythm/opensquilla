"""CLI: opensquilla pet status/test/say (PRD F4 diagnostics).

These talk to the AgentRMC native bridge directly (loopback) so they work as
diagnostics regardless of whether the gateway's in-process pet service is
running. ``status`` also reports the ``[pet]`` configuration gate.
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console

from opensquilla.onboarding.config_store import default_config_path, load_config
from opensquilla.pet.bridge_client import BridgeClient

pet_app = typer.Typer(help="Inspect and test the desktop-pet (AgentRMC) bridge.")
console = Console()


def _load_pet_config():
    try:
        config = load_config(default_config_path())
    except Exception:  # noqa: BLE001 - diagnostics must not crash on bad config
        from opensquilla.gateway.config import PetConfig

        return PetConfig()
    return config.pet


def _client(cfg) -> BridgeClient:
    return BridgeClient(cfg.bridge_url, timeout_ms=cfg.request_timeout_ms)


@pet_app.command("status")
def pet_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show [pet] config and probe the native bridge /state."""
    cfg = _load_pet_config()

    async def _probe():
        client = _client(cfg)
        try:
            return await client.state()
        finally:
            await client.aclose()

    state = asyncio.run(_probe())
    reachable = state is not None
    if json_output:
        console.print_json(json.dumps({
            "enabled": cfg.enabled,
            "bridge_url": cfg.bridge_url,
            "dispatch_enabled": cfg.dispatch_enabled,
            "reachable": reachable,
            "state": state,
        }))
        return
    console.print(f"[pet].enabled      : {cfg.enabled}")
    console.print(f"bridge_url         : {cfg.bridge_url}")
    console.print(f"dispatch_enabled   : {cfg.dispatch_enabled}")
    console.print(f"bridge reachable   : {'yes' if reachable else 'no'}")
    if state is not None:
        console.print(f"state              : {json.dumps(state, ensure_ascii=False)}")
    if not cfg.enabled:
        console.print(
            "[yellow]Reminder service is off; set [pet].enabled = true to arm it.[/yellow]"
        )


@pet_app.command("test")
def pet_test(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Send a test notification to the pet (verifies the bridge wiring)."""
    cfg = _load_pet_config()

    async def _send() -> bool:
        client = _client(cfg)
        try:
            return await client.notify({
                "event": "message",
                "title": cfg.title,
                "message": "OpenSquilla：桌宠提醒链路测试",
            })
        finally:
            await client.aclose()

    ok = asyncio.run(_send())
    if json_output:
        console.print_json(json.dumps({"ok": ok}))
        return
    if ok:
        console.print("[green]Test notification delivered (204).[/green]")
    else:
        console.print("[red]Pet unreachable or rejected the notification.[/red]")
        raise typer.Exit(code=1)


@pet_app.command("say")
def pet_say(
    text: str = typer.Argument(..., help="Text to speak through the pet."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Speak TEXT through the pet /speak endpoint."""
    cfg = _load_pet_config()

    async def _send() -> bool:
        client = _client(cfg)
        try:
            return await client.speak(text, title=cfg.title)
        finally:
            await client.aclose()

    ok = asyncio.run(_send())
    if json_output:
        console.print_json(json.dumps({"ok": ok}))
        return
    if ok:
        console.print("[green]Spoken.[/green]")
    else:
        console.print("[red]Pet unreachable or rejected the speech request.[/red]")
        raise typer.Exit(code=1)


__all__ = ["pet_app"]
