"""Shared fixtures for CLI command tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_local_tui_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize doctor's CLI-local terminal-UI probe for every CLI test.

    Whether OpenTUI is available depends on the developer machine (companion
    module, bun, node_modules), so any test that invokes ``opensquilla
    doctor`` would otherwise be environment-dependent. Tests for the TUI
    finding itself re-set ``_local_tui_findings`` explicitly.
    """
    monkeypatch.setattr("opensquilla.cli.doctor_cmd._local_tui_findings", lambda: [])
