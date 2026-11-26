# PRD：将 AgentRMC 语音能力（识别 / 桌宠提醒 / 定向会话）内置进 OpenSquilla

- **状态**：Draft v0.3（§12.1 落位决策已定：(a) core 网关常驻服务，经 `OptionalServiceOwner` 挂载；插件市场上线后可整体降格为内置插件，边界约定见 §7.4。**v0.3 新增**：§5.0 语音能力信息架构（识别 / 生成 两轴分离 + Voice Preset 音色预设体系）与 F5 音色预设；能力页拆「语音识别」与「语音生成与音频工具」两张卡，桌宠播报音色消费预设，§14 给出桌宠音色下发路线）
- **RFC 草稿**：`RFC-issue-draft-desktop-companion.md`（同目录，英文，可直发上游 issue）
- **日期**：2026-11-12
- **目标仓库**：`opensquilla/opensquilla`（main @ `fdc3898a3`，2026-11-10）
- **来源项目**：`Agentrmc`（Qt/C++ 桌宠，native @ 0.3.9+；外部桥 `plugins/opensquilla-bridge/`）
- **PR 形态**：按 §10 拆为 4 个串联 PR（上游要求 small & focused，见 `CONTRIBUTING.md`）

---

## 1. TL;DR

把现在**挂在 OpenSquilla 体外**的三项语音能力收编为 OpenSquilla 一等公民功能：

1. **语音识别（ASR）**——多 Provider 抽象：本地 SenseVoice（离线）+ OpenAI 兼容云端双协议 + 保留现有 ElevenLabs；打通 WebUI 麦克风、通道语音消息、CLI/TUI 一键录音输入。
2. **桌宠语音提醒**——网关内置常驻服务订阅进程内会话事件，直接驱动桌宠 `/notify` `/speak`（轮次结束 / 等审批 / 等选方案时出声、按需拉回窗口），淘汰外部 Node 桥 + VBS + 计划任务看门狗三层拼装。
3. **桌宠定向会话**——网关内置派发收件器：桌宠里说/写的话 → `/dispatch/pending?exe=OpenSquilla.exe` 取件 → 会话守卫校验 → `sessions.send` 幂等注入 → `/dispatch/ack` 回执。

**范围红线**：只做 OpenSquilla 自己的这三条链路；不实现、不改动 Qoder / ZCode / DSH / WorkBuddy 等其他宿主的提醒与定向派发；不改桌宠（AgentRMC native）与 AgentBridge 协议一行。

---

## 2. 背景与问题

### 2.1 现状架构（2026-11 实测）

```
OpenSquilla 网关 :18791 (ws)
   └─ (体外的) opensquilla-pet-bridge.mjs ——零依赖 Node 常驻脚本
        ├─ WS 订阅 sessions.changed / session.event.*
        ├─ POST 127.0.0.1:45915 /notify  → 桌宠气泡 + TTS + 拉前台
        └─ GET  /dispatch/pending?exe=OpenSquilla.exe（1.5s 轮询）
              → sessions.resolve 守卫 → sessions.send 注入 → /dispatch/ack

桌宠语音输入(识别)：仅存在于 AgentRMC 本体（local SenseVoice / 云 AsrClient）
OpenSquilla 语音输入(识别)：仅 WebUI 麦克风 → /api/audio/transcribe → ElevenLabs Scribe 单 Provider
```

### 2.2 痛点

| # | 痛点 | 证据 |
|---|---|---|
| P1 | 桥的生命周期脆弱：从 tool call 派生的进程 15–30s 静默消失；`.vbs` 写成 JScript 导致看门狗**从未成功拉起过一次**（2026-11-02 事故，语音提示全灭） | `agentrmc-voice-reminder` 技能硬规则 7/8 + 事故记录 |
| P2 | 部署面大：要装 Startup VBS + 每 5 分钟 schtasks 看门狗 + 心跳文件判假活，全是用户手动步骤 | `plugins/opensquilla-bridge/README.md` 安装四步 |
| P3 | 事件桥靠网关 WS 回环订阅，半开连接要自研探针（sessions.list×3 失败强重连、20s 握手限时、心跳文件区分卡死） | 桥源码 L34/L280 注释 |
| P4 | ASR 只有 ElevenLabs 一家：无本地离线识别、无 OpenAI 兼容端点支持，中文长句/隐私场景没法选本地 SenseVoice；通道语音消息、CLI/TUI 均无语音入口 | `gateway/audio_transcription.py`（provider_factory 仅 ElevenLabs） |
| P5 | 定向会话依赖桥常驻；桥一死，桌宠里给 OpenSquilla 派发的指令只落草稿，用户误以为已送达 | README「离线补投」段仅覆盖 notify，不覆盖 dispatch |

### 2.3 为什么值得内置（PR 立论）

- 三项能力都以 **OpenSquilla 侧状态机/事件流为核心**，进程内订阅可以消灭 P1–P3 的全部分布式故障模式（网关重启→服务随之重启；无跨进程保活）。
- OpenSquilla 已有音频基础设施（`provider/audio.py`、`gateway/audio_transcription.py`、`onboarding/audio_specs.py`、`application/capability_setup.AudioCapabilitySetup`），ASR 是顺路抽象，不是新造轮子。
- AgentBridge 是纯回环 HTTP 契约，桌宠端零改动即可对接；上游维护者可在 CI 用假桥服务器测试（满足「默认测试离线」红线）。

