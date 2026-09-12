"""Small spawn target for cross-process owner registry contention tests."""

from __future__ import annotations

import os
from multiprocessing.connection import Connection
from pathlib import Path


def write_owner(connection: Connection, state_dir: Path, owner_id: str) -> None:
    from opensquilla import process_tree

    try:
        connection.send("ready")
        assert connection.recv() == "write"
        with process_tree.task_process_scope(
            state_dir,
            session_key="synthetic-session",
            task_id=owner_id,
        ):
            scope = process_tree._CURRENT_TASK_PROCESS_SCOPE.get()
            process_tree._insert_owner_record(
                scope,
                owner_id=owner_id,
                platform=process_tree._platform_kind(),
                controller_pid=os.getpid(),
            )
        connection.send("written")
    finally:
        connection.close()
