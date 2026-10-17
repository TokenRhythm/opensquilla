# 资源打开与批注能力统一开发计划

- 日期：2026-10-17
- 状态：方案与多 agent 源码 review 已完成；架构尚未实施。
- 基线：PR 集成工作树 `149a6f3fe`。本轮唯一产品代码修复是产物列表事件绑定；不改变资源身份、权限、主/side 生命周期或批注协议。
- 关联：[会话切换与工作台状态保留](session-workbench-retention-plan.md)、[提示词批注编辑](prompt-annotation-editing.md)。

## 背景与已确认的问题

用户从聊天链接、右上角产物列表、工作区文件树及 Agent 的预览工具打开同一个文件，会看到不同的外壳、模式和操作。普通 PPT 文件预览只有阅读工具，而注册的 document 预览另有版本、变更和请求修改。HTML 的入口、显式源码意图、失败回退及资源 bundle 也不一致。入口差异不应成为用户理解功能的前提。

本轮先修复独立 bug：`WorkbenchResourceCollectionPanel` 发出 `workbenchEvent`，而 `AppWorkbench` 的对象式 `v-on` 使用 `workbench-event` 键，生成了不匹配的监听属性。列表打开、下载等事件不能到达 runtime。两处监听改用 camelCase 键，保留渲染时生成的事件处理闭包及旧版本缩略图防污染校验。该修复不能被表述成资源架构已经统一。

### 当前链路与能力差异

| 入口 | 当前身份及链路 | 需要统一的体验 / 必须保留的区别 |
| --- | --- | --- |
| 右上角产物列表 | `resource-collection` → `openWorkbenchResource` → resources `openCurrent`，可返回 canonical document | 修复事件后可正常打开。下载原始交付、打开当前工作文档、查看固定历史版本仍须区分 |
| 聊天中的普通工作区文件链接 | `kind: file`，按 session、工作区绑定及路径标识；普通文件 renderer | 对已有注册文档应能解析到相同资源；带行号的引用仍优先源码定位 |
| 右侧工作区文件树 | 文件服务与 `WorkspaceFilesPanel` 自有选择及阅读状态 | 按需解析所选文件，不能遍历时注册每个文件，不能复制整个文件树进入会话库存 |
| `open_workspace_preview` / document 预览 | 注册的文档、revision、预览 bundle / lease；Office 共用 renderer | 统一资源动作与外壳，保留版本、授权和完整 HTML 依赖环境 |

源码锚点：`AppWorkbench.vue` 的 `openWorkbenchResource` / `resourceSessionKey`，`workbenchResourceItems.ts`，`artifactItems.ts`，`WorkspaceFilePanel.vue`，`WorkspaceFilesPanel.vue`，`ResourcePreviewPanel.vue` 与 `artifactWorkbenchProvider.ts`（均在 `opensquilla-webui/src`）。后端锚点：`gateway/workspace_files.py`、`gateway/generated_artifact_adoption.py` 与 `artifact_session/working_files.py`。

普通 HTML **已经默认预览**，不是所有文件入口都默认源码。带行号的打开、显式源码模式、用户保留的模式、富内容读取失败回退需要分别判断。普通文件的离线 `srcdoc` 与注册文档的 bundle / lease 不具有相同的依赖加载环境；不能只把默认模式改成 preview 就宣称行为统一。

## 产品目标与范围

1. 同一 owner、工作区和资源版本从不同入口打开时，复用资源身份、打开动作及适用的工作台标签。用户上次选择的模式与阅读位置保留，显式的新打开意图优先。
2. 可渲染文件默认看效果；代码文件默认源码；带行号的链接默认源码并定位。显式预览、源码、下载、外部打开均保持各自动作含义。
3. 请求修改不应被局限为“只能改选区”。用户指令决定修改范围，选区是辅助定位的上下文，候选文件是最终写回目标。
4. 能力由资源、格式、平台和当前权限共同决定，不由从哪个入口进入决定。无可用能力时给出具体原因和可用替代动作。
5. 沿用既有聊天、资源服务、审批和写回链，不新增专用 Agent、模型工具或另一套资源数据库。