---

## 3. 目标 / 非目标

### 3.1 目标（Goals）

| ID | 目标 | 衡量 |
|---|---|---|
| G1 | OpenSquilla 网关进程内置桌宠提醒服务，事件到 `/notify` P50 ≤ 1.5s | E2E 计时 |
| G2 | 桌宠→会话定向派发在网关内置实现，端到端幂等（同 dispatch_id 重投不产生第二条消息） | 契约测试 |
| G3 | ASR Provider 抽象：`elevenlabs / openai_transcriptions / openai_chat / sensevoice` 四家可配置、可降级（含本地静音门与设备选择，FR-1.7） | 单测覆盖四 Provider + E2E |
| G4 | 外部桥退役路径：内置服务与 `opensquilla-pet-bridge.mjs` 功能对等（README 给出迁移开关） | 对等清单 §8.2 |
| G5 | 默认全关（opt-in），不装桌宠/不配置的用户零感知、零额外端口探测 | 配置门控 |
| G6（v0.3） | 能力页信息架构与配置树按 §5.0 两轴分离；生成轴能力（TTS/克隆/dubbing/音乐）以 ElevenLabs 为基线全部可配可用；Voice Preset 预设体系（F3.6）可供桌宠消费 | UI E2E §9 + 单测 |

### 3.2 非目标（Non-Goals）

- ❌ OpenSquilla 侧实现 TTS 或音频播放（出声永远是桌宠的事；本期「语音生成与音频工具」卡只做**配置与可用性管理**——密钥、预设、工具开关、试听，实际播报输出仍由桌宠/工具调用方负责）。
- ❌ 实现/改造桌宠 UI、SenseVoice 模型训练、唤醒词、常驻监听（听麦克风仍是桌宠能力；本期 OpenSquilla 的 ASR 只做「已有音频 → 文本」）。
- ❌ 任何其他 Agent 宿主（Qoder/ZCode/DSH/WorkBuddy）的语音提醒、定向会话、工单（work-items）语义。
- ❌ AgentBridge 协议扩展（不加新端点、不改字段；`/work-items/*`、`/command`、`/workers` 不在本期）。
- ❌ 修改 v4 网关契约的事件名。
- ❌ 把 `agentrmc-bridge`（Qoder 插件线）搬进 OpenSquilla。

---

## 4. 用户与场景

| 角色 | 场景 | 现在 | 内置后 |
|---|---|---|---|
| OpenSquilla 主用户（Windows + 桌宠） | 长任务跑完/等审批时人不在电脑前 | 手动装桥+VBS+计划任务，坏了不自知 | `opensquilla.toml` 开 `[pet]`，网关带起服务，`opensquilla doctor` 可查 |
| 同上 | 在桌宠里说「把 PRD 第三节改紧凑」，期望落进**指定 OpenSquilla 会话** | 依赖桥 1.5s 轮询 | 网关内置收件，注入+ack 同一状态机，落账可查 |
| 无桌宠用户 / Linux/macOS | 语音发消息、录音转文字 | 只有 ElevenLabs；通道语音无转写 | 任选兼容 OpenAI 的 ASR 端点（含 MiMo chat 协议）；可选本地模型 |
| CLI/TUI 重度用户 | 按住热键说一段 prompt | 无 | push-to-talk：录 → 转 → 填入输入框（可改配置直接发送） |
| 隐私敏感用户 | 中文语音不出网 | 无离线选项 | `local_sensevoice`（`opensquilla[voice]` extra） |

平台说明：桌宠与 AgentBridge 是 Windows-only，`[pet]` 服务在非 Windows 上启动即禁用并在 doctor 里标注；ASR 能力跨平台。

---

## 5. 功能需求

### 5.0 语音能力信息架构（v0.3）

**两轴模型**：「识别（ASR / 转写）」与「生成（TTS / 克隆 / dubbing / 音乐）」是两条正交的能力轴，各自选型、各自配置，互不隶属。ElevenLabs 横跨两轴（有 Scribe 也有 TTS/克隆/音乐），因此它在 UI 里**只归属生成轴**（全家桶供应商）；识别轴的引擎下拉里若选到 ElevenLabs，语义是「复用生成卡里已配置的 ElevenLabs 做转写」，不重复填密钥。

```
┌─ 能力 → 语音识别（新卡）──────────────┐   ┌─ 能力 → 语音生成与音频工具（原「语音与音频」卡）─┐
│ 引擎下拉：                             │   │ 供应商：ElevenLabs（密钥/环境引用/base_url）      │
│  · 本地 SenseVoice（离线，免费）       │   │ 默认 TTS：音色 = 预设下拉 + 试听按钮               │
│    └ 模型目录 / 语言 / ITN / 线程数    │   │ 进阶（折叠）：TTS 模型、输出格式、稳定性等参数     │
│  · OpenAI 兼容 /audio/transcriptions   │   │ 音频工具开关：语音克隆 / dubbing / 音乐生成        │
│  · OpenAI 兼容 chat ASR（input_audio） │   │  （复用既有 tools/builtin/media.py 后端）          │
│  · ElevenLabs Scribe（引用生成卡）     │   └────────────────────────────────────────────────────┘
└───────────────────────────────────────┘
            ▲                                        ▲
            │ 输入：WebUI 麦克风 / 通道语音 / CLI     │ 输出：Agent 工具、桌宠播报（§14）
            └────────────── Voice Preset ─────────────┘
                  （命名音色包：voice+model+参数，桌宠播报与 Agent TTS 共用）
```

