"""Android entrypoint: boot the OpenSquilla gateway on loopback.

Called from MainActivity via Chaquopy. Runs until the process dies.
"""
import asyncio
import os
import sys
import traceback

PORT = 18790


def serve(home_dir: str) -> int:
    # Redirect all state (config.toml, DB, logs, auth token) into app-private storage.
    os.environ["HOME"] = home_dir
    os.environ.setdefault("OPENSQUILLA_STATE_DIR", os.path.join(home_dir, "state"))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    # Some libs probe TMPDIR / temp paths; Chaquopy exposes /data/local/tmp as tmp.
    os.environ.setdefault("TMPDIR", os.path.join(home_dir, "tmp"))
    os.makedirs(os.environ["TMPDIR"], exist_ok=True)
    os.makedirs(os.environ["OPENSQUILLA_STATE_DIR"], exist_ok=True)

    # Android does not bundle the desktop V4 ML artifact/ONNX runtime. If an
    # older persistent config has only a preset binding, make the Android-only
    # decision explicit instead of treating the missing desktop runtime as an
    # enabled user request. An explicit enabled= setting is never overwritten.
    _router_seed_status = "not-run"
    try:
        import re
        _config_path = os.path.join(os.environ["OPENSQUILLA_STATE_DIR"], "config.toml")
        if os.path.isfile(_config_path):
            with open(_config_path, "r", encoding="utf-8") as _config_file:
                _config_text = _config_file.read()
            _router_match = re.search(
                r"(?ms)^\[squilla_router\]\n(?P<body>.*?)(?=^\[|\Z)",
                _config_text,
            )
            if _router_match and not re.search(
                r"(?m)^\s*enabled\s*=", _router_match.group("body")
            ):
                _router_seed = "enabled = false\nrequire_router_runtime = false\n"
                _config_text = (
                    _config_text[:_router_match.start("body")]
                    + _router_seed
                    + _router_match.group("body")
                    + _config_text[_router_match.end("body"):]
                )
                with open(_config_path, "w", encoding="utf-8") as _config_file:
                    _config_file.write(_config_text)
                _router_seed_status = "disabled"
            else:
                _router_seed_status = "unchanged"
        else:
            _router_seed_status = "config-missing"
    except Exception as e:
        _router_seed_status = f"seed-error: {e!r}"

    # Diagnostics: capture stderr + periodic thread-dump (logcat is unreliable here).
    import faulthandler
    _diag = open(os.path.join(home_dir, "py_stderr.log"), "w", buffering=1)
    sys.stderr = _diag
    sys.stdout = _diag
    faulthandler.dump_traceback_later(40, repeat=True, file=_diag)

    # Android is a single-instance app: a stale gateway.pid (left behind by a
    # hard kill) blocks startup once the recorded pid gets reused by another
    # process — the gateway then refuses to boot ("already running") and the
    # app appears stuck on the launch screen. The pid-lock has no meaning for
    # us, so clear it before every boot.
    try:
        _pid_path = os.path.join(os.environ["OPENSQUILLA_STATE_DIR"], "state", "gateway.pid")
        if os.path.exists(_pid_path):
            _stale_pid = open(_pid_path, "r").read().strip()
            os.remove(_pid_path)
            print(f"[pidlock] cleared stale gateway.pid (pid={_stale_pid})", file=_diag)
        else:
            print("[pidlock] no stale gateway.pid", file=_diag)
    except Exception as _e:
        print(f"[pidlock] clear failed: {_e!r}", file=_diag)

    # Startup permission probe: can this (app) process see shared storage?
    try:
        _entries = os.listdir('/sdcard')
        print(f'[perm-check] /sdcard OK: {len(_entries)} entries, e.g. {_entries[:8]}', file=_diag)
    except Exception as e:
        print(f'[perm-check] /sdcard FAILED: {e!r}', file=_diag)

    from opensquilla.gateway.boot import start_gateway_server

    async def _main() -> int:
        # start_gateway_server() starts uvicorn as a background task and returns
        # immediately (same as the CLI). Hold the event loop open afterwards,
        # otherwise asyncio.run() would cancel the server task on return.
        await start_gateway_server(port=PORT)
        await asyncio.Event().wait()
        return 0

    try:
        return asyncio.run(_main())
    except Exception:
        traceback.print_exc()
        return 1