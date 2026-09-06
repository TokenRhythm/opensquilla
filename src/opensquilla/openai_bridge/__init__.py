"""OpenAI 兼容 HTTP 桥接层。

将 OpenSquilla gateway 的 agent 能力以 OpenAI 兼容端点（/v1/chat/completions）
暴露给任何支持 OpenAI SDK 的平台/工具。

直接启动： ``python -m opensquilla.openai_bridge.server``
CLI 启动： ``opensquilla openai-bridge run``
"""

from opensquilla.openai_bridge.server import create_app, run_server  # noqa: F401