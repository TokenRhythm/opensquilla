"""Exercise the desktop helper transport against real, isolated owned processes."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PARENT = r"""
import asyncio, json, sys, time
from contextlib import AsyncExitStack
from pathlib import Path
from opensquilla.desktop_process_cleanup import configure_desktop_process_cleanup
from opensquilla.process_tree import (
    create_owned_subprocess_exec, capture_process_tree_owner, _strict_process_start_identity,
)

async def run():
    control, state, nonce, ready, kind = sys.argv[1:]
    configure_desktop_process_cleanup(
        ownership_dir=Path(control), state_dir=Path(state), instance_nonce=nonce,
    )
    tool_ready = Path(ready + '.tool')
    code = ('import pathlib,signal,sys,time; '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'pathlib.Path(sys.argv[1]).touch(); time.sleep(60)')
    async with AsyncExitStack() as stack:
        if kind == 'mcp':
            from opensquilla.mcp.stdio import MCPStdioClient
            from opensquilla.mcp.types import MCPServerConfig
            client = MCPStdioClient(MCPServerConfig(
                name='synthetic', transport='stdio', command=sys.executable,
                args=['-c', code, str(tool_ready)],
            ))
            await stack.enter_async_context(client._transport())
            child = client._process
            owner = client._process_owner
        else:
            child = await create_owned_subprocess_exec(
                sys.executable, '-c', code, str(tool_ready),
                stdout=asyncio.subprocess.PIPE,
            )
            owner = capture_process_tree_owner(child, isolated=True)
        assert child is not None and owner is not None
        async with asyncio.timeout(5):
            while not tool_ready.exists():
                await asyncio.sleep(0.01)
        receipt = Path(ready + '.tmp')
        receipt.write_text(json.dumps({
            'tool': child.pid, 'anchor': owner.pgid,
            'anchor_identity': _strict_process_start_identity(owner.pgid),
        }))
        receipt.replace(ready)
        # Simulate a Gateway that cannot service cancellation or its subprocess watcher.
        time.sleep(60)

asyncio.run(run())
"""


def _running(pid: int) -> bool:
    result = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=2,
    )
    status = result.stdout.strip()
    return bool(status) and not status.startswith('Z')


@pytest.mark.skipif(os.name != 'posix', reason='POSIX desktop process cleanup transport')
@pytest.mark.parametrize('root_exited', [False, True], ids=['blocked-root', 'exited-root'])
@pytest.mark.parametrize('spawn_kind', ['tool', 'mcp'])
def test_node_cleanup_stops_exact_launch_with_unresponsive_gateway(
    tmp_path, root_exited, spawn_kind,
):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for the desktop transport integration')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    parts = tuple(int(part) for part in re.findall(r'\d+', version)[:2])
    if parts < (22, 7):
        pytest.skip('Node 22.7+ is required to execute the production TypeScript directly')

    # Native Node type transformation avoids a dependency on a pre-existing dist
    # build or Electron, while executing the production helper and deadline code.
    for name in ('desktop-process-cleanup', 'desktop-quit'):
        source = (_ROOT / f'desktop/electron/src/{name}.ts').read_text()
        (tmp_path / f'{name}.ts').write_text(source)
    (tmp_path / 'package.json').write_text('{"type":"module"}')
    runner = tmp_path / 'run.mjs'
    runner.write_text('''
import { readFileSync } from 'node:fs'
import { cleanupDesktopProcessTrees } from './desktop-process-cleanup.ts'
import { DesktopQuitBudget } from './desktop-quit.ts'
const launch = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const budget = new DesktopQuitBudget(500, 4500)
try {
  const complete = await cleanupDesktopProcessTrees({ launch, budget })
  console.log(JSON.stringify({ complete }))
  process.exitCode = complete ? 0 : 1
} finally { budget.dispose() }
''')
    runtime = tmp_path / "runtime's bundle"
    runtime.write_text(
        '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
        + shlex.quote(str(_ROOT / 'desktop/electron/scripts/gateway-entry.py')) + ' "$@"\n'
    )
    runtime.chmod(0o700)
    env = {**os.environ, 'PYTHONPATH': str(_ROOT / 'src')}
    control = tmp_path / 'control'
    state = tmp_path / 'custom task state'
    parents: list[subprocess.Popen] = []
    receipts = []
    nonces = ['a' * 43, 'b' * 43]
    try:
        for index, nonce in enumerate(nonces):
            ready = tmp_path / f'ready-{index}.json'
            errors = tmp_path / f'errors-{index}.log'
            with errors.open('wb') as error_stream:
                parents.append(subprocess.Popen(
                    [sys.executable, '-c', _PARENT, str(control), str(state), nonce,
                     str(ready), spawn_kind],
                    env=env, stdout=subprocess.DEVNULL, stderr=error_stream,
                ))
            # MCP SDK imports can be slow on a busy native CI worker. This is
            # fixture readiness; the production cleanup still has its 5s budget.
            deadline = time.monotonic() + 30
            while not ready.exists() and parents[-1].poll() is None:
                assert time.monotonic() < deadline, 'synthetic parent did not launch its tool'
                time.sleep(0.01)
            assert ready.exists(), errors.read_text()
            receipts.append(json.loads(ready.read_text()))

        if root_exited:
            parents[0].kill()
            parents[0].wait(timeout=3)
        assert _running(receipts[0]['tool']), 'root exit alone must leave the test tool alive'
        request = tmp_path / 'launch.json'
        request.write_text(json.dumps({
            'command': str(runtime), 'args': ['gateway', 'run'], 'cwd': str(_ROOT),
            'mode': 'bundled', 'env': env, 'ownershipDir': str(control),
            'instanceNonce': nonces[0],
        }))
        result = subprocess.run(
            [node, '--experimental-transform-types', str(runner), str(request)],
            capture_output=True, text=True, timeout=8,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {'complete': True}
        assert not _running(receipts[0]['tool'])
        assert not _running(receipts[0]['anchor'])
        assert _running(receipts[1]['tool']), 'preserve a different launch in the same DB'
        assert _running(receipts[1]['anchor'])
        assert (parents[0].poll() is not None) is root_exited
    finally:
        # All signals below target groups created and retained by this test only.
        # Consult its registry as well if a fixture failed before publishing READY.
        from opensquilla import process_tree

        for reference in process_tree._load_owner_records(state):
            if process_tree._posix_controller_matches(reference.record):
                try:
                    os.killpg(reference.record.controller_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for receipt in receipts:
            if (
                receipt['anchor_identity'] is not None
                and process_tree._strict_process_start_identity(receipt['anchor'])
                == receipt['anchor_identity']
            ):
                try:
                    os.killpg(receipt['anchor'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for parent in parents:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=3)
