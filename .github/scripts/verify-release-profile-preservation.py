#!/usr/bin/env python3
"""Seed and verify synthetic Desktop profile data around installer operations."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

_LABEL_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,80}")
LONG_SESSION_KEY = "agent:main:webchat:release-recovery-long-session"
LONG_SESSION_ID = "release-recovery-long-session"
LONG_SESSION_MESSAGE_COUNT = 320
_LONG_SESSION_BASE_TIMESTAMP_MS = 1_700_000_000_000
_RUNTIME_PACK_SENTINEL = b"synthetic retained Runtime Pack payload\n"
_SYSTEM_TOOL_SENTINELS = {
    "python": b"synthetic external Python sentinel\n",
    "node": b"synthetic external Node.js sentinel\n",
    "git": b"synthetic external Git sentinel\n",
}

# Frozen at the v0.5.0rc3 session/transcript shape. Do not replace this with
# current runtime DDL: the release gate must prove the candidate migrates and
# reads an existing installation rather than a freshly created database.
_RC3_SESSION_SCHEMA = """
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    runtime_ms INTEGER,
    last_channel TEXT,
    last_to TEXT,
    last_account_id TEXT,
    last_thread_id TEXT,
    delivery_context TEXT,
    model TEXT,
    model_provider TEXT,
    provider_override TEXT,
    model_override TEXT,
    auth_profile_override TEXT,
    auth_profile_override_source TEXT,
    context_tokens INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens_fresh INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    billed_cost_usd REAL NOT NULL DEFAULT 0.0,
    estimated_cost_component_usd REAL NOT NULL DEFAULT 0.0,
    cost_source TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    session_file TEXT,
    spawned_by TEXT,
    parent_session_key TEXT,
    forked_from_parent INTEGER NOT NULL DEFAULT 0,
    spawn_depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    thinking_level TEXT,
    fast_mode INTEGER NOT NULL DEFAULT 0,
    verbose_level TEXT,
    reasoning_level TEXT,
    send_policy TEXT NOT NULL DEFAULT 'allow',
    queue_mode TEXT NOT NULL DEFAULT 'steer',
    label TEXT,
    display_name TEXT,
    derived_title TEXT,
    channel TEXT,
    group_id TEXT,
    subject TEXT,
    origin TEXT,
    agent_id TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_sessions_updated_at ON sessions(updated_at);

CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    turn_usage TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_transcript_session_id ON transcript_entries(session_id);
CREATE INDEX idx_transcript_session_key ON transcript_entries(session_key);

CREATE VIRTUAL TABLE transcript_fts
USING fts5(content, content=transcript_entries, content_rowid=id);
CREATE TRIGGER transcript_fts_ai AFTER INSERT ON transcript_entries BEGIN
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER transcript_fts_ad AFTER DELETE ON transcript_entries BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER transcript_fts_au AFTER UPDATE ON transcript_entries BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def _validated_label(value: str) -> str:
    if _LABEL_PATTERN.fullmatch(value) is None:
        raise ValueError("label must contain only ASCII letters, digits, dot, underscore, or dash")
    return value


def _workspace_files(label: str) -> dict[str, str]:
    return {
        "IDENTITY.md": f"# Synthetic {label} identity sentinel\n",
        "USER.md": f"# Synthetic {label} user\n",
        "SOUL.md": f"# Synthetic {label} soul\n",
        "MEMORY.md": f"# Synthetic {label} memory\n",
    }


def _config_text(home: Path, label: str) -> str:
    return (
        f"# Synthetic {label} release-preservation profile\n"
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(home / 'workspace'))}\n"
        'search_provider = "duckduckgo"\n'
        "\n"
        "[llm]\n"
        'provider = "ollama"\n'
        'model = "opensquilla-release-session-recovery-smoke"\n'
        'base_url = "http://127.0.0.1:11434"\n'
        "\n"
        "[squilla_router]\n"
        "enabled = false\n"
        "\n"
        "[llm_ensemble]\n"
        "enabled = false\n"
        "\n"
        "[privacy]\n"
        "disable_network_observability = false\n"
    )


