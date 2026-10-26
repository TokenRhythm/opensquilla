# 桌面 TokenRhythm 账号登录与模型来源

实现日期：2026-10-22。适用于 Electron macOS / Windows。本文记录实现边界、复验入口和未执行的真实 UAT；不包含部署或数据库迁移。

## 用户流程

- 侧栏底部统一为账号菜单，未登录显示“登录 / 设置”，登录后显示网站昵称与首字头像。折叠状态保留头像入口，支持键盘导航与 Esc。
- 桌面设置的首项是“账号与余额”，不依赖 Gateway 启动成功。支持浏览器授权、取消、重新打开浏览器、刷新、退出及网站账号/用量/充值链接。
- 钱包直接呈现后端十进制字符串；可用余额、充值余额、可用赠金分开显示。欠款和冻结金额折叠展示。错误使用空值或带更新时间的旧数据，不合成零余额。
- 模型服务分别呈现账号连接和手动 TokenRhythm Key。账号卡片不提供专用 Key 的查看、复制或编辑操作。未登录时隐藏登录专属操作；已有显式账号媒体绑定仍保留恢复入口。
- 登录保留已有配置，包括凭证失效的配置。只有没有保存模型连接时自动选择账号；首次向导保留手动配置和跳过。
- 相同 TokenRhythm 服务下切换凭证来源不改变模型或路由；跨服务商继续使用现有主用服务切换流程。
- 图片/视频默认沿用模型服务，并保留已有独立凭证；账号卡片的展开项可显式选择账号、手动或沿用。两种媒体的选择互不影响。
- 退出立即中止账号代理请求并停用账号选项，保留暂停的来源绑定，不自动切到手动 Key。更换网站账号必须退出后重新登录，新账号不能接管旧绑定。

## 模块与凭证边界

- `desktop/electron/src/tokenrhythm-account.ts` 独立负责 PKCE S256、动态回环回调、随机 state、稳定 installation_id、串行刷新、账号/钱包/Key 状态、退出及待撤销会话。
- `tokenrhythm-model-proxy.ts` 仅接受受管 Gateway 的临时授权；固定 HTTPS 上游及方法/路径白名单，拒绝浏览器跨源调用、任意目标、查询参数和上游重定向。上游认证头由主进程设置。
- 账号令牌和模型 Key 使用本地 AES-256-GCM 加密，存于 `account.local.enc`，随机密钥存于同目录的 `account.local.key`；目录权限为 `0700`，文件权限为 `0600`（Windows 沿用用户数据目录的访问控制），账号记录临时文件原子替换。它不提供钥匙串对同用户其他进程的隔离，保护依赖本机文件权限。macOS 默认启用 Chromium 模拟钥匙串，启动与 OAuth 不访问真实钥匙串，不弹系统凭据授权框。旧 `account.enc` 保留且不尝试解密，升级后需要重新登录；本地密钥缺失或记录损坏时保留数据并拒绝覆盖。
- 每个 profile、每个官方环境隔离账号凭据。正式域名为 `https://tokenrhythm.studio`。本地测试包通过 `npm run dist:local:test` 内置 `https://gpt.tokenrhythm.studio`；仅安装包文件名附加 `test-YYYYMMDD-HHMMSS`，应用版本、名称和数据目录与普通包一致，共用对话历史和设置。已有 `-test` 版本的目录选择逻辑也改为共用原目录；旧测试目录不会自动覆盖或合并到原目录。也可显式设置 `OPENSQUILLA_TOKENRHYTHM_ORIGIN=https://…`（无路径、查询、用户名密码）覆盖站点。后端返回的站点链接也需与该源一致。
- `contracts/desktop/tokenrhythm-account.d.ts` 是 renderer 的展示数据边界，无账号令牌、模型 Key 或本机代理凭证。
- Gateway 启动环境中的 `OPENSQUILLA_DESKTOP_ACCOUNT_URL/TOKEN` 在加载工具和 dotenv 前移除。`src/opensquilla/desktop_account.py` 在私有进程内获取临时模型凭证，不写入 TOML。
- 已有配置默认 `credential_source=manual`；新增账号专属连接可保存 `credential_source=account`，但真实账号绑定与 Key 均由加密账号模块持有。删除并存的手动连接保留账号模型/路由，并阻止环境 Key 补位。
- 传输层替换本机地址，服务身份继续使用官方 TokenRhythm 地址，维持能力、价格、关联头和计费回执处理。代理包含 `/api/models`、`/v1/models`、聊天、图片、视频提交与查询，不包含钱包 health。
- 每轮固定账号/媒体来源快照；账号失效不能回退到手动 Key 或环境变量。聊天传输不确定失败不重放推理；代理不自动重放视频提交。视频任务按账号归属保存；模型缓存包含临时凭证指纹。
- 启动、窗口前台与 60 秒轮询核验账号；请求发出前补做过期核验。网络失败保留加密凭证但暂停新调用。明确撤销或刷新失效才结束会话。
- 退出先关闭本机授权并写退出标记，再清除 Key 与展示缓存；离线待撤销任务加密保存、按旧会话重试，不阻塞新账号验证。
- 取消授权后才返回的令牌也先进入加密待撤销队列；断网或重启不会丢失清理任务。队列保存失败时阻止正常退出，待存储恢复后重新保存。
- 完整配置迁移先暂停并排空账号操作，替换目录前关闭账号及代理、清除实例缓存，之后从磁盘重新初始化。迁移排除 `desktop-account` 中的会话、待撤销任务与 installation ID，目标 profile 需要重新登录；源 profile 和替换备份保持不变。