本轮不实现这些架构变化；不扩展普通主会话历史工具发现问题，不改变 main / side 的创建、上下文快照、续租、关闭、后台执行与回收语义。

## 建议的代码组织

### 1. 扩展现有解析能力，先找已有文档

复用文件服务的 resolve 与现有 `artifact_working_sources` / generated-artifact adoption 映射，按授权的 session、工作区绑定及规范化路径查找 canonical document；不要按 basename、展示名称或内容 hash 猜测同一资源。查不到时保留普通文件身份，不在普通读取中隐式创建文档。

将“当前工作文件”“可变文档 head”“已发布交付”“固定历史 revision”明确建模。`auto` 打开可以沿后端明确关系指向工作文档，但历史版本和原始交付下载不能悄悄追随最新 head。外部文件变化或文件替换必须重新验证映射和 revision。

显示归属与读取授权分开：side 文件可以显示在父会话的工作台，但仍使用 child 的资源权限和 sessionKey。审查发现 `sessionKeyFromWorkbenchItem` 对 collection 的 payload 与父 scope 组合存在需验证的风险；这是后续专项验证项，不代表本轮已复现或修复 side 库存串读。

### 2. 一个打开协调入口，各格式保留 adapter

在现有 workbench 模块内提取公共打开协调函数 / composable，接收资源引用、读取 session、显示 owner、意图 `auto | preview | source | external`，以及可选行号 / 初始阅读位置。聊天链接、产物列表、文件树、工具预览分别构造参数，停止各自决定资源身份与默认模式。

协调函数负责 resolve、能力判断、请求失效检查、同 owner 标签复用和激活；格式 adapter 继续负责渲染和内容载入。标签 identity 不包含普通 preview/source 模式，但固定历史版本、diff、跨 owner 或跨工作区资源不能被合并。所有异步结果继续使用现有 generation / owner 机制，关闭或切换后不得抢占当前会话。

没有注册的文件仍可只读预览。只有用户显式请求需要文档能力的操作时，才按授权创建 / 采用工作文档；HTML 必须先确定 entry、依赖 bundle 和版本边界。不要为了打开文件自动模拟模型工具调用，不要在文件树列举或历史恢复时自动 import / publish。

### 3. 共用工作台外壳与能力呈现

文件标题、预览/源码、下载、外部打开及请求修改使用相同的能力呈现规则。版本/变更仅在存在文档关系时展示，普通只读文件不伪造版本历史。复用现有 renderer，逐步消除多层重复工具栏，不把工作台重构与事件 bug 混为一个补丁。

HTML 若依赖不可用、预览出错或资源不受支持，明确说明状态，提供源码 / 下载；不能静默展示源码却声称已经预览成功。错误不是权限绕过的理由，不用宿主 `file://` 或取消 iframe 隔离来补 WebUI 能力。

### 4. 统一批注请求合同，保留 HTML / Office 定位差异

当前 HTML 与 Office 已共用批注草稿、正常聊天提交以及 `resolve_page_context → render_page_context`。无需另建编辑 Agent、工具协议或发送通道。

| 层次 | 共用部分 | 格式 / 平台差异 |
| --- | --- | --- |
| 用户动作 | 请求修改、可选选择上下文、自然语言指令、草稿编辑与取消 | 无精确选择器时，允许表达整页 / 整文件修改；具体入口待实施时定稿 |
| 请求身份 | owner / session、resource / document、revision 与 hash、用户修改意图 | HTML entry / bundle 与页面目标；Office slide/page/sheet、shape/text/range |
| 定位 | 选区用于解释指令，不能作为直接写句柄或额外权限 | DOM locator 与 OOXML 标识各自校验，不互相强制转换 |
| 写回 | 现有审批、候选文件、版本比较、原子提交、冲突反馈 | HTML 依赖与页面验证；Office 完整包验证与渲染验收 |

HTML 精确 DOM 选择目前要求 desktop native surface 的协议 3/4 和 IPC；Office 在本地 renderer 使用 document capability 判断，并非一律 desktop-only。WebUI HTML 批注后续需要受控预览 bridge / adapter，不能承诺普通网页和任意 iframe 都支持选区。