def _runtime_config_text(home: Path) -> str:
    """Return the deterministic config produced by the first current-runtime load."""

    return (
        f"state_dir = {json.dumps(str(home / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(home / 'workspace'))}\n"
        'search_provider = "duckduckgo"\n'
        "config_version = 1\n"
        "\n"
        "[llm]\n"
        'provider = "ollama"\n'
        'model = "opensquilla-release-session-recovery-smoke"\n'
        'base_url = "http://127.0.0.1:11434"\n'
        "\n"
        "[squilla_router]\n"
        "enabled = false\n"
        "\n"
        "[llm_ensemble]\n"
        "enabled = false\n"
        "\n"
        "[privacy]\n"
        "disable_network_observability = false\n"
        "\n"
        "[control_ui]\n"
        'default_locale = "en"\n'
    )


def _long_history_message(label: str, index: int) -> str:
    return f"Synthetic retained history message {index:04d} ({label})"


def _runtime_pack_sentinel_path(home: Path) -> Path:
    return (
        home
        / "state"
        / "runtime-packs"
        / "v1"
        / "packages"
        / "preservation-sentinel"
        / "payload.bin"
    )


def _external_sentinel_paths(external_root: Path) -> dict[str, Path]:
    return {
        component: external_root / component / f"{component}-sentinel.bin"
        for component in _SYSTEM_TOOL_SENTINELS
    }


def _write_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _verify_exact_bytes(path: Path, expected: bytes, label: str) -> None:
    actual = path.read_bytes()
    if actual != expected:
        raise AssertionError(f"{label} changed while installing or uninstalling Desktop")


def seed_profile(home: Path, label: str, *, external_root: Path | None = None) -> None:
    """Create a synthetic RC3-shaped profile without replacing any file."""

    home = home.resolve()
    workspace = home / "workspace"
    state = home / "state"
    protected = [home / "config.toml", state / "sessions.db"] + [
        workspace / name for name in _workspace_files(label)
    ]
    existing = [path for path in protected if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite preservation fixture: {existing[0]}")

    workspace.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    for name, expected in _workspace_files(label).items():
        (workspace / name).write_text(expected, encoding="utf-8", newline="")
    (home / "config.toml").write_text(_config_text(home, label), encoding="utf-8", newline="")
    _write_new_bytes(_runtime_pack_sentinel_path(home), _RUNTIME_PACK_SENTINEL)
    if external_root is not None:
        for component, path in _external_sentinel_paths(external_root.resolve()).items():
            _write_new_bytes(path, _SYSTEM_TOOL_SENTINELS[component])

    with sqlite3.connect(state / "sessions.db") as connection:
        connection.executescript(_RC3_SESSION_SCHEMA)
        connection.execute(
            "CREATE TABLE release_preservation_chat (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO release_preservation_chat (id, body) VALUES (?, ?)",
            (f"{label}-session", f"synthetic retained chat ({label})"),
        )
        connection.execute(
            """
            INSERT INTO sessions (
                session_key,
                session_id,
                created_at,
                updated_at,
                status,
                chat_type,
                label,
                display_name,
                channel,
                agent_id,
                schema_version
            ) VALUES (?, ?, ?, ?, 'done', 'direct', ?, ?, 'webchat', 'main', 8)
            """,
            (
                LONG_SESSION_KEY,
                LONG_SESSION_ID,
                _LONG_SESSION_BASE_TIMESTAMP_MS,
                _LONG_SESSION_BASE_TIMESTAMP_MS + LONG_SESSION_MESSAGE_COUNT * 1_000,
                f"Synthetic retained long session ({label})",
                f"Synthetic retained long session ({label})",
            ),
        )
        connection.executemany(
            """
            INSERT INTO transcript_entries (
                session_id,
                session_key,
                message_id,
                role,
                content,
                created_at,
                token_count,
                provenance_kind,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, 8, ?, 8)
            """,
            [
                (
                    LONG_SESSION_ID,
                    LONG_SESSION_KEY,
                    f"release-recovery-message-{index:04d}",
                    "user" if index % 2 else "assistant",
                    _long_history_message(label, index),
                    _LONG_SESSION_BASE_TIMESTAMP_MS + index * 1_000,
                    "external_user" if index % 2 else None,
                )
                for index in range(1, LONG_SESSION_MESSAGE_COUNT + 1)
            ],
        )
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"seeded sessions.db failed PRAGMA quick_check: {result!r}")