## 网站衔接

### 浏览器登录结果页

桌面接收一次性回调后，将浏览器重定向到不含授权码和 state 的随机本机结果地址。
页面先显示“正在完成登录”；账号验证、加密存储和模型凭证就绪后，才显示“登录成功”、
返回桌面端的说明与“打开 OpenSquilla”按钮。取消、网络／存储故障和凭证领取失败
不会显示成功。结果接口只返回完成状态，不包含账号资料、令牌或模型 Key。

安装版通过固定的 `opensquilla://open` 尝试唤起应用，保留按钮供用户再次点击；
浏览器是否显示“打开应用”确认框取决于其权限设置。开发模式不注册或调用系统协议，
通过校验同源请求的临时本机接口唤起本次登录所在的桌面窗口。结果监听最多保留五分钟，
新登录、退出程序或配置迁移／更新维护会关闭它。关闭浏览器页不会取消已经接受的令牌交换。

页面由桌面客户端提供，修改后重新运行 Electron `npm run dev` 即可本地验证；
已安装版本需要更新桌面构建。网站授权确认页仍由 TokenRhythm 后端提供。

本地验证：Electron 构建、账号及生命周期 59 项测试、现有深链接与单实例脚本通过；
真实 Chrome 使用模拟账号验证等待／成功／失败／取消状态、自动和手动唤起回调、
严格 CSP 及桌面／手机布局。未执行安装版系统协议弹窗或真实账号 UAT 验收。

账号接口的 `links.account/usage/recharge` 返回根相对路径，桌面主进程使用自己的账号站点 origin 拼接完整 URL；UAT 和正式环境使用各自的 origin。为分批发布兼容旧后端的同源完整 URL，继续拒绝外域、协议相对地址、用户凭据及非网页协议。

后端 `/api/oauth/authorize` 未登录时跳转 `/login?return_to=…`，返回地址从已经校验的 OAuth 请求重建。网站的密码登录、短信登录/注册、已有登录态使用同一校验规则，以完整页面导航回 `/api/oauth/authorize`，仍需用户确认授权。

只接受同源根相对 `/api/oauth/authorize`；拒绝外域、协议相对地址、反斜杠、控制字符、片段及其他路径。现有协议同意和受限账号分流保持生效。相关改动位于相邻的 `token-rhythm-frontend` 与 `token-rhythm-backend` 仓库。

