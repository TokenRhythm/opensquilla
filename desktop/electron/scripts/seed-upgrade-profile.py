"""Seed and verify synthetic task/token data for the local installer rehearsal."""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from opensquilla.gateway.token_store import TokenStore
from opensquilla.session.models import SessionNode, SessionStatus
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:local-upgrade-rehearsal"
TOKEN_NAME = "local-upgrade-rehearsal"
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_RUN_MODE_RE = re.compile(r'^(\s*run_mode\s*=\s*)"[^"]*"(.*)$')


async def _seed(database: Path, token_file: Path) -> None:
    storage = await SessionStorage.open(str(database))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=SESSION_KEY,
                status=SessionStatus.DONE,
                display_name="Local upgrade rehearsal task",
                subject="Synthetic data; safe to delete",
            )
        )
    finally:
        await storage.close()

    issued = TokenStore(database).create(
        name=TOKEN_NAME,
        roles={"operator"},
        scopes={"operator.read", "operator.write"},
        capabilities={"safe"},
        source_kind="named",
    )
    token_file.write_text(issued.token, encoding="utf-8")


async def _verify(database: Path, token_file: Path) -> None:
    storage = await SessionStorage.open(str(database))
    try:
        session = await storage.get_session(SESSION_KEY)
    finally:
        await storage.close()
    if session is None or session.display_name != "Local upgrade rehearsal task":
        raise RuntimeError("synthetic task was not preserved")

    token = token_file.read_text(encoding="utf-8").strip()
    record = TokenStore(database).verify(token, peer_ip="127.0.0.1")
    if record is None or record.name != TOKEN_NAME:
        raise RuntimeError("synthetic named token was not preserved")


def _set_full_access(config: Path) -> None:
    lines = config.read_text(encoding="utf-8").splitlines()
    sandbox_start: int | None = None
    sandbox_end = len(lines)
    for index, line in enumerate(lines):
        section = _SECTION_RE.match(line)
        if section is None:
            continue
        if sandbox_start is not None:
            sandbox_end = index
            break
        if section.group(1).strip() == "sandbox":
            sandbox_start = index
    if sandbox_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("[sandbox]", 'run_mode = "full"'))
    else:
        for index in range(sandbox_start + 1, sandbox_end):
            match = _RUN_MODE_RE.match(lines[index])
            if match is not None:
                lines[index] = f'{match.group(1)}"full"{match.group(2)}'
                break
        else:
            lines.insert(sandbox_start + 1, 'run_mode = "full"')
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("seed", "verify", "set-full"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.operation == "set-full":
        if args.config is None:
            parser.error("--config is required for set-full")
        _set_full_access(args.config)
    else:
        if args.database is None or args.token_file is None:
            parser.error("--database and --token-file are required")
        args.database.parent.mkdir(parents=True, exist_ok=True)
        args.token_file.parent.mkdir(parents=True, exist_ok=True)
        if args.operation == "seed":
            asyncio.run(_seed(args.database, args.token_file))
        else:
            asyncio.run(_verify(args.database, args.token_file))


if __name__ == "__main__":
    main()
