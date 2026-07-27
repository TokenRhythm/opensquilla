<!-- 本文件译自 README.md @ 8794ffbe。英文 README 为权威来源。 -->
<!-- 检查是否过期： git log 8794ffbe..HEAD -- README.md -->

# OpenSquilla — 高效省 Token 的 AI Agent

<p align="center">
  <img src="assets/opensquilla-long-logo.png" alt="OpenSquilla logo" width="500">
</p>

<p align="center">
  <b>同样的预算，让 Agent 做更多事、做更好的事。</b><br>
  微内核 AI Agent——智能路由、持久记忆、安全沙箱、开箱即用的搜索与本地嵌入。
</p>

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/opensquilla/opensquilla/ci.yml?style=for-the-badge" alt="CI"></a>
  <a href="https://opensquilla.ai/"><img src="https://img.shields.io/badge/website-opensquilla.ai-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://github.com/opensquilla/opensquilla/releases"><img src="https://img.shields.io/github/v/release/opensquilla/opensquilla?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge" alt="Apache 2.0 License"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>中文</b> · <a href="README.ja.md">日本語</a> · <a href="README.fr.md">Français</a> · <a href="README.de.md">Deutsch</a> · <a href="README.es.md">Español</a>
</p>

> 本文档译自英文 [`README.md`](README.md)，如有出入请以英文版为准。

---

## 最新动态

- 📢 **2026-07-03** —— 我们的技术报告 **[Agentic Routing: The Harness-Native Data Flywheel](docs/releases/agentic_routing_v0.pdf)**（预览版）已发布，随 OpenSquilla **0.5.0 Preview 1** 一同放出。报告详细介绍了 harness 原生路由如何把日常 Agent 流量转化为自我改进的数据飞轮。

---

## 概览

OpenSquilla 是一个高效利用 Token 的微内核 AI Agent。本地模型路由会把每一轮都发给能处理它的最便宜模型；持久记忆、分层沙箱、内置网络搜索和设备端嵌入共同构成了这个
统一共享的轮次循环。

每个入口——Web UI、CLI 和聊天渠道——都跑在同一个循环里，因此工具调度、重试和决策日志的行为处处一致。可插拔的提供商层对接 TokenRhythm、OpenRouter、OpenAI、Anthropic、
Ollama、DeepSeek、Gemini、Qwen/DashScope 等 20 多个 LLM 提供商，无需改动你的代码或
配置结构。

OpenSquilla 0.5.0 是当前正式发布版本。

如需面向任务的产品文档，请从
[OpenSquilla 产品指南](README.product.md)或[文档索引](docs/README.md)开始。

---

## 安装

OpenSquilla 可运行于 Windows、macOS 和 Linux。请选择与你的使用场景匹配的安装方式。

桌面安装包和终端快速安装会直接给你一个预构建的**发布版**，无需 Git。另外两种——从源码安装和从源码开发——则需要克隆 Git 仓库后再构建(`git clone` + Git LFS)，其中也包括 Vue 控制台。发布 wheel 和桌面安装包已内置控制台，用户无需安装 Node.js 或 npm。

发布版安装命令使用 GitHub 上已发布的 release 资源。Python wheel 安装使用带版本号的 wheel
文件名，因为安装器会校验嵌入在 wheel 文件名中的版本号。

对于 0.5.0 的桌面使用，建议从 GitHub Release 下载打包桌面安装包:macOS 上为
`OpenSquilla-0.5.0-mac-arm64.dmg`，Windows 上为 `OpenSquilla-0.5.0-win-x64.exe`。

