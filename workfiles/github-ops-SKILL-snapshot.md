---
name: github-ops
description: GitHub 操作元技能 v3.0（通用三层结构）。L0 通用核心：前置检查/红线确认块/建库前查重名/敏感扫描三分类/PowerShell 坑/失败策略。L1 仓库档案：OpenSquilla 四实体 fork + seira-agent + deepread 私有库（纯配置）。L2 场景库：A 同步上游（零覆盖）/ B 备份 / C PR / D 分支卫生 / E 新建仓库上传。任何涉及 fetch/merge/push/建库/PR/解决合并冲突的操作前先读此技能。
triggers:
- 同步上游
- 合并上游
- 备份仓库
- push origin
- realfork
- PR 冲突
- 解决冲突
- cherry-pick
- merge upstream
- 新建仓库
- 上传仓库
- 上传项目
- 私有库
- 私人库
- 建库
---

# GitHub 操作元技能（v3.0 通用三层结构）

取代旧 `github-pr-workflow`（已并入本技能删除）。实战来源：PR #1044~#1331、2026-08-21 上游同步 + 功能恢复 + 备份全流程、2026-08-26 seira-agent/deepread 新建私有库 + 公开库误占用事故。

**版本：v3.0（2026-08-26）**
- v3.0 特殊性→普遍性：流程（L0）与配置（L1）分离——换仓库只需在 L1 加一行档案；新增 0.6 建库前查重名、0.7 敏感扫描三分类、0.8 PowerShell 坑扩充、场景 E 新建仓库上传；OpenSquilla 专属配置（四实体/主权区/webui 构建）从流程下沉为 L1.1
- v2.1（2026-08-21）：0.1 前置检查、2.0 checkpoint 断点恢复、各场景失败策略（retry ≤ 2 → fallback）、0.5 确认块预填检查项
- v2.0：0.5 红线确认句、2.0 强制检查清单、2.1 分级报告、场景 D 分支卫生

> 全部命令来自实战验证，非理论推断。

## L0 通用核心（任何 GitHub 操作先过这一层）

### 0.1 前置环境检查（每次操作前一次性执行，全绿才动手）

```powershell
# ① gh 已登录（能看到目标账号）
gh auth status
# ② remote 齐备（该仓库需要哪些 remote 由 L1 档案定义）
git remote -v
# ③ 本地仓库非浅克隆（浅克隆导致 merge-base 失效——2026-08-21 实证坑）
git rev-parse --is-shallow-repository   # true 必须先 --unshallow
# ④ 当前所在分支（确认不会在错误分支上操作）
git branch --show-current
```

任何一项不满足：先修复再继续。① 失败 → `gh auth login`；② 缺失 → `git remote add`；③ 为 true → `git fetch <remote> --unshallow`（一次性，网络流量大）。

**路由**：新建仓库 → 再过 0.6；向仓库发布新内容 → 再过 0.7。

### 0.5 红线强制确认句（8/21 事故后强化）

任何**远端不可逆操作**——删除任何分支/仓库/文件/PR、创建分支/仓库、提交/更新/关闭 PR、向私人备份库以外的仓库 push——执行前**必须先输出确认块**，等主人明确回复"确认"：

```
【待确认】操作：<具体命令>
影响：<会改变什么远端状态>
风险：<不可逆点>
预检：<该操作的前置检查结果——如删分支前的 OPEN PR 查询、push 前净差异 stat、建库前查重名结果>
```

确认块不是走过场：**没有主人的明确确认字，任何远端不可逆操作都不许执行。** 本地操作（commit、merge、分支删除、clean）不受此约束，但见场景 D 边界。
**重名即停**：0.6 发现目标名已被占用 → 不改造现有仓库（rename / 转可见性 / 改描述 / 覆盖内容），停下问主人（8/26 事故：擅自 rename + 转私了已发布的公开技能库，事后完整还原）。

**红线操作清单（预填检查项）**：

