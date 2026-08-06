"""CLI commands for running the OpenAI-compatible HTTP bridge."""

from __future__ import annotations

import typer

app = typer.Typer(help="Run the OpenAI-compatible HTTP bridge.")


@app.command("run")
def run_openai_bridge(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        envvar="OPENAI_BRIDGE_HOST",
        help="监听地址（默认 127.0.0.1）。",
    ),
    port: int = typer.Option(
        8787,
        "--port",
        envvar="OPENAI_BRIDGE_PORT",
        help="监听端口（默认 8787）。",
    ),
    gateway_url: str = typer.Option(
        "ws://localhost:18791/ws",
        "--gateway",
        envvar="OPENAI_BRIDGE_GATEWAY_URL",
        help="OpenSquilla gateway WebSocket 地址。",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="OPENAI_BRIDGE_TOKEN",
        help="适配层 API Key（缺省自动生成随机值）。",
    ),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        envvar="OPENAI_BRIDGE_NO_AUTH",
        help="跳过适配层认证（仅限本地调试）。",
    ),
    session_mode: str = typer.Option(
        "persistent",
        "--session-mode",
        envvar="OPENAI_BRIDGE_SESSION_MODE",
        help="persistent=每模型常驻会话（agent 有记忆）；stateless=每请求新会话。",
    ),
    display_model: str = typer.Option(
        "OpenSquilla",
        "--display-model",
        envvar="OPENAI_BRIDGE_MODEL",
        help="对外暴露的模型名（默认 OpenSquilla），内部映射到 main agent。",
    ),
    timeout: float = typer.Option(
        180,
        "--timeout",
        envvar="OPENAI_BRIDGE_TIMEOUT",
        help="非流式响应最长等待秒数（默认 180）。",
    ),
) -> None:
    """启动 OpenAI 兼容 HTTP 桥接服务。

    将 OpenSquilla gateway 的 agent 能力暴露为 /v1/chat/completions，
    任何支持 OpenAI SDK 的客户端均可直接接入。
    """
    from opensquilla.openai_bridge import run_server

    run_server(
        host=host,
        port=port,
        gateway_url=gateway_url,
        bridge_token=token,
        no_auth=no_auth,
        session_mode=session_mode,
        display_model=display_model,
        timeout_s=timeout,
    )