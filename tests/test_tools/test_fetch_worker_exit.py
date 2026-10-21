"""The real fetch helper must not keep a closing Python interpreter alive."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from opensquilla.tools import fetch_work


@pytest.mark.parametrize("abandon", ["cancel", "timeout"])
def test_process_exits_normally_with_unreturned_fetch_worker(abandon):
    script = textwrap.dedent("""
        import asyncio
        import runpy
        import sys
        import threading

        print('loading helper', flush=True)
        run_blocking_fetch_work = runpy.run_path(sys.argv[1])['run_blocking_fetch_work']
        print('helper loaded', flush=True)

        started = threading.Event()

        def unreturned_dns():
            started.set()
            threading.Event().wait()

        async def main():
            task = asyncio.create_task(run_blocking_fetch_work(unreturned_dns))
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(.005)
            print('worker started', flush=True)
            if sys.argv[2] == 'cancel':
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError('cancel did not propagate')
            else:
                try:
                    await asyncio.wait_for(task, .02)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError('timeout did not propagate')

        asyncio.run(main())
        print('normal interpreter exit', flush=True)
    """)
    # Load the actual self-contained helper without importing the whole tools
    # package in the child. This tests helper/atexit behavior, not Gateway boot
    # performance. A final print alone would miss an executor's atexit join.
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, fetch_work.__file__, abandon],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"child did not exit: stdout={exc.stdout!r}; stderr={exc.stderr!r}",
            pytrace=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "loading helper", "helper loaded", "worker started", "normal interpreter exit",
    ]
