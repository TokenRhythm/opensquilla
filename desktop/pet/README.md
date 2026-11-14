# 蛋仔 · OpenSquilla 桌面 Q 版桌宠

OpenSquilla 自己的桌面小伙伴 **蛋仔（Eggy）**：一只引用默认橙色 logo 的 Q 版蛋形宠物。
只做桌面宠物本体 + 气泡，**没有** AgentRMC 那种语音面板 / 完整面板。

能力：

- **语音提示**：任务完成 / 需要确认 / 方案选择 / 需要回答时出声（Windows 本地 SAPI，免 KEY；或 OpenAI 兼容云端 TTS）。
- **权限确认气泡**：`approval_required` → 显示工具 / 命令 / 风险，一键 **允许 / 拒绝**（直接调网关 `exec.approval.resolve`）。
- **方案选择 / 澄清气泡**：`plan_presentation` 与交互式 `user_input` 选择卡 → 渲染选项 / 表单，提交走 `chat.clarify_submit`。
- **直接回复会话**：单击蛋仔弹出回复框，选会话、输入内容，走网关 `sessions.send`（离线时回退到 companion 派发队列）。
- **音色 / 模型拉取**：设置里填 TTS Base URL / API Key，一键拉取 `/models` 与音色列表，试听。

## 运行

零运行时依赖：复用同仓库 `desktop/electron` 已安装的 Electron 二进制，WS / HTTP / fetch 全用 Node 内置。

```bat
:: 方式一：双击 / 命令行
desktop\pet\start-pet.cmd

:: 方式二：手动
desktop\electron\node_modules\electron\dist\electron.exe desktop\pet --enable-logging
```

启动后蛋仔出现在屏幕右下角，托盘有图标（显示/隐藏、静音、设置、退出）。

- **单击蛋仔**：挥手 + 打开「回复会话」气泡。
- **拖动蛋仔**：移动位置（自动记忆）。
- **右键蛋仔 / 托盘**：菜单。
- 空白区域鼠标穿透，不挡桌面操作。

## 从桌面 app 启动（集成）

OpenSquilla 桌面 app（[desktop/electron](../electron)）内置了蛋仔开关，无需手动跑脚本：

- 托盘菜单 → **蛋仔桌宠 (Eggy)** 复选项：勾选即拉起蛋仔，取消即关闭；选择会持久化
  （`%APPDATA%/@opensquilla/desktop-electron/desktop-pet.json`），下次启动桌面 app 自动恢复。
- 桌面 app 启动时若已启用，会自动把蛋仔作为子进程拉起，并通过 `EGGY_GATEWAY_URL`
  传入桌面 app 自己的网关地址，保证两者共用同一网关/会话；桌面 app 退出时自动带走蛋仔。
- 实现见 [desktop/electron/src/desktop-pet.ts](../electron/src/desktop-pet.ts)（自包含：独立偏好文件 + 子进程生命周期），
  在 `main.ts` 的托盘菜单 / `whenReady` / `will-quit` 三处挂钩。打包版若未 bundled 蛋仔目录则该开关自动置灰。

## 与网关的关系

蛋仔同时扮演两个角色：

1. **网关 WS 客户端**（`lib/gateway.js`）：握手 `connect` → 订阅 `sessions.subscribe` + 逐会话
   `sessions.messages.subscribe`，接收 `session.event.*`（payload 带 `session_key`），
   驱动富气泡与语音，并调用 RPC 回答审批 / 澄清 / 发送消息。默认 `ws://127.0.0.1:18791/ws`，
   `auth=none` 回环即 owner；`auth=token` 时在设置里填 Token。
2. **companion HTTP 服务**（`lib/companion.js`，默认 `127.0.0.1:45915`）：实现网关
   `opensquilla.pet` 服务对接的既有契约 —— `GET /state`、`POST /speak`、`POST /notify`、
   `GET /dispatch/pending?exe=`、`POST /dispatch/ack`（body ≤ 64KB，成功 204）。
   因此 `opensquilla pet say` / `pet_speak` / `pet_notify` 工具与网关 `[pet]` 服务可直接驱动蛋仔。

去重约定：WS 在线时由 WS 事件驱动气泡与语音；`/notify` 仅在 WS 离线时补位（模板气泡 + 语音），
`/speak` 始终出声。离线积压 spool（`%APPDATA%/LIG/AgentRMCNative/pending-events.jsonl`）启动时静默回放；
暂停文件 `%TEMP%/opensquilla-pet-bridge.pause` 存在即静音。

网关侧 `[pet]` 配置见仓库 `opensquilla.toml.example`（`bridge_url` 默认即 45915）。
蛋仔不修改网关与 AgentRMC 的任何契约。

## 配置

`%APPDATA%/opensquilla-pet/pet-config.json`（设置面板可视化编辑）：

- `gateway.url` / `gateway.token`
- `companion.port` / `companion.exe`
- `tts.provider`（`windows` | `openai`）、`baseUrl`、`apiKey`、`model`、`voice`、`voicesUrl`、`volume`、`rate`、`enabled`
- `pet.muted` / `autoSpeak` / `showTurnStart` / `size` / `x` / `y`
- `spool.dir` / `spool.pauseFile`（空 = 兼容默认路径）

## 目录

```
desktop/pet/
  main.js            主进程：窗口/托盘/网关/companion/TTS/IPC 编排
  preload.js         沙箱 contextBridge
  lib/config.js      配置持久化
  lib/gateway.js     网关 WS 客户端（内置 WebSocket）
  lib/companion.js   回环 HTTP 契约 + 派发队列
  lib/tts.js         Windows SAPI / OpenAI 兼容 TTS + 模型/音色拉取
  lib/spool.js       离线 spool / 暂停文件
  renderer/          蛋仔 SVG + 动画 + 气泡 + 设置
  assets/            默认 logo（肚皮印记 / 托盘图标）
  scripts/probe-gateway.mjs  网关协议只读探针
```

## 验证

```bat
node desktop\pet\scripts\probe-gateway.mjs
```

确认握手 / `sessions.list` / `exec.approvals.get` / 订阅成功。