**Voice Preset（音色预设）**：命名的可复用配置包——`{name, voice_id, model_id, stability, similarity_boost, style, use_speaker_boost, speed, language_code}`。消费方两个：① 生成卡「默认 TTS」下拉（写 `[audio.tts]`，保持向后兼容：未选预设 = 沿用 `[audio.tts]` 裸配置）；② 桌宠播报音色（§14 通过 AgentBridge 扩展下发 voice_id 参数；本期只做预设的 CRUD + 试听，桌宠侧下发为后续 PR）。

**配置映射**（与实现同步，见 F4）：识别轴落 `[audio.stt]`（provider 枚举含 `sensevoice | openai_transcriptions | openai_chat | elevenlabs`）；生成轴落 `[audio]` + `[audio.providers.elevenlabs]` + `[audio.tts]`；预设落 `[audio.presets.<name>]`。`provider = "elevenlabs"` 时识别复用生成卡的密钥与 base_url，UI 在识别卡显示来源提示「在『语音生成与音频工具』中配置」。

### F1 语音识别（ASR）

- **FR-1.1 Provider 抽象**：定义 `SttProvider`（async `transcribe(audio: AudioBytes, *, language, hints) -> SttResult`），现有 ElevenLabs Scribe 迁移为第一个实现；行为不回退（WebUI 麦克风路径不变）。
- **FR-1.2 云端双协议**（对齐 AgentRMC `AsrClient` 语义）：
  - `openai_transcriptions`：multipart `POST {base}/audio/transcriptions` → `{"text"}`（OpenAI Whisper、硅基流动、Groq 等）；
  - `openai_chat_input_audio`：`POST {base}/chat/completions`，音频作 `input_audio` 的 user 消息，取 `choices[0].message.content`（MiMo `mimo-v2.5-asr` 等，与对话/TTS 同平台共用 key）；
  - 两者 `base_url/api_key_env/model` 独立配置；key 走既有 `endpoint_identity`/`credential_env_for_endpoint` 口径，不落明文。
- **FR-1.3 本地引擎（可选 extra）**：`local_sensevoice`——`sherpa-onnx` + SenseVoice int8 模型（zh/en/ja/ko/yue），模型文件用户自放置或 `opensquilla voice model import <dir>`；CPU 4 线程、单次 ≤10s 语音典型 <1s；模型不随 wheel 分发（对齐 AgentRMC ModelImport 模式）。
- **FR-1.4 接入点**：
  - WebUI：`/api/audio/transcribe` 按配置路由到生效 Provider（现状仅 ElevenLabs 的 `_default_provider_factory` 换成注册表）；
  - 通道语音消息：telegram/discord/qq/wecom 等入站 `voice/audio` 附件，配置开启后自动转写为文本再进会话（默认关）；
  - CLI/TUI：`chat` 模式 push-to-talk（默认 `Alt+V` 可配；录音电平指示、Esc 取消）；
  - 工具面：内置 `transcribe_audio` 工具（受 tools policy 管辖），供 Agent 处理任意音频附件。
- **FR-1.5 诊断**：`opensquilla voice doctor`（枚举 Provider 可用性、模型完整性、麦克风设备列表）；`opensquilla voice transcribe <file>` 手动验证。
- **FR-1.6 失败语义**：转写失败绝不吞消息——通道路径退化为「保留附件 + 原文占位」，WebUI 路径返回结构化错误码。
- **FR-1.7 本地引擎增强（v0.3，✅ 已实现）**：① 静音门——WebUI 侧实时电平采样（AnalyserNode），全程无信号不发转写请求（省云积分）；② 幻觉守卫——本地 SenseVoice ≤2 字输出且无 voiced 帧判为静音噪声丢弃；③ 空结果走 fallback 链（空文本不算成功，继续尝试下一个 provider）；④ 每请求语言提示（按语言 LRU 缓存识别器，上限 3）；⑤ WebUI 麦克风输入设备选择器（`enumerateDevices` + `deviceId` 精确约束 + 静音设备自动降级重试，localStorage 持久化），解决虚拟麦/蓝牙耳机 Hands-Free 通道收不到声音的问题。

### F2 桌宠语音提醒

- **FR-2.1 常驻服务**：网关启动时按 `[pet] enabled=true` 创建 `PetReminderService`（asyncio 任务，与 gateway 同生命周期；pidlock 防双网关）。
- **FR-2.2 事件订阅（进程内）**：经 `gateway/event_bridge.py` + `session_streams` 的订阅接口接收（优先方案；见 §7.2 决策）。事件映射沿用外部桥已验证的表：

| 网关事件 | `/notify event` | 行为（桌宠端既有语义） |
|---|---|---|
| `session.event.done` / `chat.done` / `task.succeeded` | `stop` | 朗读+气泡 7s，**不抢前台**（0.3.9 起） |
| `session.event.approval_required` | `permission` | 朗读+气泡+拉 `raise` 窗口 |
| `session.event.plan_presentation` | `permission` | 同上 |
| `session.event.turn_committed` | `turn-start` | 静默归档信号，不出声不建卡，不吃去重窗 |