| 操作 | 执行前必须预检 | 确认块必带 |
|---|---|---|
| 删除任何分支（含 realfork） | `gh pr list --state open --head <branch>` 无关联 OPEN PR | OPEN PR 查询结果 |
| 创建 PR | `git diff upstream/main...HEAD --stat` 净差异只含目标文件 | 净差异 stat |
| push 私人备份库以外的仓库 | 目标分支非 main + 净差异符合预期 | 净差异 stat |
| 创建仓库 | 0.6 查重名通过（无重名） | 查重名结果 + 目标可见性 |
| 关闭 PR / 删仓库 | 列出影响面 | 影响面清单 |

### 0.6 建库前查重名（2026-08-26 教训固化）

```powershell
gh repo view <owner>/<name> --json name,visibility,description
```

- **重名存在 → 停下问主人**：复用更新？改名？不擅自改造现有仓库——现有仓库有自己的生命周期定位（8/26：deep-code-analyzer 是已发布的 PUBLIC 技能库，SEIRA 项目撞上名字，擅自 rename + 转私被主人纠正）。
- 404（不存在）→ 进 0.7。
- 主人确认复用现有仓库（如版本更新 v2.6.2→v3.3.0）：属红线操作，确认后走 clone → 覆盖 → 单 commit → push，**保留历史**；要全新历史（force push 丢旧提交）必须主人明说。

### 0.7 发布前敏感扫描三分类（2026-08-26 实践固化）

对拟发布目录全量扫描：

```powershell
Get-ChildItem -Recurse -File <dir> -Include *.yml,*.yaml,*.md,*.json,*.py,*.js,*.ts,*.toml |
  Select-String -Pattern 'api[_-]?key\s*[:=]|sk-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{20}|password\s*[:=]\s*\S|secret\s*[:=]\s*\S'
```

命中三分类处置：
1. **真密钥**（key 值 / sk- / ghp_ / password 值）→ 发布前必须删除或打码；若已外泄立即作废轮换
2. **公开代码引用**（分析档案里出现的被分析项目公开源码，如 AnythingLLM 公开源码）→ 正常，可发布
3. **工具术语**（tokenMeter、ask-user 等命中模式但非密钥的词）→ 误报，可发布

### 0.8 Windows/PowerShell 坑

- 无 `&&` → 用 `;` 或换行（`ParserError: "&&" 不是此版本中的有效语句分隔符`）
- `gh repo create --source . --push` 在 remote 已存在时**静默不推**（提示 Unable to add remote）→ 事后必验证：`git status -sb` 显示 `## main...origin/main`，或 `gh api repos/<owner>/<name>/branches`
- git push/fetch 的 remote 输出被 PowerShell 当 stderr 报红 ≠ 失败，看 `old..new branch -> branch` 行确认
- 无 `wc -l` → `Measure-Object -Line`
- upstream 被 force-push（fetch 提示 forced update）时，以 fetch 后的 upstream/main 为准重新比对 `git rev-list --left-right --count upstream/main...main`
- 脚本含凭证词（token 等）标识符的写盘脱敏坑：会被脱敏器破坏成裸名 REDACTED（py_compile 能过、运行 NameError）→ 变量改名 + 环境变量名运行时字符串拼接

### 0.9 通用失败策略

- retry ≤ 2 → fallback；仍失败 → 停下报告，不硬闯（不 force-push 硬闯）
- 长流程必须落盘 checkpoint 支持断点恢复（见场景 A.0）
- 判断不了 → 停下，把上下文（文件清单 + 双方意图摘要）给主人，不自行猜测

## L1 仓库档案（纯配置；新增仓库加一行，不改 L0/L2）

> **发布流水线认知**：`私有开发库 → 本地 skills/ 注册 → 公开技能库`，半成品只进第一格。同一代码线可并存于多格：如 deep-code-analyzer 线，已发布技能库 = `QinLuza/deep-code-analyzer`（PUBLIC），未完成项目 = `QinLuza/seira-agent`（PRIVATE）。两者不混淆——**重名不等于可占用**（见 0.6）。

### L1.1 OpenSquilla fork（四实体）

