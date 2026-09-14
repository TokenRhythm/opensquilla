# Windows validation prompt — local-first previews

The block below is a self-contained prompt for a Codex task running on a real
Windows machine. It requests verification, not automatic code changes or merging.

---

请在真实 Windows 上验收 OpenSquilla PR #1644：
https://github.com/TokenRhythm/opensquilla/pull/1644

目标是证明任务文件归属、Gateway 重启恢复、Desktop 原生标注修改和文件操作在 Windows 正常。
不要用 Linux/macOS 或 mock 结果代替 Windows 验收，不调用付费模型，不合并 PR。

## 1. 先确认环境和精确源码

- 读取仓库 AGENTS.md。只使用获得授权的普通检出目录，不在 `.codex`、系统临时目录或用户现有工作目录中直接跑完整验收。缺少合适目录时先询问。
- 只读获取 PR 当前 head SHA，固定该提交验收；报告 base/head、物理 cwd、Git root、未提交改动和系统/架构。保留任何原有改动。
- 核实 Python 实际导入当前检出的 `src/opensquilla`；核实 Node/npm、依赖及当前构建配置。共享 Git tree 不代表共享执行环境。
- WebUI 产物应由当前构建配置确认；不要假定历史路径。核实 WebUI 与 Electron 产物匹配验收源码。
- 所有日志、截图、fixture profile 和下载文件放在新建的专用验收目录；不要复用真实用户 profile、会话、密钥、Gateway 或端口。

## 2. 先跑环境预检和已失败文件

