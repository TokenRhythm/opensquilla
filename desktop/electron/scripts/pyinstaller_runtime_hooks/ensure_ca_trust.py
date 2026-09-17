"""Restore a usable default CA store in the frozen Desktop gateway."""

from __future__ import annotations

import importlib
import os
import ssl
import sys
from pathlib import Path

_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_PACKAGING_ERROR = (
    "OpenSquilla Desktop could not initialize its packaged TLS trust store. "
    "Reinstall OpenSquilla Desktop or rebuild the Desktop gateway with the certifi CA bundle."
)


def _remove_blank_ca_overrides() -> None:
    for name in _CA_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and not value.strip():
            os.environ.pop(name, None)


def _default_context_has_ca_certificates() -> bool:
    context = ssl.create_default_context()
    return bool(context.get_ca_certs(binary_form=True))


def _certifi_bundle_path() -> Path:
    try:
        certifi = importlib.import_module("certifi")
        bundle = Path(certifi.where())
        if not bundle.is_file() or bundle.stat().st_size == 0:
            raise OSError
    except Exception:
        raise RuntimeError(_PACKAGING_ERROR) from None
    return bundle


def ensure_frozen_default_ca_trust() -> None:
    if not getattr(sys, "frozen", False):
        return

    _remove_blank_ca_overrides()
    if any(os.environ.get(name) for name in _CA_ENV_VARS):
        # Explicit operator and enterprise trust configuration remains authoritative.
        return

    try:
        if _default_context_has_ca_certificates():
            return
    except Exception:
        # A broken or unreadable platform trust path is exactly when the
        # packaged certifi bundle is needed. Validate that fallback below.
        pass

    os.environ["SSL_CERT_FILE"] = os.fspath(_certifi_bundle_path())
    try:
        fallback_is_usable = _default_context_has_ca_certificates()
    except Exception:
        raise RuntimeError(_PACKAGING_ERROR) from None
    if not fallback_is_usable:
        raise RuntimeError(_PACKAGING_ERROR)


ensure_frozen_default_ca_trust()