```
              upstream（官方源）= opensquilla/opensquilla
              fetch only · push never
                    │
┌────────────────────────────────────────────────┐
│ 本地仓库 D:\AIstudio\Harness\OpenSquilla-QinLuza-Studio │
│ ★ main = upstream 全部内容 + 全部 fork 改动          │
│   （唯一事实来源，所有功能直接在 main 开发维护）       │
└───────┬────────────────────────┬───────────────┘
        │ push 备份               │ push PR 分支（仅用户明确发话）
        ▼                        ▼
 origin（私人镜像）            realfork（真 fork）
 = QinLuza/opensquilla-QinLuza-studio    = QinLuza/opensquilla-QinLuza
 fork:false · 推一切安全       parent=upstream · PR head 来源
```

| 铁律 | 说明 |
|---|---|
| upstream 只 fetch 永不 push | 官方源只读 |
| origin 推一切 | 私人备份，每次有意义的 commit 都推 |
| realfork 未经用户明确发话不碰 | 仅发/更新 PR 时推目标 PR 分支；**永不推 main** |
| main 是唯一事实来源 | PR 分支 = main 的子集，只用于向官方提交 |

**推送矩阵**：

| 操作 | origin | realfork | upstream |
|---|---|---|---|
| 日常改动 / 同步上游后 / 备份 | ✅ | ❌ | ❌ |
| 创建/更新 PR | ✅ 顺手备份 | ✅ 仅 PR 分支 | ❌ |
| 删除 realfork 分支 | — | ✅ 但先 `gh pr list --state open` 确认无关联 OPEN PR，再走 0.5 确认块 | ❌ |

**主权区文件**（`src/opensquilla/squilla_router/`、`src/opensquilla/engine/routing/`、`src/opensquilla/provider/ensemble.py`、`src/opensquilla/router_tiers.py`）：上游若动了这些，**默认保本地版本**，逐行 review 上游意图后再决定是否吸收，不盲目跟随上游新写法。

**主题分支备份矩阵**（仅私人 origin）：功能模块 commit 进 main 后建同名主题分支推 origin。已建：`backup/context-waterline`、`fix/sqlite-busy-timeout`、`feat/openai-bridge-v2`、`ci/remove-schedule-triggers`、`fix/provider-model-discovery-rpc`、`feat/custom-provider-rebuild`。合并上游前后跑 `merge-base --is-ancestor` 覆盖验证。

**构建/运行时**：
- 2026-08-21 已执行 `git fetch upstream --unshallow`，merge-base/merge-tree 预检链路可用
- 前端纯改动：`cd opensquilla-webui; npm run build` → dist 落 `src/opensquilla/gateway/static/dist` → 浏览器强刷（Ctrl+Shift+R）即可，无需重启 gateway
- 后端 Python 改动：需重启 gateway（桌面 bat 拉起，kill 后看门狗自动恢复）
- webui 合并前检查：`npx vue-tsc --noEmit; node scripts/check-i18n.mjs`

### L1.2 seira-agent（PRIVATE，2026-08-26 建）

- 定位：SEIRA（Software Engineering Inspection & Repair Agent · 软件工程检查与维修智能体）未完成项目的私人开发库
- 源：`.agent-presets\deep-code-analyzer`（v3.3.0-preset）→ 本地工作副本 `workfiles/seira-agent/` → `github.com/QinLuza/seira-agent`（main，initial commit 8b76ce8）

### L1.3 deepread（PRIVATE，2026-08-26 建）

### L1.4 AGENTOS（PRIVATE，2026-08-27 建）

- 定位：设计工作区统一快照——OpenSquilla 基座改造设计堆（融合路由/fanout/子代理协议、SPOT-架构与 SPOT-E、SKILL 分层规范 V2.3、web-digest 草稿）+ 五个参照项目源码体检档案（opensquilla/deepseek-harness/hermes/ouroboros/pi）+ 废稿归档
- 源：`D:\ProductProject\AGENTOS`——与 L1.2/L1.3 不同，git 仓直接在源目录 init（该目录本身是独立项目目录非 AGENTOS 工作区，设计文档在此持续迭代，复制 workfiles 副本会造成双真相源）
- 特例：嵌套仓 `SPOT-架构/spot-framework`（= QinLuza/spot-framework，已独立发布）与 `.opensquilla/` 运行时附件已 .gitignore 排除，不随本仓重复打包

