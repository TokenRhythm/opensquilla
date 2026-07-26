"""Pin workspace-agent-file writes to the existing ``O_NOFOLLOW`` contract."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.gateway.config import GatewayConfig


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require admin on Windows")
def test_open_workspace_agent_file_for_write_uses_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WRONLY branch must include O_NOFOLLOW.

    We monkeypatch ``os.open`` so we can observe the flag set passed in
    without actually creating a filesystem entry, then drop a symlink
    in the target path and assert the function rejects it instead of
    silently following.
    """

    registry = AgentRegistry(GatewayConfig(), persist_changes=False)
    target_path = tmp_path / "agent.md"

    # First, prove the WRONLY branch actually does pass O_NOFOLLOW by
    # intercepting os.open and looking at the flags.
    captured: dict[str, int] = {}

    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only capture opens that target our path; ignore mkstemp / etc.
        if os.path.abspath(str(path)) == os.path.abspath(str(target_path)):
            captured["flags"] = flags
            # Return a real fd so the test does not blow up later.
            return real_open(str(path), flags, *args, **kwargs)
        return real_open(str(path), flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)

    # Create the file first so lstat() returns a regular file (not ENOENT).
    target_path.write_text("hello", encoding="utf-8")

    fd = registry._open_workspace_agent_file_for_write(target_path)  # noqa: SLF001
    os.close(fd)
    # Cleanup the file we created via the spy.
    try:
        target_path.unlink()
    except FileNotFoundError:
        pass

    flags = captured.get("flags")
    assert flags is not None, "os.open was not called for the write path"
    # O_NOFOLLOW must be set. ``getattr(os, "O_NOFOLLOW", 0)`` returns 0
    # on platforms without the constant, so we cannot compare to 0 —
    # we have to check the constant itself.
    nofollow_const = getattr(os, "O_NOFOLLOW", None)
    if nofollow_const is not None:
        assert flags & nofollow_const, (
            f"WRONLY open must include O_NOFOLLOW; got flags=0x{flags:x}"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require admin on Windows")
def test_open_workspace_agent_file_for_write_rejects_symlink(
    tmp_path: Path,
) -> None:
    """A symlink at the target path must be rejected, not followed."""

    real = tmp_path / "real.md"
    real.write_text("original", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink not supported in this env: {exc}")

    registry = AgentRegistry(GatewayConfig(), persist_changes=False)

    with pytest.raises(ValueError, match="symlink"):
        registry._open_workspace_agent_file_for_write(link)  # noqa: SLF001

    # And the real file is untouched.
    assert real.read_text(encoding="utf-8") == "original"
