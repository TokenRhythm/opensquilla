"""Smoke an installed TUI companion through the real bridge lifecycle."""

from __future__ import annotations

import asyncio

from opensquilla.cli.tui.opentui.bridge import OpenTuiBridge


async def _run() -> None:
    bridge = OpenTuiBridge(ready_timeout=15.0)
    try:
        await bridge.start()
        await bridge.send("shutdown")
        await asyncio.sleep(0.5)
    finally:
        await bridge.close()
    print("TUI_HOST_BRIDGE_SMOKE_OK", flush=True)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
