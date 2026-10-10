from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_sandbox
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.token_store import TokenStore


def _context(tmp_path, *, owner=True, write=True):
    scopes = {"operator.read", "operator.write"} if write else {"operator.read"}
    return RpcContext(
        conn_id="isolated-token-worker",
        principal=Principal(
            role="operator", scopes=frozenset(scopes),
            is_owner=owner, authenticated=True,
        ),
        config=SimpleNamespace(state_dir=str(tmp_path)),
    )


def _seed(tmp_path):
    path = tmp_path / "sessions.db"
    store = TokenStore(path)
    public_id = store.create(
        name="existing", roles={"operator"}, scopes={"operator.read"}, capabilities=set(),
    ).record.public_id
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    return path, public_id


def _params(method, public_id):
    if method == "create":
        return {"name": "new-token", "hostExecute": False}
    return {"publicId": public_id} if method == "revoke" else {}


async def _call(tmp_path, method, params, **context_options):
    return await get_dispatcher().dispatch(
        "token-worker", f"sandbox.tokens.{method}", params,
        _context(tmp_path, **context_options),
    )


async def _wait(event):
    assert await asyncio.to_thread(event.wait, 3), "worker did not reach checkpoint"


@contextmanager
def _writer(path, *, sql_started=None, loop_progress=None):
    ready = threading.Event()
    release = threading.Event()
    released = threading.Event()
    errors = []

    def hold():
        try:
            with sqlite3.connect(path, timeout=3) as connection:
                connection.execute("BEGIN IMMEDIATE")
                ready.set()
                if sql_started is not None:
                    assert sql_started.wait(3)
                    loop_progress.wait(0.5)
                else:
                    assert release.wait(3)
                connection.rollback()
                released.set()
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        yield ready, release, released
    finally:
        release.set()
        if sql_started is not None:
            sql_started.set()
            loop_progress.set()
        holder.join(timeout=5)
        assert not holder.is_alive()
        assert not errors


def _observe_writes(monkeypatch, started):
    calls = []

    class ObservedStore(TokenStore):
        def _connect(self):
            connection = super()._connect()

            def trace(sql):
                if sql.lstrip().startswith(("INSERT INTO sandbox_tokens", "UPDATE sandbox_tokens")):
                    calls.append(sql.split()[0])
                    started.set()

            connection.set_trace_callback(trace)
            return connection

    monkeypatch.setattr(rpc_sandbox, "TokenStore", ObservedStore)
    return calls


@pytest.mark.parametrize("method", ["list", "create", "revoke"])
async def test_constructor_and_operation_run_off_loop_with_context(tmp_path, monkeypatch, method):
    _, public_id = _seed(tmp_path)
    loop_thread = threading.get_ident()
    marker = contextvars.ContextVar("token-worker-context", default=None)
    seen = []
    operation = "list_active" if method == "list" else method

    class ObservedStore(TokenStore):
        def __init__(self, path):
            seen.append(("constructor", threading.get_ident(), marker.get()))
            super().__init__(path)

    def observed(self, *args, **kwargs):
        seen.append(("operation", threading.get_ident(), marker.get()))
        return getattr(TokenStore, operation)(self, *args, **kwargs)

    monkeypatch.setattr(ObservedStore, operation, observed)
    monkeypatch.setattr(rpc_sandbox, "TokenStore", ObservedStore)
    token = marker.set("request-scope")
    try:
        result = await _call(tmp_path, method, _params(method, public_id))
    finally:
        marker.reset(token)
    assert result.ok
    assert [entry[0] for entry in seen] == ["constructor", "operation"]
    assert all(entry[1] != loop_thread and entry[2] == "request-scope" for entry in seen)


@pytest.mark.parametrize("method", ["create", "revoke"])
async def test_loop_progresses_while_waiting_for_write_lock(tmp_path, monkeypatch, method):
    path, public_id = _seed(tmp_path)
    started = threading.Event()
    progress = threading.Event()
    _observe_writes(monkeypatch, started)
    heartbeat_before_release = []

    with _writer(path, sql_started=started, loop_progress=progress) as (ready, _, released):
        await _wait(ready)

        async def heartbeat():
            await _wait(started)
            heartbeat_before_release.append(not released.is_set())
            progress.set()

        result, _ = await asyncio.gather(
            _call(tmp_path, method, _params(method, public_id)), heartbeat(),
        )
        assert result.ok
    assert heartbeat_before_release == [True]


