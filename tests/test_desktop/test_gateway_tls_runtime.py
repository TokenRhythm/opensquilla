import importlib.util
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = (
    ROOT
    / "desktop"
    / "electron"
    / "scripts"
    / "pyinstaller_runtime_hooks"
    / "ensure_ca_trust.py"
)
_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")


class _FakeContext:
    def __init__(self, *, has_ca_certificates: bool) -> None:
        self._has_ca_certificates = has_ca_certificates

    def get_ca_certs(self, *, binary_form: bool = False) -> list[bytes]:
        assert binary_form is True
        return [b"synthetic-ca"] if self._has_ca_certificates else []


@pytest.fixture(autouse=True)
def restore_ca_environment() -> Iterator[None]:
    """Keep hook tests from exporting a temporary CA bundle to later tests."""

    original = {name: os.environ.get(name) for name in _CA_ENV_VARS}
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def ca_hook(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delattr(sys, "frozen", raising=False)
    spec = importlib.util.spec_from_file_location("opensquilla_desktop_ca_hook_test", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def _clear_ca_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_non_frozen_runtime_is_untouched(
    ca_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "   ")
    monkeypatch.setattr(
        ca_hook.ssl,
        "create_default_context",
        lambda: pytest.fail("non-frozen runtime must not inspect the CA store"),
    )

    ca_hook.ensure_frozen_default_ca_trust()

    assert os.environ["SSL_CERT_FILE"] == "   "


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_nonempty_ca_override_remains_authoritative(
    ca_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    _set_frozen(monkeypatch)
    _clear_ca_environment(monkeypatch)
    monkeypatch.setenv(variable, "/synthetic/enterprise-trust")
    monkeypatch.setattr(
        ca_hook.ssl,
        "create_default_context",
        lambda: pytest.fail("explicit CA configuration must not be replaced or probed"),
    )

    ca_hook.ensure_frozen_default_ca_trust()

    assert os.environ[variable] == "/synthetic/enterprise-trust"


def test_healthy_frozen_system_ca_store_is_unchanged(
    ca_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_frozen(monkeypatch)
    _clear_ca_environment(monkeypatch)
    monkeypatch.setattr(
        ca_hook.ssl,
        "create_default_context",
        lambda: _FakeContext(has_ca_certificates=True),
    )
    monkeypatch.setattr(
        ca_hook.importlib,
        "import_module",
        lambda _name: pytest.fail("healthy system trust must not load certifi"),
    )

    ca_hook.ensure_frozen_default_ca_trust()

    assert "SSL_CERT_FILE" not in os.environ
    assert "SSL_CERT_DIR" not in os.environ


def test_empty_frozen_ca_store_falls_back_to_certifi(
    ca_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_frozen(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", " \t")
    monkeypatch.setenv("SSL_CERT_DIR", "")
    bundle = tmp_path / "cacert.pem"
    bundle.write_bytes(b"synthetic-ca-bundle")
    contexts = iter(
        [
            _FakeContext(has_ca_certificates=False),
            _FakeContext(has_ca_certificates=True),
        ]
    )
    monkeypatch.setattr(ca_hook.ssl, "create_default_context", lambda: next(contexts))
    monkeypatch.setattr(
        ca_hook.importlib,
        "import_module",
        lambda name: SimpleNamespace(where=lambda: str(bundle))
        if name == "certifi"
        else pytest.fail(f"unexpected import: {name}"),
    )

    ca_hook.ensure_frozen_default_ca_trust()

    assert os.environ["SSL_CERT_FILE"] == str(bundle)
    assert "SSL_CERT_DIR" not in os.environ


def test_initial_default_context_failure_falls_back_to_certifi(
    ca_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_frozen(monkeypatch)
    _clear_ca_environment(monkeypatch)
    bundle = tmp_path / "cacert.pem"
    bundle.write_bytes(b"synthetic-ca-bundle")
    contexts = iter(
        [
            OSError("compiled trust path is unavailable"),
            _FakeContext(has_ca_certificates=True),
        ]
    )

    def create_context() -> _FakeContext:
        result = next(contexts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ca_hook.ssl, "create_default_context", create_context)
    monkeypatch.setattr(
        ca_hook.importlib,
        "import_module",
        lambda name: SimpleNamespace(where=lambda: str(bundle))
        if name == "certifi"
        else pytest.fail(f"unexpected import: {name}"),
    )

    ca_hook.ensure_frozen_default_ca_trust()

    assert os.environ["SSL_CERT_FILE"] == str(bundle)


def test_missing_certifi_bundle_fails_with_redacted_action(
    ca_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_frozen(monkeypatch)
    _clear_ca_environment(monkeypatch)
    secret_path = tmp_path / "secret-value-must-not-appear.pem"
    monkeypatch.setattr(
        ca_hook.ssl,
        "create_default_context",
        lambda: _FakeContext(has_ca_certificates=False),
    )
    monkeypatch.setattr(
        ca_hook.importlib,
        "import_module",
        lambda _name: SimpleNamespace(where=lambda: str(secret_path)),
    )

    with pytest.raises(RuntimeError) as exc_info:
        ca_hook.ensure_frozen_default_ca_trust()

    message = str(exc_info.value)
    assert "Reinstall OpenSquilla Desktop" in message
    assert str(secret_path) not in message


def test_invalid_certifi_bundle_fails_post_fallback_validation(
    ca_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_frozen(monkeypatch)
    _clear_ca_environment(monkeypatch)
    bundle = tmp_path / "invalid.pem"
    bundle.write_bytes(b"synthetic-invalid-ca")
    contexts = iter([_FakeContext(has_ca_certificates=False), OSError("private detail")])

    def create_context() -> _FakeContext:
        result = next(contexts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ca_hook.ssl, "create_default_context", create_context)
    monkeypatch.setattr(
        ca_hook.importlib,
        "import_module",
        lambda _name: SimpleNamespace(where=lambda: str(bundle)),
    )

    with pytest.raises(RuntimeError) as exc_info:
        ca_hook.ensure_frozen_default_ca_trust()

    message = str(exc_info.value)
    assert "packaged TLS trust store" in message
    assert "private detail" not in message


def test_desktop_build_and_smoke_wire_the_ca_contract() -> None:
    build_source = (ROOT / "desktop/electron/scripts/build-gateway.mjs").read_text(
        encoding="utf-8"
    )
    entry_source = (ROOT / "desktop/electron/scripts/gateway-entry.py").read_text(
        encoding="utf-8"
    )
    smoke_source = (ROOT / "desktop/electron/scripts/smoke-gateway.mjs").read_text(
        encoding="utf-8"
    )
    project_source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "'--runtime-hook',\n  caRuntimeHookPath," in build_source
    assert "'--collect-data',\n  'certifi'," in build_source
    assert "'--hidden-import',\n  'certifi'," in build_source
    assert '"certifi>=2024.7.4"' in project_source
    assert "--_desktop-ca-probe" in entry_source
    assert "--internal-child" in entry_source
    assert "--_sandbox-filesystem-worker" in entry_source
    assert "--elevated-helper" in entry_source
    assert "x509_ca={ca_certificate_count}" in entry_source
    assert "spawnSync(gatewayBinary, ['--_desktop-ca-probe']" in smoke_source
    assert (
        "spawnSync(gatewayBinary, ['--internal-child', 'filesystem-worker', '-']"
        in smoke_source
    )
    assert "caCertificateCount <= 0" in smoke_source
    for name in (
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ):
        assert f"'{name}'" in smoke_source


def test_gateway_entry_runs_hidden_filesystem_worker_without_entering_cli(
    tmp_path: Path,
) -> None:
    gateway_entry = ROOT / "desktop/electron/scripts/gateway-entry.py"
    target = tmp_path / "worker-probe.txt"
    target.write_text("worker-ok\n", encoding="utf-8")
    payload = {
        "kind": "read_file",
        "path": str(target),
        "displayPath": str(target),
    }

    completed = subprocess.run(
        [sys.executable, str(gateway_entry), "--_sandbox-filesystem-worker"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["message"].splitlines() == ["1\tworker-ok"]
    assert completed.stderr == ""


def test_gateway_entry_runs_canonical_internal_filesystem_worker(
    tmp_path: Path,
) -> None:
    gateway_entry = ROOT / "desktop/electron/scripts/gateway-entry.py"
    target = tmp_path / "worker-probe.txt"
    target.write_text("worker-ok\n", encoding="utf-8")
    payload = {
        "kind": "read_file",
        "path": str(target),
        "displayPath": str(target),
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(gateway_entry),
            "--internal-child",
            "filesystem-worker",
            "-",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["message"].splitlines() == ["1\tworker-ok"]
    assert completed.stderr == ""


def test_desktop_gateway_entry_routes_sandbox_helper_before_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_path = ROOT / "desktop/electron/scripts/gateway-entry.py"
    helper_arguments: list[list[str]] = []
    app_called = False

    def elevated_setup_helper_main(arguments: list[str]) -> int:
        helper_arguments.append(arguments)
        return 17

    def app() -> None:
        nonlocal app_called
        app_called = True

    helper_module = ModuleType("opensquilla.sandbox.backend.windows_default_setup")
    helper_module.elevated_setup_helper_main = elevated_setup_helper_main
    cli_module = ModuleType("opensquilla.cli.main")
    cli_module.app = app
    monkeypatch.setitem(
        sys.modules,
        "opensquilla.sandbox.backend.windows_default_setup",
        helper_module,
    )
    monkeypatch.setitem(sys.modules, "opensquilla.cli.main", cli_module)
    monkeypatch.setattr(sys, "argv", [str(entry_path), "--elevated-helper", "payload"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(entry_path), run_name="__main__")

    assert helper_arguments == [["--elevated-helper", "payload"]]
    assert exc_info.value.code == 17
    assert app_called is False