在仓库根目录，选择该检出的 Python，不使用碰巧在 PATH 中的另一环境：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& .venv\Scripts\python.exe -m pytest tests/test_sandbox/test_trusted_sandbox_execution.py tests/test_tools/test_approval_unification.py tests/test_live_multi_provider_matrix.py tests/test_live_provider_profile_smoke.py tests/test_session/test_delete_session_material_cleanup.py -q -rs --tb=short
```

保留真实退出码。PR 曾在 Windows 的以下三个 case 失败：

- `test_delete_captures_effective_material_root[delete-project]`
- `test_delete_captures_effective_material_root[prune-project]`
- `test_project_history_delete_cleans_only_its_sessions_material`

当时测试夹具把原始路径直接写入 `path_key`，与生产的大小写/斜线规范化不一致。核实修复使用生产 `project_path_key`，并实际重跑整个测试文件。不能删断言、加 skip 或降低权限校验来取得通过。

预检失败时先分类。确认是环境问题需要对照证据；不要连续重跑完整套件。

另一个 Windows CI 失败也需保留并复现：

```powershell
& .venv\Scripts\python.exe -m pytest tests/test_engine/test_agent_finalize_evidence_gate.py -q -rs --tb=short
```

失败 ID 为 `test_gate_caps_at_two_challenges_then_accepts_red_final`，实际模型调用 3 次、预期 7 次。该失败出现在名为 `desktop-installer-contracts` 的分片，但测试的是 Agent 完成前的证据检查，不是原生文件菜单。现有日志不足以认定是 Git 子进程超时或产品回归；核对 Git 状态采集结果，并在相同 Windows 环境对照 PR 基线。不能把 macOS 通过或主线跳过的 Windows 检查当作基线通过。

## 3. 后端与原生接口定向回归

```powershell
& .venv\Scripts\python.exe -m pytest tests/test_gateway/test_local_first_workspaces.py tests/test_gateway/test_execution_workspace_preparation.py tests/test_gateway/test_workspace_config_provenance.py tests/test_gateway/test_workspace_preview_registration.py tests/test_gateway/test_working_file_actions.py tests/test_tools/test_sessions_spawn_regressions.py tests/test_tools/test_memory_workspace_ownership.py tests/test_session/test_delete_session_material_cleanup.py tests/test_engine/test_artifact_delivery_sources.py -q -rs --tb=short
```

按当前 package scripts 构建 WebUI 和 Electron，并运行：

```powershell
# cwd: desktop/electron
npm.cmd run build
node scripts/test-resource-file-actions.mjs
node scripts/test-artifact-preview-lease-broker.mjs
node scripts/test-native-workbench-surface.mjs
node --test scripts/fixtures/workspace-inline-preview/provider.test.mjs
```

检查 Windows drive-letter 大小写、反斜线、中文/空格文件名及当前支持的 junction/symlink 情况。权限不够的 case 必须明确标记未验收，不自动提权、关闭 Defender 或修改系统执行策略。

## 4. 真实链路：用免费确定性模型替身

读取本目录 README.md 和 `verify-journey.mjs --help`。复用现有 loopback-only provider、真实 Gateway、普通文件工具和原生 Desktop；不要预先写好产物或注入伪造的成功工具回执。

```powershell
# cwd: desktop/electron；参数必须换成已授权的真实路径。
node scripts/fixtures/workspace-inline-preview/verify-journey.mjs --source-root <普通检出绝对路径> --output <全新Web证据目录> --surface web
node scripts/fixtures/workspace-inline-preview/verify-journey.mjs --source-root <同一普通检出绝对路径> --output <全新Desktop证据目录> --surface desktop
```

如果脚本因本机条件无法完成某一步，可通过真实客户端操作补验，但必须保留原失败，不能手工修改数据库/store 来跳过流程。

### Web 必须观察到

1. 普通任务 A/B 用同一相对路径生成不同标识的页面，持久根目录不同。
2. A 连续改三轮；每轮核对实际文件字节/哈希及页面，B 不变。
3. 第二轮后真正停止并重启测试 Gateway，用原 profile 恢复 A 后完成第三轮。仅刷新页面或重开 SQLite 不算进程重启。
4. 绑定与 Document 身份不变；不产生新的共享根产物。初始基线加三次不同修改应有四个版本，重复打开不增版本，普通预览零正式 publication/交付。
5. 若执行子任务场景，必须真实 `sessions_spawn` 并让子任务普通工具写入：父子绑定相同，文件落到 A，而不是父任务代写后宣称子任务成功。

### Desktop 必须观察到

1. 确认使用真实 owned local Gateway，而非把远端连接冒充本机。
2. 普通生成后正文入口可点击；右侧真正显示页面。
3. 点击原生标注按钮，圈选标题，输入 `把标题改成紫色`，提交聊天。
4. 模型请求中实际 `imageCount > 0` 且 `annotationCount > 0`；原 CSS 字节改变，页面 computed style 和可见颜色改变；Document 不换、保存工作版本、零正式交付。
5. 右键菜单不会被原生预览遮挡；键盘可达、关闭后焦点合理。
6. 实际点击“在文件资源管理器中显示”，定位真实源码，不是缓存副本。
7. 实际操作“另存为”：正常保存字节相同；取消不创建文件；覆盖测试只用专门新建的目标，取消/失败保持原内容。保存 HTML 不宣称包含外置 CSS/图片。
8. 若当前菜单提供默认程序打开，则实际核对原文件；若产品未展示该项，不临时加回，也不把接口测试算作 UI 通过。
9. 对远端连接，不出现服务器原文件的本机定位/打开能力；仍可保存对应内容。没有可用的独立远端环境时标记未验收。

## 5. 异常与兼容

复用现有定向故障注入测试核对：缺目录、撤权、路径越界、符号链接、过期会话、提交前后取消、同请求重试、删除会话。必须不回退共享目录、不递归删除源码、不删除其他任务材料。

明确项目/显式共享配置允许有意共享；旧空绑定会话保持旧解析。这些不是独立普通任务隔离失败。工作目录独立不等于新增了沙箱安全隔离。

显式发布后再修改源码，旧下载必须字节不变；普通预览不误拦 PDF/PPTX 或 Channel 的交付。未覆盖的格式/Channel 必须列出，不用 HTML 结果推断全部通过。

## 6. 交付报告与停止条件

- 输出逐项 PASS/FAIL/NOT RUN、精确命令/退出码、失败 test ID、系统和 head SHA。
- 附关键界面截图、A/B 目录及哈希对照、Document/revision/publication 计数、真实进程重启证据、原生标注的非零图片/标注计数。
- 单独列出 Windows 原生人工操作和自动合约检查，不混为一种证据。
- 发现问题先报告复现、预期/实际及最小修复建议；本任务不自动改产品代码、提交、推送或合并。
- 关闭本次创建的进程，保留证据；不关闭用户其他客户端，不删除真实数据，不上传密钥或私有会话。

全量验收只有在以上环境预检通过、目录授权和依赖/产物明确后才考虑；没有成功执行的整条命令不能标为通过。
