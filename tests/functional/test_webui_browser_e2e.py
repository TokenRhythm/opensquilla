"""Opt-in real-browser smoke for the Control UI.

The default test suite skips this file. Run it with:

    OPENSQUILLA_WEBUI_BROWSER_E2E=1 uv run pytest tests/functional/test_webui_browser_e2e.py -q -s
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.webui_browser


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _node() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _install_playwright(work_dir: Path) -> None:
    result = subprocess.run(
        [_npm(), "--prefix", str(work_dir), "install", "playwright"],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    browser_result = subprocess.run(
        [_npm(), "--prefix", str(work_dir), "exec", "playwright", "install", "chromium"],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert browser_result.returncode == 0, browser_result.stderr or browser_result.stdout


def _wait_for_health(port: int, server: subprocess.Popen[str]) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 20.0
    last_error = ""
    while time.monotonic() < deadline:
        if server.poll() is not None:
            stdout = server.stdout.read() if server.stdout else ""
            stderr = server.stderr.read() if server.stderr else ""
            raise AssertionError(
                f"gateway exited early code={server.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
        except Exception as exc:  # noqa: BLE001 - included in timeout assertion.
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"gateway did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def test_control_ui_loads_in_real_browser(tmp_path: Path) -> None:
    if os.environ.get("OPENSQUILLA_WEBUI_BROWSER_E2E") != "1":
        pytest.skip("set OPENSQUILLA_WEBUI_BROWSER_E2E=1 to run browser smoke")

    port = _free_port()
    server_script = tmp_path / "webui_smoke_server.py"
    browser_script = tmp_path / "webui_smoke_browser.js"
    state_dir = tmp_path / "state"
    proposal_dir = state_dir / "proposals" / "deadbeef"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "SKILL.md").write_text(
        "---\n"
        "name: browser-audit-proposal\n"
        "kind: meta\n"
        "description: Browser smoke proposal.\n"
        "triggers: [browser audit proposal]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: classify\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: x}\n"
        "---\n"
        "# browser-audit-proposal\n",
        encoding="utf-8",
    )
    (proposal_dir / "gates.json").write_text(
        json.dumps(
            {
                "auto_enable_eligible": True,
                "auto_enable": {
                    "status": "skipped",
                    "reason": "risk_too_high",
                    "risk_level": "medium",
                    "max_risk": "low",
                    "details": {
                        "validation_profile": "static-safety-v2",
                        "skills": ["artifact-writer"],
                        "tools": [],
                        "reasons": ["capability:artifact-writer:filesystem-write"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    server_script.write_text(
        textwrap.dedent(
            f"""
            import uvicorn

            from opensquilla.gateway.app import create_gateway_app
            from opensquilla.gateway.config import AuthConfig, GatewayConfig

            config = GatewayConfig(
                host="127.0.0.1",
                port={port},
                auth=AuthConfig(mode="none"),
            )
            app = create_gateway_app(config)

            if __name__ == "__main__":
                uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
            """
        ),
        encoding="utf-8",
    )
    browser_script.write_text(
        textwrap.dedent(
            """
            const { chromium } = require("playwright");

            (async () => {
              const browser = await chromium.launch({ headless: true });
              const page = await browser.newPage();
              const errors = [];
              page.on("pageerror", err => errors.push(String(err)));
              const response = await page.goto(process.env.TARGET_URL, {
                waitUntil: "domcontentloaded",
                timeout: 30000,
              });
              await page.waitForSelector(".conn-pill.connected", { timeout: 15000 });
              const proposalRow = page.locator(".sk-proposal-row").filter({
                hasText: "deadbeef",
              });
              await proposalRow.waitFor({ state: "visible", timeout: 15000 });
              const showButton = proposalRow.getByRole("button", {
                name: "Show",
                exact: true,
              });
              await showButton.click();
              const proposalDialog = page.locator("dialog.sk-dialog[open]");
              await proposalDialog.locator(".sk-audit-grid").waitFor({ timeout: 15000 });
              const auditText = await proposalDialog.innerText();
              const result = {
                status: response ? response.status() : 0,
                title: await page.title(),
                path: new URL(page.url()).pathname,
                appCount: await page.locator("#app").count(),
                basePath: await page.locator("#opensquilla-data").getAttribute("data-base-path"),
                authMode: await page.locator("#opensquilla-data").getAttribute("data-auth-mode"),
                proposalRows: await proposalRow.count(),
                proposalShowButtons: await showButton.count(),
                auditText,
                pageErrors: errors,
              };
              await browser.close();
              console.log(JSON.stringify(result));
            })().catch(err => {
              console.error(err && err.stack ? err.stack : String(err));
              process.exit(1);
            });
            """
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["OPENSQUILLA_STATE_DIR"] = str(state_dir)
    server = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_health(port, server)
        _install_playwright(tmp_path)
        browser_env = dict(env, TARGET_URL=f"http://127.0.0.1:{port}/control/skills")
        result = subprocess.run(
            [_node(), str(browser_script)],
            cwd=tmp_path,
            env=browser_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        _stop_process(server)

    assert payload["status"] == 200
    assert payload["title"] == "Skills — OpenSquilla"
    assert payload["path"] == "/control/skills"
    assert payload["appCount"] == 1
    assert payload["basePath"] == "/control"
    assert payload["authMode"] == "none"
    assert payload["proposalRows"] == 1
    assert payload["proposalShowButtons"] == 1
    assert "Auto-enable Audit" in payload["auditText"]
    assert "static-safety-v2" in payload["auditText"]
    assert "medium / low" in payload["auditText"]
    assert "capability:artifact-writer:filesystem-write" in payload["auditText"]
    assert payload["pageErrors"] == []