- **FR-2.3 必带字段**：`session_id`（工单去重/分组锚点）、`project`（workspace 目录名）、`event_id`（approval_id/turn_id/stream_seq 派生，防 180s 迟到去重压掉同会话第二道题）、`speech`（朗读短文案带「OpenSquilla」与状态，`message` 留详情）——与外部桥 README 完全同口径。
- **FR-2.4 去重与限流**：同一会话同一事件身份 15s 去重；`done` 与 `task.succeeded` 前后脚到达视为一条。
- **FR-2.5 免打扰**：pause 哨兵文件（默认 `%TEMP%\opensquilla-pet-bridge.pause` 兼容旧桥，可配）；`[pet] quiet_hours` 可选；cron 派生会话默认跳过（防自激），`include_cron=true` 打开。
- **FR-2.6 离线落盘**：桌宠不在线（`/state` 不可达）时通知写入 `%APPDATA%\LIG\AgentRMCNative\pending-events.jsonl`（同址同格式，超 1MB 丢旧留新），桌宠启动后自补投；`--selftest` 类探针不落盘。
- **FR-2.7 显式提醒入口**：
  - 内置工具 `pet_speak(message)` / `pet_notify(event,title,message,raise)`（受 tools policy 管辖）——支撑「现在就说 X」「半小时后喊我」（后者 = 既有 cron `agent_turn` 调工具，替代 `voice-reminder.ps1`）；
  - CLI `opensquilla pet say/notif/test`。
- **FR-2.8 状态可见**：`/state` 周期探测（pet_visible/tts_ready/listening…）进网关健康页与 `opensquilla doctor pet`；桌宠退出→服务静默降级为落盘模式，恢复后自动回到实时（无需重启）。
- **FR-2.9 安全**：仅 127.0.0.1；提醒文案禁止注入密钥/完整路径 token（模板渲染时走既有 `redaction.py` 脱敏）。

### F3 桌宠定向会话（OpenSquilla-only）

- **FR-3.1 收件轮询**：同服务内 `DispatchPoller`：默认 1.5s `GET /dispatch/pending?exe=OpenSquilla.exe`；桌宠不在线自动暂停（30s 退避重试）。`[pet] dispatch_enabled`（默认 true，依赖 `[pet] enabled`）。
- **FR-3.2 归属守卫**：注入前校验目标会话存在且可收消息（等价 `sessions.resolve`）；拒绝→ `ack {ok:false,error:"原因"}`，桌宠保留草稿并展示原因。**证明不了归属绝不瞎发**。
- **FR-3.3 幂等注入**：`sessions.send` 使用 durable admission；client message id = `agentrmc-{dispatch_id去横线}`，重取/重试不重复入站。同会话串行（一次在途），跨会话可并发。
- **FR-3.4 回执**：受理成功→ `POST /dispatch/ack {dispatch_id, ok:true}`（桌宠侧工单转进行中、回执落账）；未知 dispatch_id 回 404 由桌宠侧处理。
- **FR-3.5 边界**：只处理 `exe=OpenSquilla.exe` 的条目；取件缺字段（dispatch_id/session_id/text）拒绝并 ack。

### F3.5 语音生成与音频工具（v0.3 新增，原「语音与音频」卡拆分后的生成轴）

- **FR-3.5a 卡定位**：能力页独立卡「语音生成与音频工具」，供应商固定为 ElevenLabs（唯一具备完整生成全家桶的已接供应商，后续可扩 OpenAI-compatible TTS，见 §14）；密钥/base_url/`[audio].enabled` 的读写路径与现状一致（`onboarding.audio.configure` mutation 不变）。
- **FR-3.5b 默认 TTS**：音色 = Voice Preset 下拉（§5.0 / F5），试听按钮（调 `tts_audio` 工具后端，产出可播放音频）；「不使用预设」时沿用 `[audio.tts]` 裸配置（model/voice/语言码/稳定性等，既有字段全部保留兼容）。
- **FR-3.5c 音频工具开关组**：TTS 之外，把 `tools/builtin/media.py` 已实现的 ElevenLabs 能力收进卡内可见/可关：语音克隆（voice_clone）、语音转换（voice_conversion）、dubbing、音乐生成（music/instrumental）。工具注册与 policy 不变，UI 仅呈现「哪些可用 + 各自配额/状态」（读 `get_elevenlabs_capabilities`/订阅信息）。
- **FR-3.5d 识别轴引用**：`[audio.stt].provider = "elevenlabs"` 时识别复用本卡的密钥与 base_url，识别卡显示「复用『语音生成与音频工具』配置」，不出现第二个密钥框。

### F3.6 Voice Preset 音色预设体系（v0.3 新增）

