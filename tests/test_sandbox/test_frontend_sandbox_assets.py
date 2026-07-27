"""Sandbox-facing contracts in the active Vue Control UI."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEBUI_SOURCE = ROOT / "opensquilla-webui" / "src"
WEBUI_LOCALES = WEBUI_SOURCE / "locales"
SHARED_ROUTES = WEBUI_SOURCE / "router" / "sharedRoutes.ts"
CHAT_VIEW = WEBUI_SOURCE / "views" / "ChatView.vue"
RUN_MODE_COMPONENT = WEBUI_SOURCE / "components" / "chat" / "ChatComposerRunMode.vue"
CHAT_SEND = WEBUI_SOURCE / "composables" / "chat" / "useChatSend.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_vue_has_no_standalone_sandbox_or_approvals_destination() -> None:
    routes = _read(SHARED_ROUTES)

    assert "path: '/sandbox'" not in routes
    assert "path: '/approvals'" in routes
    assert "redirect: '/sessions'" in routes


def test_vue_chat_wires_policy_limited_run_mode_into_send_metadata() -> None:
    view = _read(CHAT_VIEW)
    run_mode = _read(RUN_MODE_COMPONENT)
    send = _read(CHAT_SEND)

    assert ':run-mode="runMode"' in view
    assert ':allowed-run-modes="allowedRunModes"' in view
    assert '@set-run-mode="setComposerRunMode"' in view
    assert "SANDBOX_RUN_MODES" in run_mode
    assert "const fullDisabled" in run_mode
    assert "return !allowedRunModes.value.includes(mode)" in run_mode
    assert "runMode: normalizeSandboxRunMode(options.runMode.value)" in send


def test_webui_run_mode_locale_source_matches_managed_execution() -> None:
    english = json.loads(_read(WEBUI_LOCALES / "en.json"))
    chinese = json.loads(_read(WEBUI_LOCALES / "zh-Hans.json"))

    assert english["chat"]["composer"]["runModeTrusted"] == "Managed Execution"
    assert chinese["chat"]["composer"]["runModeTrusted"] == "托管执行"


def test_removed_trusted_sandbox_term_is_absent_from_all_webui_sources() -> None:
    text_suffixes = {
        ".css",
        ".html",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".svg",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".yaml",
        ".yml",
    }
    source_files = sorted(
        path for path in WEBUI_SOURCE.rglob("*") if path.is_file() and path.suffix in text_suffixes
    )

    assert source_files
    for path in source_files:
        source = _read(path)
        assert "Trusted-Sandbox" not in source, path.relative_to(ROOT)
        assert "可信沙箱" not in source, path.relative_to(ROOT)