## 已执行的验证

以下均为本地自动化或模拟数据，不代表真实账号、计费及系统安全存储验收：

| 范围 | 结果 |
| --- | --- |
| Electron TypeScript 构建 | 通过 |
| Python Gateway wheel（包含校验后的 WebUI） | 通过 |
| 账号/OAuth/代理专项 | 17 项通过；包括状态/PKCE、超时/取消、刷新并发、保存失败、重启、网络暂停、退出断流、来源隔离、视频归属 |
| 现有 Electron 存储策略、解密缓存、向导协调、空配置、语言、profile 脚本 | 通过 |
| 最终账号来源与配置编辑回归 | 175 项通过；包含账号专属连接改为手动 Key、保留账号引用、防止环境 Key 补位 |
| Python 账号、来源、模型配置、Gateway RPC、目录回归 | 334 通过，1 跳过 |
| Python 路由/融合/选择器/模型目录及账号回归 | 498 通过 |
| Python 契约测试 | 503 通过，4 跳过；未开启需要独立工具链的集成开关 |
| WebUI 类型、架构、六种语言键一致性、主题/运行时 bundle、正式构建 | 通过 |
| WebUI 设置/账号专项 | 628 项通过 |
| WebUI 全量单测 | 11264 通过；文件搜索请求竞态 2 项失败，原始 HEAD 快照可复现同文件的竞态失败 |
| 网站认证/账号/返回地址测试 | 247 项通过 |
| 网站前端客户端与 SSR 构建 | 通过 |
| 后端 OAuth / OpenSquilla 六组测试与构建 | 81 项通过；构建通过 |
| 修改过的 Python 文件 Ruff | 通过 |
| 全量 Python mypy | 仍有原始代码 `engine/routing/policy.py:187` 的 `no-any-return`，原始 HEAD 文件可复现；本次新增代码无剩余类型错误 |

界面使用模拟数据检查了登录、未登录、等待授权、验证失败及深色主题。新增组件测试覆盖余额失败不显示假零、精度、离线恢复后的退出入口、账号卡片可用性和折叠菜单键盘操作。

## 复验命令

### 2026-10-22 审查修复

- 手动连接的发现、启用及删除主连接后的替换使用显式 `credentialSource=manual` 校验；校验失败不改变桌面的账号绑定，成功启用后才切换来源。仅切换其他服务商或在路由页选回 TokenRhythm，不改动已有账号来源。
- 账号卡片使用 `credentialSource=account` 在本次操作中验证当前登录账号，成功后才保存来源绑定；取消或失败保留之前选择的账号。激活后的服务商显式写入配置，重启后仍为 TokenRhythm。
- 模型服务编辑器与运行中路由使用各自的模型目录。钱包刷新不重载表单；真实账号状态变化保留未保存输入，设置状态按一次最新桥接快照生成。
- 仅登录不会把尚未保存的 TokenRhythm 连接加入路由。退出后仍可将显式账号图片/视频来源恢复为手动或沿用。
- 视频任务按服务域名和账号 ID 校验归属，允许同账号的本机临时凭证轮换，继续拒绝其他账号、环境及手动来源接管。
- 正常退出及信号退出等待进行中的账号令牌交换、刷新和加密写入；超出退出时间预算时保留进程并允许重试。刷新后保存与退出标记同时写入失败时阻止退出，存储恢复后须成功保存已清空的账号状态才能完成退出。
- `tokenRhythmManual` 已纳入状态类型和冻结契约测试，非桌面环境返回 `null`。

本次修复验证：配置／Gateway／媒体相关 Python 测试 689 项通过（含新增 30 个 RPC 回归场景）；契约测试 503 项通过、4 项跳过；WebUI 87 个文件共 1,872 项测试通过；Electron 账号／退出／相关生命周期测试 31 项通过。Electron 构建、WebUI 类型与架构检查、修改文件的 Ruff 及相关 Python 类型检查通过。以上仍为本地自动化验证，真实账号和系统安全存储 UAT 尚未执行。