- **FR-3.6a 数据模型**：`[audio.presets.<name>]`，字段 `voice_id`（必填）、`model_id`、`stability`、`similarity_boost`、`style`、`use_speaker_boost`、`speed`、`language_code`、`label`（展示名）、`source`（builtin/user/imported）。预设是**参数包**，不拥有密钥——播放时供应商凭证取 `[audio.providers.elevenlabs]`（§14 的 Qwen-TTS 预设扩展时同理取对应供应商）。
- **FR-3.6b 内置预设**：随版本内置少量精选公共音色（名称、voice_id、语种标签，对齐 ElevenLabs shared voices 精选）；用户可改名/删除/新增。内置预设不可物理删除，只可覆盖（写同名 user 预设）。
- **FR-3.6c 管理 UI**：能力页生成卡「音色」下拉 + 「管理音色」对话框（列表/新增/试听/删除）；试听调 `tts_audio`，失败给出结构化原因（配额/网络/voice 失效）。
- **FR-3.6d Agent 可见性**：`tts_audio` 工具新增可选 `preset` 参数（优先于裸 voice/model）；工具描述列可用预设名。
- **FR-3.6e 配置兼容**：无预设时一切行为与现状一致；`[audio.tts]` 裸字段仍是最终兜底。预设名允许中文/ASCII 混合（TOML 引号键）。
- **FR-3.6f 消费方登记**：① 生成卡默认 TTS；② Agent `tts_audio`；③ 桌宠播报（§14）。本期只做 ①②；③ 的 AgentBridge 下发为后续 PR（桌宠端已能接收 `voice` 扩展参数的规划见 §14）。

### F4 配置 / CLI / 诊断（汇总）

```toml
# opensquilla.toml（新增两段，默认注释关闭）
[pet]
enabled = false
bridge_url = "http://127.0.0.1:45915"
raise_exe = "OpenSquilla.exe"        # "" = 只出声不抢前台
include_cron = false
relay_preview_chars = 0              # >0 时 done 附带回复开头（默认不带）
pause_file = "%TEMP%/opensquilla-pet-bridge.pause"
dispatch_enabled = true
dispatch_poll_ms = 1500
msg_stop = "OpenSquilla 会话 {session} 已完成一轮"
msg_permission = "OpenSquilla 等你确认授权"
msg_plan = "OpenSquilla 等你选择方案"

[audio.stt]                          # 扩展现有 audio 配置树
provider = "elevenlabs"              # 或 openai_transcriptions / openai_chat / sensevoice
language = "zh"
fallback = []                        # 失败降级链（如 ["sensevoice"]）；空文本也视为未命中继续降级
#audio.stt.openai_transcriptions]  # base_url / api_key_env / model / timeout_seconds
[audio.stt.openai_chat]            # 同上（MiMo 等平台）
[audio.stt.sensevoice]             # model_dir / language=auto / use_itn / num_threads
# channel_transcribe = false       # 通道语音自动转写总开关（实现归入 [audio.stt] 子表）

[audio.presets.<name>]               # v0.3：Voice Preset 音色参数包（F3.6）
# label = "沉稳男声"
# voice_id = "21m00Tcm4TlvDq8ikWAM"
# model_id = "eleven_multilingual_v2"
# stability / similarity_boost / style / use_speaker_boost / speed / language_code
# source = "user"                    # builtin | user | imported
```

CLI 新增（沿用 `src/opensquilla/cli/*_cmd.py` 惯例）：`opensquilla pet status|test|say`、`opensquilla voice doctor|transcribe|model`、`opensquilla voice preset list|add|remove`（F3.6）。

---

## 6. 接口契约（与桌宠零改动对齐）

AgentBridge：`127.0.0.1:45915`（`native/src/core/AgentBridge.h`）

| 方法 | 请求 | 应答 | 本期用途 |
|---|---|---|---|
| `GET /state` | — | 200 + `pet_visible/listening/tts_ready/pet_enabled/chat_mode/docked/recording/transcribing` | 验活/健康 |
| `POST /speak` | `{"message"}`（空→400） | 204 | 即时念白 |
| `POST /notify` | `{"event","title","message","raise","session_id","project","event_id","speech"}` | 204 | 三类事件提醒 |
| `GET /dispatch/pending?exe=OpenSquilla.exe` | — | 200 + 派发数组（可为空） | 定向取件 |
| `POST /dispatch/ack` | `{"dispatch_id","ok","error?"}` | 200/404 | 注入回执 |

约束：body ≤64KB（超 413）、header ≤16KB、半截请求 10s 断开、全 204/200 无 Content-Type 要求；45915 单实例——连不上 ≠ 桌宠没跑，先查 `AgentRMCNative` 进程再下结论（诊断文案要体现）。

## 7. 架构方案

### 7.1 模块布局（新增，尽量薄）

```
src/opensquilla/
  audio/                    # 从 provider/audio.py 抽出 ASR 侧抽象（Provider 注册表、能力探测）
  pet/
    __init__.py
    config.py               # [pet] pydantic 模型（并入 GatewayConfig，走 config_version 迁移）
    bridge_client.py        # AgentBridge HTTP 客户端（httpx，超时/重试/状态缓存）
    events.py               # 事件→/notify 映射、15s 去重、event_id 派生
    spool.py                # pending-events.jsonl 离线补投（含 1MB 截断）
    dispatch.py             # 收件轮询 + 守卫 + 幂等注入 + ack
    service.py              # PetReminderService：生命周期编排（start/stop/健康上报）
  gateway/
    boot.py                 # lifespan 挂载 PetReminderService（门控 enabled）
    audio_transcription.py  # provider_factory → 注册表（行为兼容）
  cli/pet_cmd.py, voice_cmd.py
  tools/builtin/voice.py    # transcribe_audio / pet_speak / pet_notify（visibility/policy 注册）
tests/…                     # 契约测试 + 假桥服务器（aiohttp/httpx MockTransport）
docs/features/desktop-pet.md, docs/features/voice-asr.md
```