`gateway/page_context.py` 中 “Modify only this candidate file” 限定的是候选写回目标；`artifact_session/working_files.py` 接受完整 OOXML 候选文件，不是只能改一个 shape 或一句文本。后续应澄清提示词：用户明确要求可以覆盖整页 / 整文件，允许正常权限下的辅助脚本或临时素材；保持无关内容、处理歧义，并保留 document/session/revision/SHA、未发布修改和 CAS 校验。格式验证通过不等于排版保真，仍需真实渲染对比。

## 资源与生命周期约束

- 打开动作只处理选定资源；复用 existing single-flight、AbortController、metadata LRU 和 runtime restore queue，不新建常驻资源镜像或事件总线。
- 现有资源库存为最多 12 个 session / 2 MiB 元数据预算，文档元数据最多 32 项 / 2 MiB；活动、在途和未决 mutation owner 受保护，可超预算。这不是总 JS heap 上限。
- 标签描述符、阅读状态、业务操作 owner 与 renderer 分离。隐藏可释放 Office/PDF/iframe 可重建资源，不能因此结束 side、PTY 或已接受的 Agent 任务。
- 解析或准备预览中切换 session，旧响应只能更新原 owner；关闭后不能重开标签。用户编辑草稿、未决提交、固定版本选择不能被 LRU 丢弃。
- 回退时停用新的打开协调入口，保留已有资源 ID 和描述符的可读性；不通过清空用户标签或草稿回退。

## 多 agent reference review

本节是固定版本的本地源码分析，未运行 reference 客户端，也不等于用户录屏行为的端到端证明。三个独立 reviewer 分别检查 ZCode、DeepSeek、OpenSquilla 最小补丁与反例边界。

### ZCode

- 路径：`C:/code/reference/ZCode`；SHA：`29628c9acdb81b703bbd4080c207a0e7ce5e276e`。
- `packages/ui/src/lib/workspaceSidePane.ts:943-981,1499-1507`：目前对 PPTX / media 做了归一，渲染 source 类型不等于资源身份；按 sourceKey 和 ownerTaskId 复用。diff 不应仅按路径去重。
- `packages/ui/src/hooks/useAppPanels.ts:322-345`：workspace 与 owner 共同参与面板恢复。
- `packages/ui/src/app-shell/WorkspaceShellLayout.tsx:1257-1288` 与 `packages/ui/src/lib/workflowArtifactOpen.ts:17-28` 仍有不同的聊天路径 / artifact 打开链；`packages/ui/src/workspace-file-tree/WorkspaceFileTree.tsx:538-554` 从文件树打开 CodeViewer，而聊天 HTML 可进入浏览器，不能宣称它已将四个入口全部统一。
- `packages/ui/src/PreviewPane.tsx:597-604,632-634,708-725`：评论携带工作区、路径、行号、选中文本进入聊天，是上下文而非直接写权限。
- `packages/ui/src/lib/workflowArtifactOpen.ts:8-15,24-27`：本地嵌入浏览器 `file://` 路径受环境限制，Web、远程工作区等采用其他路径；不能移植为 Gateway WebUI 的通用方案。

采用其身份、owner 复用和上下文原则，不照搬平台专属打开方式，也不把现有入口分歧当目标架构。

### DeepSeek harness

- 路径：`C:/code/reference/deepseek-harness`；SHA：`d347e703908d0406b7a7ef80e3a0e594d86b2215`（`dsh-0.1.3-alpha.1`）。
- `packages/client/ui-chat/src/client/apply.ts:122-127` → `packages/api/session-controller/src/index.ts:269-299`：统一 openFile / openWorkspacePath 动作。
- `packages/client/ui-deliverables/src/client/index.ts:82-91`：内联引用和 ProducedFiles chips 共用选择器与 openFile；产物源自成功文件操作的事实。
- `packages/client/ui-deliverables/src/client/turn-deliverables.ts:34-58,105-120` 的工具白名单并不覆盖所有 shell / 转换 / Code Dispatch 子调用；OpenSquilla 应继续使用自己的权威 artifact/document 记录。
- `packages/util/native-command/src/path-opener.ts:29-30,120-150,175-187`：HTML 默认系统浏览器外部打开，可支持“默认看效果”的产品判断，不能作为内嵌 sandbox 或驻留资源管理的实现依据。
- `packages/context/file-reference/src/index.ts:16-17` 与 `file-reference-local/src/index.ts:66-75`：引用按需读取，有 read 能力才注入提示，未读不能声称已看过。
- `ui-deliverables/src/client/index.ts:41-65` 的并发合并 / 重连 generation 和 `file-reference-local/src/index.ts:45-62,91-110` 的查询规模 / dispose 可借鉴。

