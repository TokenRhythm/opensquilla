"""API key must reach the container via env-file, never via argv."""

import os
import stat
import sys

import pytest

from opensquilla.contrib.swebench import agent as agent_mod


@pytest.mark.skipif(sys.platform == "win32", reason="code-task Windows support is WIP")
def test_write_secret_env_file_private_and_removed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123")
    path = agent_mod._write_secret_env_file()
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        # The file must be readable as UTF-8 — both for the linux
        # docker subprocess and for any local Windows inspection that
        # happens before unlink.
        content = open(path, encoding="utf-8").read()
        assert "OPENROUTER_API_KEY=sk-or-test-123" in content
    finally:
        os.unlink(path)


@pytest.mark.skipif(sys.platform == "win32", reason="code-task Windows support is WIP")
def test_write_secret_env_file_cleans_up_on_write_failure(monkeypatch, tmp_path):
    """If the fdopen + write path raises, the partial file must not leak."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123")

    real_mkstemp = agent_mod.tempfile.mkstemp
    mkstemp_fds: list[int] = []

    def patched_mkstemp(*args, **kwargs):  # type: ignore[no-untyped-def]
        fd, path = real_mkstemp(*args, dir=str(tmp_path), **kwargs)
        mkstemp_fds.append(fd)
        return fd, path

    monkeypatch.setattr(agent_mod.tempfile, "mkstemp", patched_mkstemp)

    closed_descriptors: list[int] = []

    real_close = os.close

    def spy_close(fd):  # type: ignore[no-untyped-def]
        closed_descriptors.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)

    # Patching a ``write`` attribute onto the ``os.fdopen`` *function*
    # would not affect the file object it returns — replace fdopen
    # itself with a factory for a fake handle that (like the real one)
    # takes ownership of the fd and raises from ``write``.
    class _ExplodingHandle:
        def __init__(self, fd):  # type: ignore[no-untyped-def]
            self._fd = fd

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc_info):  # type: ignore[no-untyped-def]
            # The real fdopen handle closes the wrapped descriptor when
            # the ``with`` block exits, even on error.
            os.close(self._fd)
            return False

        def write(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated write failure")

    def fake_fdopen(fd, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _ExplodingHandle(fd)

    monkeypatch.setattr(agent_mod.os, "fdopen", fake_fdopen)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        agent_mod._write_secret_env_file()

    # The failed tmp file must have been unlinked — no half-written key
    # lying around for a later reader.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("opensquilla-swebench-")]
    assert leftovers == [], f"secret env-file leaked: {leftovers}"
    # ``fdopen`` took ownership of the descriptor, so the handle (not
    # the raw-fd fallback) must have closed the mkstemp fd.
    assert mkstemp_fds, "mkstemp was never called"
    assert all(fd in closed_descriptors for fd in mkstemp_fds), (
        "secret env-file fd was leaked on write failure"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="code-task Windows support is WIP")
def test_write_secret_env_file_closes_raw_fd_when_fdopen_fails(
    monkeypatch, tmp_path
):
    """If ``os.fdopen`` raises before taking ownership of the fd, the
    raw descriptor from ``mkstemp`` must be closed rather than leaked.

    (``os.chmod`` cannot be used to trigger this branch: the production
    code deliberately suppresses ``OSError`` from that belt-and-braces
    call and continues.)"""

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123")

    real_mkstemp = agent_mod.tempfile.mkstemp
    mkstemp_fds: list[int] = []

    def patched_mkstemp(*args, **kwargs):  # type: ignore[no-untyped-def]
        fd, path = real_mkstemp(*args, dir=str(tmp_path), **kwargs)
        mkstemp_fds.append(fd)
        return fd, path

    monkeypatch.setattr(agent_mod.tempfile, "mkstemp", patched_mkstemp)

    closed_descriptors: list[int] = []

    real_close = os.close

    def spy_close(fd):  # type: ignore[no-untyped-def]
        closed_descriptors.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "close", spy_close)

    # Force fdopen to raise before it ever takes ownership of the fd.
    # The cleanup branch must release the raw mkstemp descriptor.
    def boom_fdopen(fd, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(agent_mod.os, "fdopen", boom_fdopen)

    with pytest.raises(OSError, match="simulated fdopen failure"):
        agent_mod._write_secret_env_file()

    # The raw mkstemp fd must have been closed by the cleanup path.
    assert mkstemp_fds, "mkstemp was never called"
    assert all(fd in closed_descriptors for fd in mkstemp_fds), (
        "mkstemp fd was leaked when fdopen raised before taking ownership"
    )
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("opensquilla-swebench-")]
    assert leftovers == [], f"secret env-file leaked: {leftovers}"


def test_send_task_keeps_key_out_of_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-supersecret")
    captured: list[list[str]] = []
    env_files_seen: list[str] = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.append([str(c) for c in cmd])
        if "--env-file" in cmd:
            env_path = cmd[cmd.index("--env-file") + 1]
            env_files_seen.append(env_path)
            # The file must exist (and hold the key) while docker runs.
            assert "sk-or-supersecret" in open(env_path, encoding="utf-8").read()
        return FakeResult()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    adapter = agent_mod.OpenSquillaAdapter(model="test-model", timeout=5)
    adapter.send_task("fix it", agent_id="a1", container_name="c1", artifact_dir=tmp_path)

    exec_cmds = [c for c in captured if "--env-file" in c]
    assert exec_cmds, "agent invocation must use --env-file"
    for cmd in captured:
        assert not any("sk-or-supersecret" in part for part in cmd), (
            "API key must never appear in argv"
        )
    # The env-file is cleaned up after the subprocess finishes.
    assert all(not os.path.exists(p) for p in env_files_seen)