### 7.2 关键决策点（评审拍板）

| 决策 | 选项 A（推荐） | 选项 B | 理由 |
|---|---|---|---|
| 事件获取 | 进程内订阅 `session_streams`/EventBridge | 服务回环 WS 连自家网关 | A 消灭半开/探针/重连整类问题（P3）；B 解耦但复刻外部桥缺陷 |
| 注入路径 | 直调 session manager 内部 API（= `rpc_sessions` 的 resolve/send 同一实现） | 走 gateway RPC 回环 | 保证 durable admission 语义一致即可，不重复实现校验 |
| 服务形态 | gateway 内嵌 asyncio 任务 | 独立子进程 `opensquilla pet bridge` | 内嵌复用监管；子进程留给非网关用户（可后续再加） |
| 默认状态 | 全 opt-in（`enabled=false`） | 检测到桌宠自动开 | 尊重上游「默认离线、无侧效应」红线 |
| 本地 ASR 依赖 | extra `voice = ["sherpa-onnx>=…", "sounddevice>=…"]`，主包不引入 | 硬依赖 | wheel 体积与平台兼容（P4 风险 R3） |

### 7.3 与外部桥的关系

内置服务与 `opensquilla-pet-bridge.mjs` **互斥共存**：检测到 45915 侧近期同一事件重复 ack 不现实，改为文档约定「开启 `[pet]` 后卸载 VBS/计划任务」；Agentrmc 仓库侧由我们提 follow-up，把外部桥标记为 legacy fallback（§8.2 对等清单全绿后才动）。

### 7.4 插件化边界约定（为上游插件市场预留）

决策 (a) 的附带约束：`pet/` 与 `audio/`（STT 注册表）按「未来可整体降格为内置插件」的标准自律，插件机制落地时只换装载方，不重写功能。

| 约束 | 具体做法 |
|---|---|
| 单点注册 | 对网关的全部依赖收敛为 boot 里一次 `optional_owners["desktop_pet"] = OptionalServiceOwner(...)` 挂载；未来插件市场 = 把「built-in boot 调用」换成「registry loader 调用」 |
| 窄接缝 | `pet` 包内部只经三个注入 Protocol 与网关交互：①事件订阅（session_streams/EventBridge）、②会话读写（resolve/send 入口函数）、③脱敏（redaction）；不 import 其他网关内部私有模块，接缝用 `typing.Protocol` 声明在 `pet/ports.py` |
| 配置自包含 | 全部键收在 `[pet]` 一个 pydantic 段内（含版本迁移钩子），不往其他 config 段撒字段；`[audio.stt]` 同理（provider 注册表本身就是插件位的雏形，对齐 tokenjuice `rules/` 的数据驱动模式） |
| 无副作用导入 | `import opensquilla.pet` 不注册、不起任务、不探测端口；默认 disabled |
| 状态自治 | spool/pause/心跳等文件路径全部 `[pet]` 可配且有默认，删除即完全卸载（不污染 state_dir 之外的位置——兼容旧桥默认路径属例外，文档注明） |

## 8. 兼容与迁移

### 8.1 数据与目录

- 沿用旧桥的 pause 文件名与 spool 路径/格式，开内置即可无缝接管桌宠补投；
- `~/.agents/skills/agentrmc-voice-reminder` 技能保留为文档性说明（PR 不触碰用户目录）。

### 8.2 外部桥功能对等清单（迁移门槛，逐项勾）

订阅补挂（sessions.changed 发现新会话）✅→进程内天然全覆盖；15s 去重、身份去重、翻页取 workspace、project 上报、turn-start 归档、spool、假活自愈（内置后不再需要）、`--relay`、`--include-cron`、`--no-dispatch`、`--selftest`。

## 9. 测试与验收

- **UT**：事件映射表、去重窗、event_id 派生、spool 截断、ack 语义、四 STT Provider（httpx MockTransport，全离线）、幂等 cmid；v0.3 追加：静音门（全零帧→不调 provider、toast）、voiced 检测（合成信号/嗡鸣/静音判例）、幻觉守卫（≤2 字 + 无 voiced → 空文本）、空文本走 fallback 链、按语言识别器缓存上限、预设 CRUD 与兜底兼容（无预设 = 裸 `[audio.tts]`）。
- **契约/集成**：进程内起假 AgentBridge（随机端口）验证 `/notify /speak /state /dispatch/*` 全链路；`sessions.send` 用既有 test harness 的 durable admission 断言。
- **默认门禁**（CONTRIBUTING）：`uv run ruff check src tests` + `uv run pytest -q` 全绿；无网络/凭据/浏览器依赖；WebUI 若动麦克风组件需 `npm --prefix opensquilla-webui run build` 验证但不提交 dist。
- **手动矩阵**（Windows，桌宠实机）：① done→念且不抢前台；② approval_required→念+拉窗（外部桥从未实触发过此映射——本 PR 首次补验，风险 R5）；③ 桌宠未启动→spool，启动后补投不出声；④ 同一轮 done+task.succeeded 只念一次；⑤ 桌宠发定向消息→目标会话收到一条且仅一条；⑥ pause 文件在场全程静音；⑦ 网关重启服务随之恢复。
- **UI E2E（v0.3）**：假麦克风浏览器全链路——识别/生成两卡独立保存且互不踩（config 快照断言）；引擎切换即时生效（转写响应 provider 字段断言）；保存后脏横幅收敛（四轮往返）；设备选择器持久化；试听出帧；预设 CRUD+试听。
- **性能门**：事件→/notify P50 ≤1.5s、P99 ≤3s；dispatch 取件→ack ≤4s（桌宠在线）。

