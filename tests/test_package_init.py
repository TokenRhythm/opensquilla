"""Package version access must not burden the controlled-launch cold path."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_INIT = Path(__file__).resolve().parents[1] / "src" / "opensquilla" / "__init__.py"


@pytest.fixture
def package_init():
    spec = importlib.util.spec_from_file_location("_opensquilla_init_test", _INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, spec.loader


def test_cold_package_import_does_not_load_metadata():
    script = """
import sys
sys.path.insert(0, sys.argv[1])
class NoMetadata:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'importlib.metadata':
            raise AssertionError('package initialization imported metadata')
sys.meta_path.insert(0, NoMetadata())
import opensquilla
assert '__version__' not in vars(opensquilla)
assert '__version__' in dir(opensquilla)
assert 'importlib.metadata' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-S", "-c", script, str(_INIT.parents[1])],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_version_resolves_once_and_preserves_from_import(package_init, monkeypatch):
    package, _ = package_init
    calls = []

    def resolve(name):
        calls.append(name)
        return "1.2.3rc4"

    monkeypatch.setattr(importlib.metadata, "version", resolve)
    monkeypatch.setitem(sys.modules, package.__name__, package)
    namespace = {}
    exec("from _opensquilla_init_test import __version__", namespace)
    assert namespace["__version__"] == "1.2.3rc4"
    assert package.__version__ == "1.2.3rc4"
    assert calls == ["opensquilla"]
    star_namespace = {}
    exec("from _opensquilla_init_test import *", star_namespace)
    assert star_namespace["__version__"] == "1.2.3rc4"


def test_version_missing_metadata_preserves_fallback(package_init, monkeypatch):
    package, _ = package_init

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert package.__version__ == "0.0.0+unknown"


def test_version_does_not_hide_other_metadata_errors(package_init, monkeypatch):
    package, _ = package_init

    def broken(name):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(importlib.metadata, "version", broken)
    with pytest.raises(OSError, match="metadata unavailable"):
        _ = package.__version__
    assert "__version__" not in vars(package)


def test_unknown_attribute_does_not_resolve_version(package_init, monkeypatch):
    package, _ = package_init

    def unexpected(name):
        pytest.fail("unknown attribute attempted version resolution")

    monkeypatch.setattr(importlib.metadata, "version", unexpected)
    with pytest.raises(AttributeError, match="has no attribute 'missing'"):
        _ = package.missing


def test_reloading_package_invalidates_version_cache(package_init, monkeypatch):
    package, loader = package_init
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
    assert package.__version__ == "1.2.3"
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.4")
    loader.exec_module(package)
    assert "__version__" not in vars(package)
    assert package.__version__ == "1.2.4"