- 定位：DeepRead 吃透书五阶段闭环深读智能体（S1 透视→S2 苏格拉底批判→S3 知识锚定→S4 费曼转译→S5 行动转化，v1.0.0-preset，含 MCP server + 参考项目分析档案）
- 源：`.agent-presets\deepread` → 本地工作副本 `workfiles/deepread/` → `github.com/QinLuza/deepread`（main，initial commit d08bfb9）

## L2 场景库（SOP，引用 L0）

### 场景 A：同步上游（零覆盖）——参数化，默认 L1.1

#### A.0 同步前强制检查清单 + checkpoint

同步上游前**必须依序执行 5 项检查，全绿才允许继续**：

1. **当前分支 = main**：`git branch --show-current`
2. **工作区干净**：`git status --porcelain` → 0 行（有改动先 commit，未 commit 的改动在 merge 中可能被冲突覆盖）
3. **上游已 fetch**：`git fetch upstream main`
4. **冲突预检**：`git merge-tree $(git merge-base HEAD upstream/main) HEAD upstream/main` → 输出不含 `CONFLICT`
5. **净差异基线落盘（checkpoint）**：

```powershell
git diff upstream/main...HEAD --stat > workfiles/sync-baseline.txt
'{"stage":"preflight_done","baseline":"workfiles/sync-baseline.txt"}' | Set-Content workfiles/sync-checkpoint.json
```

**断点恢复**：merge 中断/冲突解决到一半被打断 → 读 checkpoint + 基线文件，确认已完成步骤（fetch/preflight/merge 到哪一步），从断点继续，不重跑；同步验证通过后删除 checkpoint。

#### A.1 上游改动分级报告（同步前知情权）

| 分级 | 含义 | 处理 |
|---|---|---|
| 无冲突 | 上游改了本地没碰的文件 | merge 自动吸收 |
| 自动合并 | 两边改了不同位置 | merge 自动处理，事后抽查 |
| 需人工决策 | `CONFLICT`，两边改同一位置 | 进入 A.3 |

主权区文件按 L1.1 处置。

#### A.2 同步

```powershell
git checkout main
git merge upstream/main     # 用 merge 不用 rebase，保留 fork 历史
```

**为什么不会覆盖**：merge 是三方合并——不同位置自动合并；同一位置两边都改 = 标冲突等人工决定；已 commit 的改动永不丢失。"同步后功能消失"不是覆盖，而是**功能本来就不在 main 上**（只存在于 PR 分支）。预防 = 所有功能直接在 main 开发，开发完立即合回 main。

**失败策略**：冲突 → 走 A.3，不 abort；非冲突错误（工作区被外部修改等）→ retry(1)；仍失败 → `git merge --abort` → 停下报告。

#### A.3 冲突解决范式（ChatComposer 模式，实证两次）

1. 读冲突标记区间，理解 HEAD（本地）与 upstream 各自意图
2. 保留我们的功能块 + 采用上游新增的守卫条件
3. 验证无残留标记：`Select-String -Pattern "<<<<<<<|=======|>>>>>>>"`（空输出 = 干净）
4. `git add <冲突文件>` → commit

解决到一半判断不了 → 停下，把冲突文件清单 + 双方意图摘要给主人（见 0.9）。

#### A.4 中断与残留处理

- merge 中断后残留的 untracked 上游文件会阻塞重新 merge → 先 `git status --porcelain` 确认无私有文件（workfiles/ 等），再 `git clean -fd`，然后重新 merge
- 想全部撤销 → `git merge --abort`

#### A.5 同步后验证与备份

```powershell
git diff upstream/main...HEAD --stat
Compare-Object (Get-Content workfiles/sync-baseline.txt) (git diff upstream/main...HEAD --stat | Out-String) -SyncWindow 0
# webui 有改动 → L1.1 检查命令
git push origin main
Remove-Item workfiles/sync-checkpoint.json
```

**失败策略**：Compare-Object 有差异 → 停下逐条核对，不 push origin，不删 checkpoint。

### 场景 B：备份本地仓库（通用）

- 每次有意义的 commit 后：`git push origin <分支>`；长期分支（main + 活跃 feature）都要备份
- origin 是私人镜像，推任何分支都安全；不需要也不应该推其他远端
- 备份完成即结束，不主动追问