## 10. PR 切分与里程碑

| PR | 内容 | 依赖 | 预估 | 状态 |
|---|---|---|---|---|
| PR-1 | `audio/` STT 抽象 + 云双协议 Provider + 路由注册表 + UT | — | 2–3 天 | ✅ 已完成（feat/audio-stt-providers） |
| PR-1b | 本地 `sensevoice` Provider（sherpa-onnx、语音级幻觉守卫、fallback 空文本、语言 LRU）+ 静音门 + WebUI 设备选择器 + 配置/示例/测试 | PR-1 | 3 天 | ✅ 已完成（含 24 个后端测试、E2E） |
| PR-1c | 能力页两轴拆分：「语音识别」卡（引擎下拉+本地参数）与「语音生成与音频工具」卡（ElevenLabs 全家桶）分离；识别卡 elevenlabs 只读引用（§5.0/F3.5） | PR-1b | 1–2 天 | 待做 |
| PR-1d | Voice Preset 预设体系（配置模型+管理 UI+试听+tts_audio preset 参数，F3.6；桌宠播报消费的接口留好） | PR-1c | 2–3 天 | 待做 |
| PR-2 | `pet/` 提醒服务（events/spool/bridge_client/工具面/CLI）+ 假桥契约测试 | — | 3–4 天 | 待做 |
| PR-3 | 定向会话 `dispatch.py` + 幂等注入 + ack | PR-2 | 2–3 天 | 待做 |
| PR-4 | TUI push-to-talk + docs/features + 配置样例注释收尾 | PR-1b | 2–3 天 | 待做 |
| PR-5（§14） | 桌宠播报音色下发（AgentBridge voice 参数）+ 多 TTS 供应商预留落地 | PR-1d, PR-2 | 另议 | 规划 |

PR-1 与 PR-2 可并行；PR-1c/1d 与 PR-2/3 可并行。每个 PR 自带 docs 片段与 changelog；描述用 `Refs` 关联 Agentrmc 侧 issue。

## 11. 风险与对策

| ID | 风险 | 对策 |
|---|---|---|
| R1 | 网关内嵌服务与外部桥双跑→重复提醒 | 文档互斥约定 + 启动时检测到 45915 有旧桥事件特征（plugin_version 字段缺失但同秒重复）告警（尽力而为，不硬阻断） |
| R2 | 进程内直调 session manager 与 RPC 路径行为漂移 | 复用 `rpc_sessions` 同一入口函数而非旁路重写；契约测试锁定 |
| R3 | sherpa-onnx 在目标 Python/平台无 wheel、模型 226MB 体积 | 严格 extra 隔离 + 模型用户侧导入（ModelImport 模式）；CI 不装 extra |
| R4 | `approval_required/plan_presentation → permission` 映射从未实触发（外部桥遗留盲区） | PR-2 手动矩阵必测项；契约测试先锁 payload 形状 |
| R5 | 45915 端口被占→桌宠静默不监听 | doctor 区分「端口占用」与「桌宠未运行」（对齐技能边界规则） |
| R6 | 提醒文案泄露敏感路径/密钥 | 模板过 `redaction`；工具面禁止透传任意会话原文 >120 字 |
| R7 | 上游对「Windows 专属集成」的接受度 | 抽象为通用 `pet`/bridge 契约（协议本身跨平台可跑假服务器），平台差异只在默认值；PR-2 描述先挂 RFC 讨论 |
| R8 | 浏览器 webm 解码依赖外部 ffmpeg（本地 ASR 的隐性前提） | provider 抛结构化错误并给安装指引（`winget install Gyan.FFmpeg`）；UI 模型目录行提示依赖；voice doctor 检查 ffmpeg 与模型文件 |
| R9 | AgentRMC 粤语微调 bundle（WSYue）误当普通话用 | 文档/doctor 明示「model_dir 内容决定语种表现」；内置预设与示例目录分开；后续可在 catalog 里锁 SHA256（§12.2） |
| R10 | 识别/生成两卡对同一 ElevenLabs 密钥的双写冲突 | 单一事实源：密钥只存 `[audio.providers.elevenlabs]`，识别卡对 elevenlabs 只读引用（UI 无第二输入框） |

## 12. 待确认问题（开 PR 前）

