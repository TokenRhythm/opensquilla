"""SQLite compare-and-swap persistence for the versioned Safe policy."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from opensquilla.sandbox.policy_models import SandboxPolicy


class PolicyVersionConflict(RuntimeError):  # noqa: N818 - public domain name
    def __init__(
        self,
        *,
        expected_version: int,
        current_policy: SandboxPolicy,
    ) -> None:
        self.expected_version = int(expected_version)
        self.current_policy = current_policy
        super().__init__(
            "policy_version_conflict: "
            f"expected {self.expected_version}, current {current_policy.policy_version}"
        )


class SandboxPolicyStore:
    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        default = SandboxPolicy()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sandbox_policy (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    policy_version INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO sandbox_policy (
                    singleton_id, policy_version, policy_json, updated_at
                ) VALUES (1, 0, ?, ?)
                """,
                (
                    json.dumps(
                        default.to_public_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    int(time.time()),
                ),
            )

    def read(self) -> SandboxPolicy:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT policy_version, policy_json FROM sandbox_policy "
                "WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("sandbox policy row is missing")
        raw = json.loads(str(row["policy_json"]))
        if not isinstance(raw, dict):
            raise RuntimeError("sandbox policy payload is invalid")
        raw["policyVersion"] = int(row["policy_version"])
        return SandboxPolicy.model_validate(raw)

    def compare_and_swap(
        self,
        base_version: int,
        policy: SandboxPolicy | dict[str, Any],
    ) -> SandboxPolicy:
        candidate = (
            policy
            if isinstance(policy, SandboxPolicy)
            else SandboxPolicy.model_validate(policy)
        )
        expected = int(base_version)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT policy_version, policy_json FROM sandbox_policy "
                "WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("sandbox policy row is missing")
            current_version = int(row["policy_version"])
            if current_version != expected:
                raw_current = json.loads(str(row["policy_json"]))
                raw_current["policyVersion"] = current_version
                raise PolicyVersionConflict(
                    expected_version=expected,
                    current_policy=SandboxPolicy.model_validate(raw_current),
                )
            saved = candidate.model_copy(update={"policy_version": current_version + 1})
            encoded = json.dumps(
                saved.to_public_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            cursor = connection.execute(
                """
                UPDATE sandbox_policy
                SET policy_version = ?, policy_json = ?, updated_at = ?
                WHERE singleton_id = 1 AND policy_version = ?
                """,
                (
                    saved.policy_version,
                    encoded,
                    int(time.time()),
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                latest = self.read()
                raise PolicyVersionConflict(
                    expected_version=expected,
                    current_policy=latest,
                )
            connection.commit()
            return saved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def pin_sandbox_policy(context: Any, config: Any) -> Any:
    """Attach one persisted policy snapshot to a runtime ``ToolContext``.

    Embedded/standalone callers may omit ``state_dir``.  They retain the
    version-zero defaults instead of making turn construction fail.
    """

    state_dir = str(getattr(config, "state_dir", "") or "").strip()
    context.sandbox_policy = (
        SandboxPolicyStore(Path(state_dir) / "sessions.db").read()
        if state_dir
        else SandboxPolicy()
    )
    context.sandbox_gateway_config = config
    return context


__all__ = [
    "PolicyVersionConflict",
    "SandboxPolicyStore",
    "pin_sandbox_policy",
]
