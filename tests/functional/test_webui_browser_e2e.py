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


def _install_playwright(work_dir: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        [_npm(), "--prefix", str(work_dir), "install", "playwright"],
        cwd=work_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    browser_result = subprocess.run(
        [_npm(), "--prefix", str(work_dir), "exec", "playwright", "install", "chromium"],
        cwd=work_dir,
        env=env,
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
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    profile_dir = tmp_path / "profile"
    skill_dir = profile_dir / "workspace" / "skills" / "browser-smoke-skill"
    skill_dir.mkdir(parents=True)
    (profile_dir / "config.toml").write_text('[auth]\nmode = "none"\n', encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: browser-smoke-skill\n"
        "description: An ordinary skill for the browser smoke test.\n"
        "---\n"
        "# Browser smoke skill\n\n"
        "Summarize the supplied text in one sentence.\n",
        encoding="utf-8",
    )
    server_script.write_text(
        textwrap.dedent(
            f"""
            import os
            from pathlib import Path

            import uvicorn

            from opensquilla.gateway.app import create_gateway_app
            from opensquilla.gateway.config import AuthConfig, GatewayConfig
            from opensquilla.skills.loader import SkillLoader

            profile = Path(os.environ["OPENSQUILLA_STATE_DIR"])
            config = GatewayConfig(
                host="127.0.0.1",
                port={port},
                auth=AuthConfig(mode="none"),
                config_path=str(profile / "config.toml"),
                state_dir=str(profile / "state"),
                workspace_dir=str(profile / "workspace"),
            )
            # Every catalog source is explicit; only the synthetic workspace is loaded.
            loader = SkillLoader(
                bundled_dir=None,
                workspace_dir=profile / "workspace" / "skills",
                managed_dir=None,
                personal_agents_dir=None,
                project_agents_dir=None,
                extra_dirs=[],
                snapshot_path=profile / "cache" / "skills_snapshot.json",
                lockfile_path=profile / "skills-lock.json",
            )
            loader.load_all()
            app = create_gateway_app(config, skill_loader=loader)

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
              const page = await browser.newPage({ locale: "en-US" });
              const errors = [];
              page.on("pageerror", err => errors.push(String(err)));
              const response = await page.goto(process.env.TARGET_URL, {
                waitUntil: "domcontentloaded",
                timeout: 30000,
              });
              await page.waitForSelector(".conn-pill.connected", { timeout: 15000 });
              const catalog = page.getByTestId("skills-catalog");
              const skillTile = catalog.locator("button.sk-tile").filter({
                has: page.getByText("browser-smoke-skill", { exact: true }),
              });
              await skillTile.waitFor({ state: "visible", timeout: 15000 });
              const skillNames = await catalog.locator(".sk-tile__name").allTextContents();
              await skillTile.click();
              const skillDialog = page.locator("dialog.sk-dialog[open]");
              const content = skillDialog.locator(".sk-detail__pre").filter({
                hasText: "Summarize the supplied text in one sentence.",
              });
              await content.waitFor({ state: "visible", timeout: 15000 });
              const result = {
                status: response ? response.status() : 0,
                title: await page.title(),
                path: new URL(page.url()).pathname,
                appCount: await page.locator("#app").count(),
                basePath: await page.locator("#opensquilla-data").getAttribute("data-base-path"),
                authMode: await page.locator("#opensquilla-data").getAttribute("data-auth-mode"),
                connected: await page.locator(".conn-pill.connected").isVisible(),
                retiredControls: await page.locator(
                  ".sk-group--ap-settings, .sk-group--meta, .sk-proposal-row"
                ).count(),
                skillNames,
                detailName: await skillDialog.locator(".sk-detail__name").innerText(),
                detailDescription: await skillDialog.locator(".sk-detail__desc").innerText(),
                skillContent: await content.innerText(),
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

    # Keep all app state and browser tooling out of the operator's profile.
    env = {key: value for key, value in os.environ.items() if not key.startswith("OPENSQUILLA_")}
    env.update(
        HOME=str(home_dir),
        USERPROFILE=str(home_dir),
        APPDATA=str(home_dir / "AppData" / "Roaming"),
        LOCALAPPDATA=str(home_dir / "AppData" / "Local"),
        XDG_CONFIG_HOME=str(home_dir / ".config"),
        XDG_CACHE_HOME=str(home_dir / ".cache"),
        XDG_DATA_HOME=str(home_dir / ".local" / "share"),
        OPENSQUILLA_STATE_DIR=str(profile_dir),
        OPENSQUILLA_USER_STATE_DIR=str(profile_dir / "user-state"),
        OPENSQUILLA_LOG_DIR=str(profile_dir / "logs"),
        OPENSQUILLA_TURN_CALL_LOG="0",
        PLAYWRIGHT_BROWSERS_PATH=os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers")
        ),
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
    )
    _install_playwright(tmp_path, env)
    server = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_health(port, server)
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
    assert payload["connected"] is True
    assert payload["retiredControls"] == 0
    assert payload["skillNames"] == ["browser-smoke-skill"]
    assert payload["detailName"] == "browser-smoke-skill"
    assert payload["detailDescription"] == "An ordinary skill for the browser smoke test."
    assert "# Browser smoke skill" in payload["skillContent"]
    assert "Summarize the supplied text in one sentence." in payload["skillContent"]
    assert payload["pageErrors"] == []