1. **落位方式**（✅ **决策 2026-11-12：选 (a)**）：核实 OpenSquilla 现状**没有**运行时可开启的通用插件系统——`src/opensquilla/plugins/` 只是源码目录（唯一成员 tokenjuice 硬编码常开，`gateway/config.py` L1535；control-UI 的 "plugin toggle" 切的是 skills，`SkillsConfig.disabled` L408）。三个候选中：
   - **(a) core 网关常驻服务——已选定**。成本已核实很低：`boot.py` 的 `optional_owners: dict[str, OptionalServiceOwner]`（generation 栅栏 + config_revision + `disabled/degraded/ready` 描述符）就是现成的「可选集成」挂载机制，memory/channels 同款；pet 注册为 `desktop_pet` owner + `[pet]` 配置开关即可。**演进预留**：上游已有内置插件的先例（`plugins/tokenjuice`、TUI Plugin Slots `RouterHudPlugin`，`docs/features/tui-frontend.md` L29）——将来插件市场/注册表上线时，本功能以**内置插件**身份平移即可，前提是现在就把模块边界收窄（见 §7.4）。
   - (b) skills 层（现状形态）：可 enable/disable 但无法 in-process 常驻订阅事件，定向派发做不了——仅作外部桥过渡，被本方案替代。
   - (c) 第三方 service 注册机制：`OptionalServiceOwner` 已覆盖内置可选服务的生命周期，缺的只是外部代码装载+信任边界层，与 skills 扩展面双轨争议、上游无立项证据——**不认领**，插件市场若上线自然承接。
2. `local_sensevoice` 的模型分发政策：MVP 走「用户自放目录 + 完整性校验」；长期对齐上游 **Runtime Pack catalog** 先例（`runtime_packs/catalog.py`：应用自有清单、只存资产名+SHA256、下载源固定、大小护栏）——RFC 里直接问维护者是否接受 sensevoice 进 catalog。
3. 通道语音自动转写的 tools/policy 归属（provider 花费走 metered_http 记账确认）。
4. TUI push-to-talk 是否与既有 `tui-real-terminal-harness` 兼容（录音线程 vs 终端输入权）。

## 13. 参考（代码锚点）

- Agentrmc：`native/src/core/{AgentBridge,SessionDispatch,AsrClient,AsrEngine,VoiceController}.{h,cpp}`、`native/models/sensevoice/`、`plugins/opensquilla-bridge/opensquilla-pet-bridge.mjs`（事件映射/守卫/ack 幂等 cmid 的既成事实）
- Opensquilla：`src/opensquilla/gateway/{audio_transcription,event_bridge,boot,rpc_sessions,service_owner}.py`（`service_owner.OptionalServiceOwner` + `boot.optional_owners` 为 pet 服务的挂载先例，memory/channels 同款）、`src/opensquilla/audio/{stt_types,stt_registry,stt_elevenlabs,stt_openai,stt_sensevoice}.py`（PR-1/1b 已落地的 STT 注册表与本地 Provider）、`src/opensquilla/provider/audio.py`、`src/opensquilla/tools/builtin/media.py`（ElevenLabs 生成全家桶：tts/克隆/转换/dubbing/音乐，PR-1c 生成卡的现成后端）、`src/opensquilla/onboarding/{audio_specs,section_status,status}.py`（能力状态聚合，识别卡/生成卡各自取数）、`opensquilla-webui/src/components/setup/SetupCapabilitiesPanel.vue`（两轴拆分的改造点）、`src/composables/setup/useSettingsPromotedForm.ts`（`[audio.stt]` 已接 config.patch）、`src/opensquilla/application/capability_setup.py`、`src/opensquilla/runtime_packs/catalog.py`（模型分发可对齐的应用自有 catalog + SHA256 先例）、`docs/features/tui-frontend.md` §Plugin Slots（内置插件先例 RouterHudPlugin）、`opensquilla.toml.example`、`CONTRIBUTING.md`、技能 `agentrmc-voice-reminder`

## 14. 桌宠音色与多 TTS 供应商路线（v0.3 规划，本期不实现）

1. **播报音色 = Voice Preset**：`[pet] voice_preset = "<name>"`，PetReminderService 在 `/speak` `/notify` 的 payload 里附加 `{voice_id, model_id, 参数}`（AgentBridge 协议扩展点；桌宠端当前 `/speak` 只吃 `message`——扩展需与 AgentRMC 一起排期，PR-5）。
2. **协议扩展草案**（与 §6 约束一致，向后兼容：旧桌宠忽略未知字段）：
   ```json
   POST /speak {"message": "...", "voice": {"provider": "elevenlabs", "preset": "沉稳男声", "voice_id": "...", "model_id": "..."}}
   POST /notify {..., "speech": "...", "voice": { 同上 }}
   ```
3. **多 TTS 供应商预留**：`[audio.tts].provider = "elevenlabs"`（默认；未来 `openai_tts | qwen_tts | mimo_tts`）。预设的 `source` 字段（F3.6a）即为此准备——预设可声明 provider 归属，播放/下发按 provider 取 `[audio.providers.<id>]` 凭证；OpenSquilla 侧 `tts_audio` 工具与桌宠播报共用同一 provider 解析。
4. **桌宠端本地播报（远期备选）**：若桌宠要离线出声，可把 STT 侧的 `audio.stt.sensevoice` 模式镜像成 `audio.tts.local`（sherpa-onnx VITS/piper 类），由桌宠经同一 `[audio]` 配置树读取——本期不立项，仅保证配置树不留死角。
