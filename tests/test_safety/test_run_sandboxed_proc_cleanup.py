"""Regression tests: ``run_sandboxed`` must always reap its child.

The original ``run_sandboxed`` only wrapped ``proc.communicate`` for
``subprocess.TimeoutExpired``. Any other exception (decode error in
``_decode_stream``, pipe ``OSError``, ``KeyboardInterrupt`` after
``Popen``) left a running child with open stdout/stderr pipes — the
process would linger until ``Popen.__del__`` ran, and the OS would
file ``ResourceWarning`` for the unreaped pipes.

The fix wraps the post-``Popen`` block in a ``try/finally`` that
calls ``proc.kill()`` + ``proc.wait()`` on the exception path and
explicitly closes the open pipes on every path.
"""

from __future__ import annotations

import subprocess

import pytest

from opensquilla.safety import sandbox as sandbox_mod
from opensquilla.safety.sandbox import (
    REASON_WALL_LIMIT,
    SandboxLimits,
    run_sandboxed,
)


class _FakeProcess:
    """Minimal stand-in for ``subprocess.Popen`` that records lifecycle calls.

    The test installs this in place of ``subprocess.Popen`` for the
    duration of ``run_sandboxed`` so we can observe kill / wait / close
    behaviour without spawning a real subprocess.

    Like the real ``Popen``, ``returncode`` starts as ``None`` and is
    only set once the child is reaped — either by ``wait()`` or by a
    successful ``communicate()`` (which calls ``wait()`` internally).
    ``waited`` records that reap, whichever call performed it.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.args = args
        self.kwargs = kwargs
        self.killed = False
        self.waited = False
        self.communicate_calls = 0
        self.stdout_closed = False
        self.stderr_closed = False
        self.stdin_closed = False
        self.returncode: int | None = None

        # ``Popen`` exposes stdout/stderr/stdin as streams; we model
        # them as objects with a close() so the finally block can
        # exercise them.
        self.stdout = _FakeStream(self, "stdout")
        self.stderr = _FakeStream(self, "stderr")
        self.stdin = None  # stdin=None in tests unless explicitly set

    def _reap(self) -> None:
        """Model the child being reaped (by ``wait`` or ``communicate``)."""
        self.waited = True
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0

    def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            # First call: raise a non-timeout exception so we exercise
            # the outer except-BaseException branch.
            raise RuntimeError("simulated pipe failure")
        self._reap()
        return (b"", b"")

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):  # type: ignore[no-untyped-def]
        self._reap()
        return self.returncode


class _FakeStream:
    def __init__(self, proc: _FakeProcess, name: str) -> None:
        self._proc = proc
        self._name = name

    def close(self) -> None:
        setattr(self._proc, f"{self._name}_closed", True)


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    factory: type[_FakeProcess] = _FakeProcess,
) -> dict[str, _FakeProcess]:
    """Replace ``subprocess.Popen`` inside ``safety.sandbox`` with a spy.

    Returns the dict the spy records the created process into under the
    ``"proc"`` key. The entry only exists once ``run_sandboxed`` has
    actually invoked the patched ``Popen`` — tests must read
    ``captured["proc"]`` *after* calling ``run_sandboxed`` so they
    assert on the very instance the code under test used.
    """

    captured: dict[str, _FakeProcess] = {}

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        proc = factory(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(sandbox_mod.subprocess, "Popen", fake_popen)
    return captured


@pytest.mark.skipif(
    not sandbox_mod.HAS_RESOURCE, reason="resource module unavailable on this platform"
)
def test_run_sandboxed_kills_child_on_non_timeout_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-timeout exception during communicate must kill + wait the child."""

    captured = _install_fake_popen(monkeypatch)

    with pytest.raises(RuntimeError, match="simulated pipe failure"):
        run_sandboxed(["/bin/echo", "hi"], SandboxLimits(wall_seconds=10))

    proc = captured["proc"]
    # The child must be killed and reaped so it does not linger.
    assert proc.killed is True
    assert proc.waited is True
    # The pipes must be closed even on the exception path.
    assert proc.stdout_closed is True
    assert proc.stderr_closed is True


@pytest.mark.skipif(
    not sandbox_mod.HAS_RESOURCE, reason="resource module unavailable on this platform"
)
def test_run_sandboxed_timeout_branch_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired path must still reap the child + close pipes.

    The post-kill ``communicate()`` succeeds here, so it is the call
    that reaps the child (production deliberately skips the explicit
    ``wait()`` when ``communicate()`` already set ``returncode``).
    """

    class _TimeoutProcess(_FakeProcess):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            self.first = True

        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            if self.first:
                self.first = False
                raise subprocess.TimeoutExpired(cmd=["/bin/sleep"], timeout=10)
            # A successful communicate() reaps the child (it calls
            # wait() internally); ``_reap`` records that and sets
            # returncode to -9 because kill() ran first.
            self._reap()
            return (b"", b"")

    captured = _install_fake_popen(monkeypatch, _TimeoutProcess)

    result = run_sandboxed(["/bin/sleep", "999"], SandboxLimits(wall_seconds=1))

    proc = captured["proc"]
    assert result.reason == REASON_WALL_LIMIT
    assert proc.killed is True
    assert proc.waited is True
    # Pipes closed by the finally block.
    assert proc.stdout_closed is True
    assert proc.stderr_closed is True


@pytest.mark.skipif(
    not sandbox_mod.HAS_RESOURCE, reason="resource module unavailable on this platform"
)
def test_run_sandboxed_timeout_branch_reaps_when_recollect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the post-kill ``communicate()`` call also raises, the child
    must still be reaped so the timeout branch upholds the "always
    reap" guarantee. ``returncode`` stays ``None`` after the failed
    communicate, so the fallback ``wait()`` branch is exercised."""

    class _RecollectFailsProcess(_FakeProcess):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            self.first = True

        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            if self.first:
                self.first = False
                raise subprocess.TimeoutExpired(cmd=["/bin/sleep"], timeout=10)
            # Second call (post-kill) must also raise — that's the
            # path that previously left the child un-reaped.
            raise RuntimeError("simulated post-kill read failure")

    captured = _install_fake_popen(monkeypatch, _RecollectFailsProcess)

    result = run_sandboxed(["/bin/sleep", "999"], SandboxLimits(wall_seconds=1))

    proc = captured["proc"]
    assert result.reason == REASON_WALL_LIMIT
    assert proc.killed is True, "child must be killed on timeout"
    assert proc.waited is True, (
        "child must be waited on even when post-kill communicate() raises"
    )
    assert proc.stdout_closed is True
    assert proc.stderr_closed is True


@pytest.mark.skipif(
    not sandbox_mod.HAS_RESOURCE, reason="resource module unavailable on this platform"
)
def test_run_sandboxed_success_path_closes_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On success, the finally block still closes the pipes that
    ``communicate`` did not close (it closes them in CPython but the
    explicit close is harmless and prevents ResourceWarning)."""

    class _HappyProcess(_FakeProcess):
        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            self.communicate_calls += 1
            self._reap()
            return (b"ok\n", b"")

    captured = _install_fake_popen(monkeypatch, _HappyProcess)

    result = run_sandboxed(["/bin/echo", "hi"], SandboxLimits(wall_seconds=5))

    proc = captured["proc"]
    assert result.reason == sandbox_mod.REASON_OK
    assert result.stdout == "ok\n"
    # Popen lifecycle clean: not killed (clean exit), pipes closed.
    assert proc.killed is False
    assert proc.returncode == 0
    assert proc.stdout_closed is True
    assert proc.stderr_closed is True