二次审查修复：账号模型请求结果不确定时，同时设置普通错误码和结构化错误码，防止上层恢复逻辑按原始 502/504 再次发送请求；保留 HTTP 状态用于诊断，手动 Key 的重试策略保持不变。macOS／Windows 更新安装前暂停账号新操作，等待已接受的令牌交换和加密写入完成；超时或失败后等待原操作结束再恢复，旧更新的恢复回调不能解除新更新或普通退出的暂停。回归覆盖真实 HTTP 错误解析、模型重试／回退、刷新与写盘等待、已接受的登录回调、更新超时及安装失败恢复。

迁移与取消授权修复验证：Electron 构建通过；账号、退出、更新、清理及迁移共 51 项测试通过；完整配置迁移测试 190 项通过、2 项跳过。迁移回归覆盖等待中的刷新、发布前失败、发布后丢失 CLI 结果和账号排空超时；取消授权回归覆盖断网重试、重启及存储故障恢复。向导协调、profile 基础与整合、清理契约脚本以及修改 Python 文件的 Ruff 检查通过。

在 OpenSquilla 仓库：

```sh
cd desktop/electron
npm run build
node --test scripts/test-tokenrhythm-account.mjs
node --test scripts/test-desktop-account-quit.mjs
node --test scripts/test-desktop-account-update.mjs
node --test scripts/test-desktop-account-cleanup.mjs scripts/test-desktop-account-migration.mjs
node scripts/test-windows-update-integration.mjs
cd ../../opensquilla-webui
npm run build
npm run test:unit -- src/composables/setup src/components/settings/SettingsAccountPanel.test.ts
cd ..
.venv/bin/pytest -q tests/test_desktop/test_account_source.py
.venv/bin/pytest -q tests/test_migration/test_opensquilla_home_migration.py
.venv/bin/pytest -q tests/test_desktop/test_account_setup_rpc.py tests/test_tools/test_media_video_credentials.py tests/test_contracts/test_onboarding_status.py
PYTHONPATH=.:src .venv/bin/pytest -q --confcutdir=tests/contracts tests/contracts
.venv/bin/mypy src/opensquilla --show-error-codes
```

Electron 专项使用模拟上游与加密适配器，需要允许本机回环监听；不会登录真实账号或产生模型费用。首次 WebUI 构建需要下载仓库校验和锁定的字体资源。

## 独立 UAT（尚未执行）

macOS 和 Windows 各准备显式隔离的验收 profile 及 UAT 站点，确认后端已启用相应 OAuth 客户端及集成接口。安装包默认与普通版共用数据目录；仅验收脚本显式指定隔离目录。

1. 新安装分别通过密码、短信登录、短信注册及已有网站登录态完成授权；核对取消、重复回调、超时、端口不可用、协议同意和受限账号流程。
2. 首次登录自动启用；已有有效或无效手动配置不被覆盖。并存来源间切换保留模型与路由，路由/融合均按所选来源计费。
3. 验证聊天流、工具调用、图片、视频提交/恢复、模型目录与真实计费回执。独立媒体 Key 保留，显式切到账号后才使用账号；视频跨账号查询拒绝。
4. 断网后显示验证暂停与旧余额时间，不自动重放推理；恢复网络后可验证。覆盖余额不足、限流、钱包失败、Key 禁用/换代。
5. 退出进行中的账号任务立即中断；离线退出重启后仍未登录，联网后只撤销原会话。换账号不接管旧任务/绑定。
6. 双设备同账号领取相同 `key_id/generation`，单设备退出不影响另一台设备；不在验收报告中记录 Key 明文。
7. 检查 renderer、TOML、日志、凭证查看接口和工具子进程环境无长期账号凭证；验证无系统钥匙串弹框、本地文件权限、缺失或损坏密钥时拒绝覆盖，以及重启恢复。旧钥匙串凭据应提示重新登录且保留原文件。
8. 检查浅色/深色、侧栏折叠、键盘焦点、六种语言与桌面更新入口。