| 安装方式 | 适合人群 | 何时使用 |
| --- | --- | --- |
| [桌面安装包](#desktop-installers)**（推荐桌面用户）** | macOS 和 Windows 用户 | 打包桌面应用 |
| [终端快速安装](#quick-terminal-install)**（推荐）** | 任意系统的最终用户 | 在终端中安装发布版 wheel |
| [从源码安装](#install-from-source) | 跟踪 `main` 分支的用户 | 从检出运行，而非修改它 |
| [从源码开发](#develop-from-source) | 贡献者 | 编辑、测试或调试源码 |

### 前置条件

| 要求 | 终端快速安装 | 从源码安装 | 从源码开发 |
| --- | :---: | :---: | :---: |
| Python 3.12+ | 通过 `uv` | 通过 `uv` 或系统 | 通过 `uv` |
| Git + Git LFS | — | 必需 | 必需 |
| Node.js 22.12+ + npm | — | 构建 Web UI 时必需 | Web UI 和 wheel 构建必需 |
| `uv` | 缺失则自动安装 | 推荐 | 必需 |

默认的 `recommended` 安装档会安装 **SquillaRouter**——OpenSquilla 的设备端模型路由
——及其模型资源；`OPENSQUILLA_INSTALL_PROFILE=core` 则会省略这些依赖。而 `--router disabled` 这个独立的 onboarding 标志则会保留已装好的依赖，只在运行时关闭路由。

在 Windows 上，SquillaRouter 内置的 ONNX 运行时还需要 Visual C++ 运行库。源码安装用的
PowerShell 安装器会通过 `winget` 自动装好它；而**终端快速安装**(`uv tool install`)这条路径不会——如果启动时记录了 `DLL load failed`
错误，请手动安装(见[故障排查](#troubleshooting))。在装好之前，OpenSquilla 会以直连单一模型的路由方式继续运行。

在 macOS 终端安装时，SquillaRouter 的 LightGBM 运行时可能还需要系统的 OpenMP 库。
桌面应用会内置它所需的运行库，但**终端快速安装**不会安装 Homebrew/系统库。
如果启动时记录了 `Library not loaded: @rpath/libomp.dylib`，请运行
`brew install libomp`，然后重启网关。在装好之前，OpenSquilla 会以直连单一模型的路由方式
继续运行。

安装链接:[Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[Node.js](https://nodejs.org/en/download) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/)。

<a id="desktop-installers"></a>

### 桌面安装包

0.5.0 桌面安装包将 Vue 控制台和网关运行时打包在一个 Electron 外壳中。

- macOS Apple Silicon:<https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg>
- Windows x64:<https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe>

中国大陆下载可直接使用 OSS 的固定安装包链接：

- macOS Apple Silicon：<https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg>
- Windows x64：<https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe>

这两个链接仅会在更新且符合条件的 Release 通过镜像校验后向前更新；需要固定版本时，请使用上方的 GitHub Release 版本化链接。

升级前请退出任何正在运行的 OpenSquilla 桌面应用。桌面安装版会继续使用平台应用数据
目录中的现有 Desktop profile。终端安装版的 `~/.opensquilla` 是另一套独立数据；如需
使用，请在设置中主动转移。

在 Windows 上从 RC3 升级到 RC4 或更高版本时，请直接运行新安装包覆盖现有安装。
请勿先卸载 RC3：RC3 的卸载程序可能会删除桌面端用户数据。升级前请备份
`%APPDATA%\OpenSquilla`。RC4 及更高版本的安装包在常规卸载时会保留用户配置数据。

<a id="quick-terminal-install"></a>

### 终端快速安装

在 Windows、macOS 和 Linux 上的推荐路径。`uv` 会把 OpenSquilla 装进独立的隔离环境，并自带一个专用的 Python，不依赖系统 Python。此路径仅安装已发布的版本；如需 `main`、开发分支
或本地检出，请使用[从源码安装](#install-from-source)。

**1. 安装 `uv`**——如果 `uv --version` 已经可用则跳过。

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. 安装 OpenSquilla**——所有平台命令相同。

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

这会从 release URL 安装 OpenSquilla wheel，再由 `uv` 下载所选 extra 所声明的依赖。
默认的 `recommended` extra 包含 SquillaRouter 运行时依赖，如 ONNX Runtime、LightGBM、
NumPy 和 tokenizers，因此首次安装需要联网，除非这些 wheel 已经缓存好了。`uv` 不会安装
系统原生运行库，如 macOS 的 `libomp` 或 Windows 的 Visual C++ Redistributable；若路由
运行时报告原生库加载错误，请参见[故障排查](#troubleshooting)。

**3. 配置并运行。**

```sh
opensquilla onboard
opensquilla gateway run
```

> [!NOTE]
> 如果在全新 `uv` 安装后立即找不到 `opensquilla`，请打开一个新终端，或重新执行第 1 步中的
> PATH 设置命令。

如需完全锁定版本的安装，请使用带版本号的 wheel URL:
`https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`。

<a id="install-from-source"></a>

### 从源码安装

用这条路径可以直接从克隆下来的代码运行 OpenSquilla，而不去改动它。这份克隆只是给安装器当包源用；装好之后
请使用 `opensquilla` 命令——不要运行 `uv run`。如果你打算修改代码，请改用
[从源码开发](#develop-from-source)。

1. **带 LFS 资源克隆**

   ```sh
   git lfs install
   git clone https://github.com/opensquilla/opensquilla.git
   cd opensquilla
   git lfs pull --include="src/opensquilla/squilla_router/models/**"
   ```

2. **运行安装器**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   该脚本会先在 `opensquilla-webui` 中执行 `npm ci` 和 `npm run build`，
   再用 `uv tool install` 把 `.[recommended]`(SquillaRouter + 记忆 +
   本地模型)装进一个专用的用户环境。每次源码重装都会用 `npm ci` 重建
   `node_modules` 并重新构建控制台；首次运行通常下载量最大，后续可利用 npm
   缓存减少网络流量，但仍会产生构建时间和磁盘写入；如果 `uv` 不可用，就退回到
   `python -m pip install --user`。如果安装后 `opensquilla` 不在 `PATH` 上，请打开
   一个新终端。

   直接执行 `pip install .`、`uv tool install .` 或通过 VCS URL 安装，属于底层的
   源码构建方式，不能替代上述安装器。本地 checkout 必须先构建 Web UI；VCS URL
   checkout 没有生成产物，会被门禁明确拒绝。请改用此源码安装器或官方 release wheel。

3. **（可选）安装进阶 extra。** 大多数渠道——Feishu（飞书）、Telegram、DingTalk（钉钉）、
   QQ、WeCom（企业微信）、Slack 和 Discord——在基础安装下即可使用。可选的 extra 有:

   - `matrix` — Matrix 渠道(引入 `matrix-nio`)
   - `matrix-e2e` — 带端到端加密的 Matrix 渠道（需要 libolm）
   - `document-extras` — 通过 WeasyPrint 生成 PDF

   ```sh
   OPENSQUILLA_INSTALL_EXTRAS=matrix bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras matrix   # Windows
   ```

4. **配置并运行**——见[配置](#configuration)。

<details>
<summary>从源码安装——终端前置条件与安装器选项</summary>

**从终端安装前置条件(Git、Git LFS、Node.js 22.12+ 与 npm、uv)**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS(Homebrew):

```sh
brew install git git-lfs node uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

在 Fedora 上使用 `sudo dnf install -y git git-lfs`；在 Arch 上使用
`sudo pacman -S --needed git git-lfs`；另请从发行版或 nodejs.org 安装 Node.js
22.12+ 与 npm，然后用上面的 `curl` 命令安装 `uv`。这些安装器
对 PATH 的修改会在新的终端会话中生效。

**安装器环境变量与 PATH 检查**

```sh
OPENSQUILLA_INSTALL_PROFILE=core   bash scripts/install_source.sh   # 最小运行时，无 SquillaRouter
OPENSQUILLA_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # 仅打印计划
```

用 `command -v opensquilla`(macOS/Linux)或 `where.exe opensquilla`(Windows)确认
你的 shell 实际运行的是哪个 `opensquilla`。如果它不在 `PATH` 上，请运行
`uv tool update-shell`。从本地检出重新安装后，请重启网关以加载更新后的包。

</details>

<a id="develop-from-source"></a>

### 从源码开发

如果你要动 OpenSquilla 的源代码——改代码、跑测试，或在这份克隆里调试行为——就用这条路径。
它不是常规的安装路径。与[从源码安装](#install-from-source)不同，此路径需要 `uv`:
`uv sync` 会创建一个仓库本地的 `.venv`，而 `uv run` 会针对此检出中的文件执行命令。

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
```

修改 Web UI 源码后需要重新执行 `npm run build`。标准 wheel 构建会拒绝缺失或
过期的控制台产物；后端开发所用的 editable `uv sync` 仍然可用。

`recommended` extra 在开发时也包含 SquillaRouter;`dev` extra 会安装测试、lint 和
类型检查工具。把额外的 extra 安装到你运行的同一个环境中:

```sh
uv sync --extra recommended --extra dev --extra matrix
uv run opensquilla channels status matrix --json
```

在这种模式下，请为[配置](#configuration)中的每一条 `opensquilla` 命令加上 `uv run`
前缀。不要用用户本地的 `opensquilla` 命令去调试开发版代码——那个命令跑在另一个 Python 环境里。

### 卸载

用 `opensquilla uninstall` 卸载 OpenSquilla。它默认保留你的数据，只移除程序本身:

```sh
opensquilla uninstall --dry-run   # 预览将被移除和保留的内容
opensquilla uninstall             # 移除程序，保留你的数据
```

如需同时删除数据，请显式选择:

```sh
opensquilla uninstall --purge-state    # 会话、日志、缓存、调度器、记忆
opensquilla uninstall --purge-config   # config.toml 和密钥（.env）
opensquilla uninstall --purge-all      # 全部（会要求你输入确认）
```

运行中的网关会先被清空并停止，删除只在 OpenSquilla 主目录内进行；若是 Docker 或桌面安装，则会改为提供引导式的卸载步骤。桌面或操作系统应用的移除仍按各平台方式处理；CLI 引导不会删除桌面 app bundle。完整参考见 [`docs/cli.md`](docs/cli.md#uninstall)。

---

## 安装隐私

OpenSquilla 使用匿名安装遥测来估算安装数量、版本采纳情况和运行时兼容性。数据只在网关
首次启动时上报，并且每个 OpenSquilla 版本只上报一次。它还会在本地按 UTC 日期汇总已完成
的顶层对话次数和 token 用量，并在启动时及此后每小时尝试向遥测服务上报待发送的 UTC
当日累计快照。OpenSquilla 也可能执行被动更新检查，包括桌面启动时以及应用持续运行期间
最多每日一次的自动更新检查。上传设了很短的超时，绝不会阻塞启动。

发送的内容:

- schema 版本
- 本地生成的稳定 `install_id` 摘要
- OpenSquilla 版本
- 事件类型(`install`、`version_seen` 或 `daily_usage`)
- 安装方式(`pip`、`source`、`docker`、`desktop` 或 `unknown`)
- 操作系统、系统版本、CPU 架构，以及 Python 主/次版本号
- 首次见到与发送的时间戳
- CI/测试环境标记(`ci_environment`)
- 每日用量事件中的 UTC 日期、完成对话次数及 input/output/cache/cache-write token 汇总

`install_id` 是一个本地单向 SHA-256 摘要，由可用的 MAC 地址派生；无 MAC 时使用本地 IP
地址，并以一个随机持久化值兜底。原始 MAC/IP 值不会被上传。

不发送的内容:用户名、主机名、路径、API key、提供商配置、聊天/会话/记忆/Agent 内容、
文件名或文件内容。源 IP 在传输层可能会被 HTTP 服务器看到，但它不在上传的数据内。

要在启动前关闭非用户主动触发的网络可观测性:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

或在配置中设置:

```toml
[privacy]
disable_network_observability = true
```

这个统一开关覆盖自动安装遥测、每日汇总用量遥测、被动更新检查，以及桌面启动时和应用持续运行期间的自动更新检查。只要统一或兼容退出开关仍启用，用户显式触发的更新可用性检查也不会绕过它。其他用户主动操作仍可能在明确意图后访问网络服务，例如打开发布页、下载发布资产，以及使用已配置的提供商、搜索或渠道。

旧环境变量仍兼容:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

进阶部署可以使用自己的安装遥测端点:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
```

---

<a id="configuration"></a>

## 配置

### 首次配置

`opensquilla onboard` 是交互式的首次配置向导。它会写入当前配置文件；当你传入 `--api-key-env` 时，提供商密钥会留在环境变量里。路由默认为 `recommended`
（在受支持的提供商上启用 SquillaRouter）；如需直连单一模型，使用 `--router disabled`。

```sh
opensquilla onboard                # 完整交互式向导
opensquilla onboard --if-needed    # 幂等:适用于脚本和重装
opensquilla onboard --minimal      # 仅配置提供商；跳过渠道与搜索
opensquilla onboard status         # 查看每个配置项，但不写入
```

在 SSH、CI 或任何没有 TTY 的环境中，请使用非交互形式——把密钥放在环境变量里，
并传入它的**变量名**，而不是它的值:

**Linux / macOS**

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

**Windows PowerShell**

```powershell
$env:OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

OpenRouter 仅作示例——可替换为任意受支持的提供商及其对应的 API key 变量。

之后无需重做整个向导即可重新配置某一个部分（以下示例假设相关 API key 已在环境中）:

```sh
opensquilla configure provider --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY
opensquilla configure router --router recommended
opensquilla configure search   --search-provider duckduckgo
opensquilla configure search   --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure channels
```

各部分:`provider`、`router`、`channels`、`search`、`image-generation`、
`memory-embedding`。Web UI 在 `/control/setup` 暴露相同的目录和状态模型:Provider 和
Router 是快速路径，而 Channels、Search、Image generation 和 Memory embedding 位于
能力中心（Capability Center），可以稍后配置。渠道留空会被当作主动跳过，而不是配置失败。

**配置加载顺序:**`OPENSQUILLA_GATEWAY_CONFIG_PATH` → `./opensquilla.toml` →
`~/.opensquilla/config.toml` → 内置默认值。对单个密钥来说，环境变量里的值始终优先于配置文件里的值。

### 从 OpenClaw 或 Hermes Agent 迁移

如果你已经在 `~/.openclaw` 或 `~/.hermes` 下有状态，请先执行一次 dry run 查看迁移报告，
然后再显式应用:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply

opensquilla migrate hermes --json
opensquilla migrate hermes --apply
```

使用 `opensquilla migrate --source openclaw,hermes --apply` 可同时导入两个默认主目录。
只有在看过 dry-run 报告之后，才加上 `--migrate-secrets`。自定义路径和冲突处理见
[`MIGRATION.md`](MIGRATION.md)。

### 运行

```sh
opensquilla gateway run                # 前台运行，127.0.0.1:18791
opensquilla gateway start --json       # 后台运行 + 健康检查等待
opensquilla chat                       # 交互式 REPL
opensquilla agent -m "你的提示词"       # 一次性执行，便于自动化
```

在 <http://127.0.0.1:18791/control/> 打开 Web UI。**Health（健康）** 视图会显示
OpenSquilla 是否就绪、哪些项尚未就绪，以及下一步的恢复建议。在 CLI 中，运行:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla doctor --config ./opensquilla.toml --json
```

`/health` 和 `/healthz` 是用于进程检查的轻量存活探针。`opensquilla doctor` 和 Web UI 的
Health 视图则是检查就绪状态的地方，涵盖提供商配置、记忆、日志、搜索、渠道、沙箱状态、
路由、图像生成以及恢复指引。按 `Ctrl+C` 可停止前台网关。

其他命令组包括 `sessions`、`skills`、`memory`、`migrate`、`cron`、`channels`、
`providers`、`models` 和 `cost`。运行 `opensquilla --help` 或
`opensquilla <组名> --help` 查看详情。

<details>
<summary>进阶配置——验证渠道、公网绑定、Docker</summary>

**连接并验证一个消息渠道**

保存渠道只是改了配置，并不代表它在运行时真的连得上。编辑渠道后请重启网关，然后验证实时渠道:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

只有当状态数据里 `enabled=true`、`configured=true` 且 `connected=true` 时，才算渠道已连上。Feishu（飞书）默认使用 websocket 模式，Telegram 使用轮询，Slack 可使用
Socket Mode——这些模式都不需要公网 URL。Feishu webhook 模式、Telegram webhook 模式、
Slack webhook 模式以及 WeCom（企业微信）则需要一个公网可达、提供商可访问的 URL。

**公网绑定**

要从另一台机器访问 Web UI，请把网关绑定到所有网络接口，并使用主机的公网 IP:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

公网访问还要求主机防火墙或云安全组允许该端口的入站 TCP。不要在
`[auth] mode = "none"` 的情况下暴露网关——在绑定到 `0.0.0.0` 之前请先配置 token 认证。

**Docker**

预构建的多架构镜像(`amd64`/`arm64`)会随每个发布标签发布到
`ghcr.io/opensquilla/opensquilla`——完整的容器部署指南见
[`docs/docker.md`](docs/docker.md)(家庭服务器与 NAS、带 token 认证的局域网访问、升级):

```sh
OPENSQUILLA_GATEWAY_IMAGE=ghcr.io/opensquilla/opensquilla:latest docker compose up -d
```

不设置 `OPENSQUILLA_GATEWAY_IMAGE` 时,compose 路径运行一个你自己构建的
`opensquilla:local` 镜像。请从一份已拉取 Git LFS 路由
资源的源码检出来构建它(克隆与 `git lfs pull` 见[从源码安装](#install-from-source)):

```sh
docker build -t opensquilla:local .
```

随后 `./start.sh`(Windows 上为 `start.ps1`)会运行 `docker compose up -d` 并跟踪网关
日志。Docker 省掉的是宿主机上的 Python 工具链，而不是本地镜像构建那一步。

</details>

提供商分级、沙箱调优、图像生成和并发设置位于 `opensquilla.toml.example`。

---

## 0.4.1 更新内容

OpenSquilla 0.4.1 是面向桌面端与 Control UI 方向的维护版本:

- **桌面可靠性** —— 打包后的网关检查现在覆盖 Coding 模式、`code-task` 和 SquillaRouter
  启动，桌面窗口/产物处理更加稳定。
- **六种语言客户端支持** —— Control UI 和桌面客户端在首屏和设置界面支持英语、简体中文、
  日语、法语、德语和西班牙语。
- **Coding 模式与路由打包** —— 如果路由资源缺失或仍是 Git LFS 指针，桌面构建会快速失败，
  防止生成功能受损的发布包。
- **遥测与 Windows 打磨** —— 安装遥测会跳过 CI 和测试环境，Windows 桌面资源使用
  OpenSquilla 徽标。
- **主线治理** —— 普通 pull request 和发布集成统一围绕 `main`，维护者分支则保留用于发布、
  热修复、预发布、集成和沙箱工作。

完整说明:[`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/releases/0.4.1.md`](docs/releases/0.4.1.md)。

## 0.2.1 更新内容

OpenSquilla 0.2.1 是一个专注于发布包启动和长时运行 Agent 可靠性的维护版本:

- **Windows 便携版启动** —— 便携版启动器能更好地检测并引导安装内置 ONNX 路由所需的
  Visual C++ 运行库。
- **长时运行的 Agent 轮次** —— 工具密集的 WebUI 会话现在能更利落地从各种状况中恢复:超大工具结果、格式错误的工具调用、产物交付环节，以及降级的最终响应。
- **更干净的 WebUI 输出** —— 生成的产物标记不会出现在普通聊天回放中，而已交付的文件仍然
  可见。
- **记忆召回评分** —— 本地以及 OpenAI 兼容的嵌入向量，会在语义搜索前先做归一化；当向量得分偏低时，强关键词匹配依然能派上用场。

完整说明:[`CHANGELOG.md`](CHANGELOG.md) ·
[发布说明](https://opensquilla.ai/news/)。

## 0.2.0 更新内容

此版本在迁移、CLI 聊天、渠道、调度和长时运行的工具工作等方面扩展了 OpenSquilla:

- **从现有 Agent 主目录迁移** —— `opensquilla migrate` 可以预览并执行从现有 OpenClaw/Hermes 主目录的导入，包括记忆、persona 文件、技能、MCP/渠道配置、冲突处理
  和迁移报告。
- **可用的聊天 CLI** —— `opensquilla chat` 拥有稳定的终端 UI、流式输出、排队输入、
  斜杠模式发现、工具/状态条，以及更具确定性的实时提示行为。
- **跨界面的 cron 自动化** —— cron 作业现在涵盖结构化排程、时区感知的精确/周期性/cron 运行、
  渠道或 webhook 投递、失败目标、手动运行，以及 WebUI/CLI/RPC 的一致性。
- **更好的 Feishu 和 Discord 渠道** —— 渠道适配器暴露更清晰的能力元数据、更安全的
  私信/群组处理、原生的文件与产物路径，以及改进的附件/线程行为，同时特权操作保持受限作用域。
- **更稳健的长时运行轮次** —— 失败的轮次不会进入提供商回放，格式错误的工具调用也会得到更稳妥的处理；需要审批的重试则会等操作者拍板。
- **更智能的上下文与工具预算** —— 提供商预算压缩、提示缓存保留、对工具结果做大小限制，以及能感知副作用的并发，让大型、工具密集的会话更可预测。
- **Web UI 与发布打磨** —— 在 0.2.0 中对按时间排序、表格布局、移动端控件、重复通知、配置表单、release URL 和安装路径都做了打磨收紧。

完整说明:[`CHANGELOG.md`](CHANGELOG.md) ·
[发布说明](https://opensquilla.ai/news/)。

---

## 核心功能

| 能力 | 它做什么 |
| --- | --- |
| **省 Token 的路由** | `SquillaRouter`——`recommended` extra 中一个本地的 LightGBM + ONNX 分类器——会按长度、语言、代码、关键词和语义嵌入给每一轮打分，再在四个分级（C0–C3；旧的 T0–T3 命名是其别名）里把它分派给能胜任的最便宜模型。分类在本机上完成；为了做这个判断，你的提示词不会离开本机。 |
| **自适应推理与提示** | OpenSquilla 仅对路由判定为复杂的轮次请求扩展推理，系统提示也随任务复杂度伸缩——简单轮次用轻量提示，复杂轮次用完整指令。 |
| **20+ 个 LLM 提供商** | 提供商注册表面向 20 多个 LLM 后端——TokenRhythm、OpenRouter、OpenAI、Anthropic、Ollama、DeepSeek、Gemini、DashScope/Qwen、Moonshot、Mistral、Groq、Zhipu、SiliconFlow、vLLM、LM Studio 等等，并支持主用 + 回退选择；首次 onboarding 会暴露已验证的子集。 |
| **按需技能与 MCP** | 15 个内置技能(coding、GitHub、cron、pptx/docx/xlsx/pdf、摘要、tmux、天气等)仅在任务需要时加载。OpenSquilla 是 MCP 客户端，也可以作为 MCP 服务端运行——`opensquilla mcp-server run` 需要 `mcp` extra(安装 `opensquilla[recommended,mcp]`)。技能可以从 CLI 编写、安装和发布。 |
| **持久化本地记忆** | 一份精选的 `MEMORY.md` 加上带日期的 Markdown 笔记，通过 SQLite 全文关键词搜索和 `sqlite-vec` 语义召回来检索。嵌入通过内置 ONNX 在设备端运行，也可切换到 OpenAI/Ollama。可选的指数衰减和需主动启用的“做梦(dream)”记忆整合也可用。 |
| **分层安全沙箱** | 基于权限矩阵的三档策略（Standard / Strict / Locked）。在 Linux 上 Bubblewrap 隔离代码执行；macOS 的 Seatbelt 后端目前只生成 profile（尚未真正执行），Windows 上则还没有沙箱后端。拒绝账本（denial ledger）会在反复拒绝后自动暂停自主运行，清除被拒的输出；技能元数据和工具结果也会做 XML 转义，以防提示注入。 |
| **内置工具** | 文件读/写/编辑、shell 与后台进程、git、网络搜索（DuckDuckGo、Bocha、Brave、Tavily 或 Exa），以及带 SSRF 防护的网页抓取、电子表格/PPTX/PDF 创作、图像生成，以及文本转语音。 |
| **统一网关** | 一个运行在 `127.0.0.1:18791` 上、带 WebSocket RPC 和内嵌控制台(`/control/`)的 Starlette ASGI 服务。Web UI、CLI，以及 Terminal、WebSocket、Slack、Telegram、Discord、Feishu、DingTalk、WeCom、Matrix、QQ 这些渠道，都共用同一个 `TurnRunner`。 |
| **持久会话、子 Agent 与调度** | 由 SQLite 支撑的会话、转录和回放存储，并带有按 Agent 隔离的工作区。Agent 可以派生深度受限的子 Agent；`SchedulerEngine` 内置了 cron 解析器，会通过 `opensquilla cron` 运行周期性作业。 |
| **操作者控制** | 人在环路(human-in-the-loop)审批可以暂停敏感的工具调用，等人来决定；按轮次和按会话的 Token 与成本汇总(`opensquilla cost`)及诊断信息均可从 CLI 和 Web UI 获取。 |

MetaSkill 文档:[`docs/features/meta-skills.md`](docs/features/meta-skills.md)、
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md)
和 [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md)。

---

## 基准测试结果

PinchBench 1.2.1 在 25 个任务上的平均结果:

| Agent | 基座模型 | 平均分 | 总输入 token | 总输出 token | 总成本 |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenSquilla | 模型路由（Opus4.7、GLM5.1、DS4 Flash） | 0.9251 | 1,721,328 | 61,475 | $0.688 |
| OpenClaw | Claude Opus 4.7 | 0.9255 | 3,066,243 | 50,890 | $6.233 |

分数是 25 个任务的均值；token 数和成本是整次运行的总计。

---

<a id="troubleshooting"></a>

## 故障排查

<details>
<summary>macOS：<code>Library not loaded: @rpath/libomp.dylib</code></summary>

如果启动时从 `lightgbm/lib/lib_lightgbm.dylib` 记录了
`Library not loaded: @rpath/libomp.dylib`,OpenSquilla 会以直连单一模型的路由方式继续
运行，但内置的 `SquillaRouter` 运行时会保持不活动，直到安装 macOS 的 OpenMP 运行库。

桌面应用会内置它所需的原生运行库。如果你是通过终端快速安装或从 shell 进行源码安装，
请用 Homebrew 安装 `libomp` 并重启网关:

```sh
brew install libomp
opensquilla gateway restart
```

</details>

<details>
<summary>Windows：<code>DLL load failed</code> / Visual C++ 运行库</summary>

如果启动时记录了 `DLL load failed while importing onnxruntime_pybind11_state`,
OpenSquilla 会以直连单一模型的路由方式继续运行，但内置的 `SquillaRouter` 运行时会保持
不活动，直到安装适用于 Visual Studio 2015–2022(x64)的 Visual C++ Redistributable。

从源码安装的 PowerShell 安装器会尝试通过 `winget` 安装该 redistributable。如果你使用的是终端快速安装，或 `winget` 不可用，请手动安装它并重启
PowerShell:<https://aka.ms/vs/17/release/vc_redist.x64.exe>。然后恢复推荐的路由:

```powershell
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY --router recommended
opensquilla gateway restart
```

</details>

---

## 致谢

OpenSquilla 的灵感来自
[OpenClaw](https://github.com/openclaw/openclaw)。内置的第三方内容在
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 中注明出处。

社区贡献者都记在 [`CONTRIBUTORS.md`](CONTRIBUTORS.md) 里，其中也包含针对 squash 合并或回放工作、按发布版本给出的署名说明。

---

## 贡献者

感谢所有为 OpenSquilla 做出贡献的人。

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=opensquilla/opensquilla&max=100&columns=10" alt="OpenSquilla contributors" />
  </a>
</p>

---

## 参与贡献

我们欢迎各种形式的贡献——bug 报告、功能想法、文档、新的提供商或渠道适配器、技能，以及
核心运行时方面的开发。请参阅 [`CONTRIBUTING.md`](CONTRIBUTING.md)，然后到
[GitHub](https://github.com/opensquilla/opensquilla) 上提 issue 或 pull request。

[行为准则](CODE_OF_CONDUCT.md) · [安全](SECURITY.md) ·
[支持](SUPPORT.md) · [许可证](LICENSE)（Apache-2.0）
