## Problem

Three pending-attachment RPC tests in `tests/test_gateway/test_turn_ingress_rpc.py` fail on Windows with `FileNotFoundError` even though the material file exists on disk:

- `test_pending_attachment_survives_restart_dispatches_once_and_cleans_owner`
- `test_pending_attachment_cancel_removes_only_its_private_owner`
- `test_session_delete_reclaims_pending_attachment_owner`

The staged owner paths are ~297 characters (`media/transcripts/<session-uuid>/.pending-chat-inputs/<64-hex owner digest>/<64-hex content sha>`), past `MAX_PATH` (260). The production writer goes through `native_io_path()` (extended-length `\\?\` prefix) and succeeds, but the test assertions read the bare pathlib object, which the Win32 layer refuses — so the assert sees "no such file" while the file is actually there.

Live reproduction confirmed this: `Test-Path` on the bare path returned `False` while the same path with the `\\?\` prefix returned `True`. The DB-level assertions (which never touch disk) pass; only the bare `read_bytes()` asserts fail.

## Fix

Route the four affected test assertions through `native_io_path()`, matching the convention documented in `paths.py` ("use this value only at the filesystem or SQLite boundary"). No product code changes.

## Verification

- Test file: **71 passed** on Windows (previously 3 failed / 68 passed)
- ruff: clean
- Pre-existing upstream bug: the same 3 failures reproduce on clean `main` in an isolated worktree, unrelated to any in-flight PR.