from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from opensquilla import process_tree


@pytest.mark.skipif(os.name != "posix", reason="PGID lifecycle is POSIX-specific")
def test_absent_owned_pgid_closes_permanently_before_numeric_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[int, int]] = []

    def absent(pgid: int, sig: int) -> None:
        probes.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(process_tree.os, "killpg", absent)
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=0),
        pid=4242,
        pgid=4242,
    )

    assert owner.is_active() is False
    # A later unrelated process may reuse 4242. The closed owner must never
    # probe or signal that numeric group again.
    assert owner.is_active() is False
    assert probes == [(4242, 0)]


@pytest.mark.asyncio
async def test_non_durable_owner_never_widens_cleanup_to_a_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree.os,
        "killpg",
        lambda pgid, sig: group_signals.append((pgid, sig)),
    )

    class DirectProcess:
        pid = 5151
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    proc = DirectProcess()
    owner = process_tree.capture_process_tree_owner(proc, isolated=False)

    assert owner.durable is False
    assert await owner.terminate(graceful_timeout=0.1, kill_timeout=0.1)
    assert proc.returncode == 0
    assert group_signals == []


def test_windows_job_assignment_failure_is_a_fail_closed_platform_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 6262
        returncode = None
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    proc = Process()
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "assign",
        classmethod(lambda _cls, _pid: (_ for _ in ()).throw(OSError("denied"))),
    )

    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="Job Object"):
        process_tree.capture_process_tree_owner(proc, isolated=True)

    assert proc.terminated is True