def verify_profile(
    home: Path,
    label: str,
    *,
    runtime_migrated: bool = False,
    external_root: Path | None = None,
) -> None:
    """Verify exact fixture bytes and a read-only SQLite integrity probe."""

    home = home.resolve()
    workspace = home / "workspace"
    state = home / "state"
    for name, expected in _workspace_files(label).items():
        actual = (workspace / name).read_text(encoding="utf-8")
        if actual != expected:
            raise AssertionError(f"{name} changed while installing or uninstalling Desktop")

    actual_config = (home / "config.toml").read_text(encoding="utf-8")
    expected_config = (
        _runtime_config_text(home) if runtime_migrated else _config_text(home, label)
    )
    if actual_config != expected_config:
        phase = "after expected runtime migration" if runtime_migrated else "during installation"
        raise AssertionError(f"config.toml changed unexpectedly {phase}")

    _verify_exact_bytes(
        _runtime_pack_sentinel_path(home),
        _RUNTIME_PACK_SENTINEL,
        "configured-state Runtime Pack sentinel",
    )
    if external_root is not None:
        for component, path in _external_sentinel_paths(external_root.resolve()).items():
            _verify_exact_bytes(
                path,
                _SYSTEM_TOOL_SENTINELS[component],
                f"external system {component} sentinel",
            )

    database = state / "sessions.db"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise AssertionError(f"sessions.db failed PRAGMA quick_check: {quick_check!r}")
        row = connection.execute("SELECT id, body FROM release_preservation_chat").fetchone()
        session_row = connection.execute(
            "SELECT session_id, label FROM sessions WHERE session_key = ?",
            (LONG_SESSION_KEY,),
        ).fetchone()
        history_row = connection.execute(
            """
            SELECT
                COUNT(*),
                MIN(message_id),
                MAX(message_id),
                MIN(created_at),
                MAX(created_at)
            FROM transcript_entries
            WHERE session_key = ?
            """,
            (LONG_SESSION_KEY,),
        ).fetchone()
        first_message = connection.execute(
            """
            SELECT content
            FROM transcript_entries
            WHERE session_key = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (LONG_SESSION_KEY,),
        ).fetchone()
        last_message = connection.execute(
            """
            SELECT content
            FROM transcript_entries
            WHERE session_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (LONG_SESSION_KEY,),
        ).fetchone()
    expected_row = (f"{label}-session", f"synthetic retained chat ({label})")
    if row != expected_row:
        raise AssertionError(f"sessions.db retained-chat row changed: {row!r}")
    expected_session = (
        LONG_SESSION_ID,
        f"Synthetic retained long session ({label})",
    )
    if session_row != expected_session:
        raise AssertionError(f"sessions.db long-session row changed: {session_row!r}")
    expected_history = (
        LONG_SESSION_MESSAGE_COUNT,
        "release-recovery-message-0001",
        f"release-recovery-message-{LONG_SESSION_MESSAGE_COUNT:04d}",
        _LONG_SESSION_BASE_TIMESTAMP_MS + 1_000,
        _LONG_SESSION_BASE_TIMESTAMP_MS + LONG_SESSION_MESSAGE_COUNT * 1_000,
    )
    if history_row != expected_history:
        raise AssertionError(f"sessions.db long-session history changed: {history_row!r}")
    if first_message != (_long_history_message(label, 1),):
        raise AssertionError(
            f"sessions.db first long-session message changed: {first_message!r}"
        )
    if last_message != (_long_history_message(label, LONG_SESSION_MESSAGE_COUNT),):
        raise AssertionError(
            f"sessions.db last long-session message changed: {last_message!r}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("seed", "verify", "verify-runtime"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--label", type=_validated_label, required=True)
    parser.add_argument("--external-root", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.operation == "seed":
            seed_profile(args.home, args.label, external_root=args.external_root)
            print(f"profile preservation fixture seeded: {args.home}")
        else:
            runtime_migrated = args.operation == "verify-runtime"
            verify_profile(
                args.home,
                args.label,
                runtime_migrated=runtime_migrated,
                external_root=args.external_root,
            )
            suffix = " after runtime migration" if runtime_migrated else ""
            print(f"profile preservation verified{suffix}: {args.home}")
    except (
        AssertionError,
        FileExistsError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