async def test_list_reads_existing_wal_database_while_writer_is_locked(tmp_path):
    path, _ = _seed(tmp_path)
    with _writer(path) as (ready, release, released):
        await _wait(ready)
        result = await _call(tmp_path, "list", {})
        assert result.ok
        assert len(result.payload["tokens"]) == 1
        assert not released.is_set()
        release.set()


@pytest.mark.parametrize(
    ("method", "params", "context_options", "code"),
    [
        ("list", {}, {"owner": False}, "UNAUTHORIZED"),
        ("create", {"name": "denied"}, {"owner": False}, "UNAUTHORIZED"),
        ("revoke", {"publicId": "denied"}, {"owner": False}, "UNAUTHORIZED"),
        ("create", {"name": "denied"}, {"write": False}, "UNAUTHORIZED"),
        ("list", [], {}, "INVALID_REQUEST"),
        ("create", {"name": ""}, {}, "INVALID_REQUEST"),
        ("create", {"name": "invalid", "hostExecute": 1}, {}, "INVALID_REQUEST"),
        ("revoke", {"publicId": ""}, {}, "INVALID_REQUEST"),
    ],
)
async def test_denial_and_validation_precede_storage(
    tmp_path, monkeypatch, method, params, context_options, code,
):
    touched = []

    def forbidden_store(path):
        touched.append(path)
        raise AssertionError("storage must not run")

    monkeypatch.setattr(rpc_sandbox, "TokenStore", forbidden_store)
    result = await _call(tmp_path, method, params, **context_options)
    assert not result.ok
    assert result.error.code == code
    assert not touched
    assert not (tmp_path / "sessions.db").exists()


async def test_create_returns_secret_once_and_revoke_is_durable(tmp_path):
    created = await _call(tmp_path, "create", {"name": "once", "hostExecute": False})
    assert created.ok
    assert set(created.payload) == {"token", "record"}
    public_id = created.payload["record"]["publicId"]
    secret = created.payload.pop("token")
    assert isinstance(secret, str)
    assert "token" not in created.payload["record"]
    listed = await _call(tmp_path, "list", {})
    assert listed.ok
    assert len(listed.payload["tokens"]) == 1
    assert "token" not in listed.payload["tokens"][0]
    assert secret.encode() not in (tmp_path / "sessions.db").read_bytes()
    del secret
    revoked = await _call(tmp_path, "revoke", {"publicId": public_id})
    assert revoked.ok and revoked.payload["revoked"] is True
    repeated = await _call(tmp_path, "revoke", {"publicId": public_id})
    assert repeated.ok and repeated.payload["revoked"] is False
    assert TokenStore(tmp_path / "sessions.db").list_active() == ()


@pytest.mark.parametrize("method", ["create", "revoke"])
async def test_repeated_cancellation_waits_for_one_committed_write(tmp_path, monkeypatch, method):
    path, public_id = _seed(tmp_path)
    started = threading.Event()
    calls = _observe_writes(monkeypatch, started)
    task = None
    try:
        with _writer(path) as (ready, release, _):
            await _wait(ready)
            task = asyncio.create_task(_call(tmp_path, method, _params(method, public_id)))
            await _wait(started)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        assert len(calls) == 1
        with sqlite3.connect(path) as connection:
            if method == "create":
                assert connection.execute(
                    "SELECT COUNT(*) FROM sandbox_tokens WHERE name = 'new-token'",
                ).fetchone()[0] == 1
            else:
                row = connection.execute(
                    "SELECT revoked_at, authorization_revision "
                    "FROM sandbox_tokens WHERE public_id = ?",
                    (public_id,),
                ).fetchone()
                assert row[0] is not None and row[1] == 2
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_storage_failure_is_settled_without_retry(tmp_path, monkeypatch, cancel):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class BrokenStore(TokenStore):
        def create(self, **kwargs):
            calls.append("create")
            started.set()
            assert release.wait(3)
            raise sqlite3.OperationalError("synthetic token write failure")

    monkeypatch.setattr(rpc_sandbox, "TokenStore", BrokenStore)
    task = asyncio.create_task(_call(tmp_path, "create", {"name": "failed"}))
    try:
        await _wait(started)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 3)
            assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        else:
            result = await asyncio.wait_for(task, 3)
            assert not result.ok
            assert result.error.code == "INTERNAL_ERROR"
        assert calls == ["create"]
        assert TokenStore(tmp_path / "sessions.db").list_active() == ()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