### 场景 C：PR（参数化，默认 L1.1）

#### C.1 创建

```powershell
git checkout -b feat/xxx main
git cherry-pick <hash>          # 如需从别处只取特定 commit
git diff upstream/main...HEAD --stat   # 净差异必须只含 PR 目标文件
git push realfork feat/xxx
gh pr create --repo opensquilla/opensquilla --head QinLuza:feat/xxx --base main
```

> 红线操作，先走 0.5 确认块（预检 = 净差异 stat）。

**失败策略**：push realfork 失败 → retry(1)；仍失败 → 停下报告网络/认证错误，不换 force-push 硬闯。

#### C.2 更新 / 消冲突

```powershell
git checkout feat/xxx
git fetch upstream main
git merge upstream/main          # 冲突按 A.3 范式解决
git diff upstream/main...HEAD --stat   # PR 净差异必须只含 PR 目标文件
git push realfork feat/xxx       # PR 自动更新，冲突提示消失
```

> 红线操作，先走 0.5 确认块。

#### C.3 PR 卫生红线

- PR 分支上禁用 `git add -A`——显式只 add PR 目标文件
- 严禁混入：`workfiles/**`、任何工作区杂物
- merge commit 本身携带上游全量文件是正常的，不算污染；污染看 `git diff upstream/main...HEAD --stat` 净差异
- PR 分支更新后同步备份到 origin

### 场景 D：本地分支卫生（通用）

只清**远端已删除**的本地分支（`[gone]`），**绝不碰远端**——远端删除永远是红线操作走 0.5；本地清理可安全自动化。

```powershell
git branch -v        # 列出 [gone] 分支（有 worktree 的带 '+' 前缀）
git branch -D <branch>   # 有 worktree 先 git worktree remove --force <path>
```

边界：只删 `[gone]` 状态分支；本地有未推送改动的分支不删；该分支还挂在 OPEN PR 上（`gh pr list --head <branch>`）则只清理不推送任何东西。

### 场景 E：新建仓库上传（2026-08-26 新增，两次实证）

适用：新项目/预设/文档 → 私人库（或主人指定的可见性）。

```
Step 1  0.1 ①（gh auth）
Step 2  0.6 查重名 → 重名即停
Step 3  0.7 敏感扫描三分类 → 真密钥先清除
Step 4  读项目 SKILL.md / preset.yml / DESIGN.md 提炼定位，写 description
Step 5  复制到 workfiles/<name>/（工作区文件管理规则；源目录不 git init）
Step 6  cd workfiles/<name>; git init -b main; git add -A; git commit -m "Initial import: <定位> (<版本>)"
Step 7  gh repo create <owner>/<name> --private --description "<描述>" --source . --push
Step 8  验证：git status -sb（## main...origin/main）+ gh repo view <owner>/<name> --json name,visibility,defaultBranchRef
Step 9  登记 L1（加一行）+ MEMORY.md 仓库矩阵
```

- Step 7 是红线操作：主人已明确发话建库（如"上传到私人库"）则发话即确认，可省确认块；主人未明确仓库名/可见性时先走 0.5。
- **失败策略**：`gh repo create --source . --push` 在 remote 已存在时静默跳过推送（0.8）→ 单独 `git push -u origin main` 并用 Step 8 验证。
- 主人要的是更新现有仓库而非新建 → 走 0.6 复用流程，红线操作。

### 场景 F：功能恢复到 main（上游未合并 PR 时本地想用）

```powershell
git checkout main
git cherry-pick <PR功能commit>   # 冲突按 A.3 解决
```

## 维护记录

- 本技能结构性修改后，用纸上演练验证：逐条执行新增命令（dry-run 或在安全仓库上），确认无语法错误、路径正确。清单未全绿前不得视为改动生效。
- L1 新增仓库：只改 L1 表格，不动 L0/L2。
- v3.0 纸上演练（2026-08-26）：0.6 查重名（deepread 404 实证）、0.7 扫描三分类（两库实战 + 工具术语误报分类实证）、0.8 `&&` 坑与 `gh repo create --push` 静默跳推（seira-agent 实证）、场景 E 全流程（seira-agent + deepread 两次端到端实证）——全部由当日实战覆盖。