该版本未找到与录屏等价的 HTML DOM picker、Office selection → 批注写回或原生 WebContents 驻留方案。采用统一动作和按需上下文，不把路径引用替代版本化 document 身份。

### 反例 review 后保留的约束

1. 显示 owner 不等于读取 authority，尤其 side；不把父 scope 当 child 资源授权。
2. current head 不等于固定交付或历史 revision；原始下载、预览、工作文档修改不能被一个无差别动作吞掉。
3. 文件树不是另一份 session 产物库存，不全树扫描注册、不另建无限缓存。
4. 产物事件补丁保留 render-time handler closure。不能改成事件发生时才创建 handler，否则旧缩略图可能被错误绑定到新 revision。

## 分阶段实施与验收

| 阶段 | 交付 | 必须通过的定向验收 |
| --- | --- | --- |
| P0 独立 bug | 两处事件监听修复，真实宿主组件回归 | PPT/HTML 列表点击、下载、错误与重试；已有 kebab emit 与旧 revision 缩略图防污染 |
| P1 身份解析 | 扩展已有 resolver，明确工作文档与固定版本关系 | 四入口打开已注册 HTML/PPT 到相同资源；同名不同目录/工作区/owner 不合并；side 用 child 权限；历史固定版本保持不变 |
| P2 打开协调与外壳 | 统一打开意图、标签复用、能力动作及状态恢复 | 默认/显式源码、行号、预览失败、刷新、冷恢复；修改/删除/权限变化；快速切换和关闭中返回不抢占 |
| P3 批注合同与格式 adapter | 共用请求修改入口，明确范围；按平台逐项补能力 | HTML entry/bundle、Office slide/shape/cell、整文件请求；发往正确 main/side；过期 revision 冲突；完整候选验证、无关内容与实际渲染对比 |

P1-P3 每阶段先做受影响定向测试，再用 WebUI / 必要原生客户端实点和截图验证。已有主/side 生命周期与资源压力基线只在受影响时复验，不为每次小改动跑全量回归。WebUI fixture、真实 Gateway、原生客户端与真实模型 API 的证据分别记录；不把源码 review 或 CI 通过写成所有功能已实测。

实施前仍需定稿：普通未注册文件第一次“请求修改”的采用行为、固定交付进入当前工作文档的提示、WebUI HTML 选择器首期范围。默认建议是显式动作时采用、版本关系清楚呈现、WebUI 先支持无选区请求修改再逐步增加受控选择器；这些尚未改代码。

## 本轮 P0 验证记录

- 修复提交：`40d7e4fb9`。新增宿主集成测试先在旧绑定下失败，再在修复后通过；通过真实列表组件、事件绑定和 runtime/provider，覆盖 PPT/HTML 的下载、权限错误、重试与 canonical preview。预览引擎 I/O 使用测试替身，不冒充真实渲染证据。
- 定向测试：`AppWorkbench.navigation.test.ts`、`WorkbenchResourceCollectionPanel.test.ts`、`workbenchResourceCollectionProvider.test.ts`，共 3 文件 / 32 项通过，包含原有旧 revision 缩略图防污染用例。
- `build:artifact` 通过，18829 使用 PR 树的新 bundle。真实 Gateway / 浏览器从右上角产物列表打开“星空画廊.pptx”，渲染 6 页并翻到第 2 页；打开 `index.html` 渲染“星空画廊”，下载成功，再次从列表打开复用同一个 HTML 标签。该验收页未捕获 warn/error 控制台记录。
- 此次仅验证上述相关行为；未重新跑 41 样例或全量回归，未验证尚未实施的 P1-P3、WebUI DOM picker、Office 写回或 packaged Desktop。
