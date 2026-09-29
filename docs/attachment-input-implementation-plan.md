# 附件上限与本机原位引用：开发计划

状态：2026-09-30 Windows native 导入 409 修复、A/B/C 及 B2 已在 `integration/attachment-inputs` 工作树实现，定向回归与独立 Windows 打包候选验收通过。B2 将 Desktop 自有本机 Gateway 下普通非图片拖拽直接转为本机路径输入（消息中的路径引用）；证据及尚未覆盖的发布边界见第 13 节。D 的元数据 v2 协议本轮不实施，保留为后置优化，不作为 A/B/C/B2 的交付前提。下文“PR A/B/C/B2/D”表示工作包划分，当前未提交 commit、未创建 PR、未发布新客户端。

## 1. 基线与范围

- 2026-09-29 已执行 `git fetch origin main`。
- 计划基线：`c4b1dd8368e5ed2050147219370e9e6d6a0bbfdc`（origin/main）。与上一轮源码评审基线 `6ddad374909fa5bd5c7f70212666f43d58933ce1` 的差异已检查：为 README、CI 和 telemetry 等测试变更，附件相关生产代码未变化。
- 实施分支：`integration/attachment-inputs`，从上述 `c4b1dd8368e5ed2050147219370e9e6d6a0bbfdc` 开始。原旧 HEAD `943b784a5fc5f6eaccb15090ac36a57f891f9275` 仅用于前期分析，未作为实现基线。
- main 新增的附件历史容量计量、显示元数据规范化和 composer 焦点保护必须保留。
- 实施分 A/B/C 三项独立交付及 D 一项独立可选优化。D 不阻塞 A/B/C；不删除 D 的目标或风险，只是不将其误列为原位操作成立的必要条件。
- 本轮修改附件及输入相关产品代码；不改会话生命周期、公开 chat wire、历史材料格式、文件权限或实际用户 profile。

目标是让 48 MiB 文件可以作为附件提交，同时让本机文件能够不搬运内容、由 Agent 使用现有工具原位访问。后者不是自动取得访问或编辑权限。

| 工作包 | 优先级 | 交付内容 | 依赖 / 风险 |
| --- | --- | --- | --- |
| PR A | P1，先做 | 普通副本附件 50 MiB，全链路边界一致 | 无 B/C/D 依赖；低—中 |
| PR B | P1 | 显式「引用本机路径（不上传）」入口 | 可独立于 A/C/D；低—中，重点是可信选择和防串会话 |
| PR C | P1 | 已分类 live 引用的附件/草稿字节计费修正 | 不依赖 D；低—中，重点是旧记录和并发草稿 |
| PR D | P2，独立评估 | 原附件入口 workspace live 元数据准备、不整读/hash | 先完成性能基线和收益评估；中等风险，不阻塞前三项 |

建议执行顺序 A → B → C；B/C 可在独立提交中并行，交叉文件由一个实施者整合。D 的基线测量可提前，协议代码不混入 A/B/C。P1/P2 是开发顺序，不表示现有生产事故等级。

## 2. 已定的产品选择与场景

新增一个明确的「引用本机路径（不上传）」菜单动作；保留现有「添加附件」及拖拽语义，不将同一按钮静默改成路径引用。首版不做 chip、富文本编辑器或新的拖拽模式。

| 场景 | 定稿行为 | 原因与边界 |
| --- | --- | --- |
| Desktop 自有本机 Gateway；workspace 内/外；首条/后续消息 | 新动作通过系统 picker 插入本机路径输入（消息中的路径引用） | 无需创建 session、读取内容或上传；不声称已验证执行端可读 |
| 已有 session 的 workspace 内普通文件，经原附件入口 | A/B/C 阶段保留原 v1 整读/hash及单文件门槛；C 修正已知引用计费；D 通过验收后才改元数据准备 | 不将后置优化提前宣传为已支持；要直接引用大文件可使用 B |
| workspace 外文件，经原附件入口 | 保留副本附件 | 不悄悄改变原按钮的历史/恢复语义；要原位请用新动作 |
| 新会话，经原附件入口 | Desktop 原生非图片拖拽由 B2 转为本机路径输入；普通浏览器/无原生路径仍是普通附件 | 不为拖拽提前创建 session；B2 不读/hash/上传源内容，图片仍走普通附件 |
| 外部 Gateway，包括 localhost SSH 隧道 | 不提供本机路径 picker 动作；本机 File 走副本上传 | URL 是 loopback 不证明该 Gateway 由当前 Desktop 拥有 |
| 文件已经在远端执行环境 | 现有工作区引用或手写执行端路径 | 不因连接远端就要求再次上传，也不猜本机路径映射 |
| 自有本机 Gateway，但工具运行于 WSL/容器/其他 FS | 纯路径动作只表示本机位置；实际工具判断可达性和权限 | 不把 Gateway 所在机器等同于工具 FS；不可达明确失败，不自动搬运 |
| 普通浏览器 File、粘贴 Blob、截图 | 保留现有内容附件路径 | 没有可信、稳定的本机绝对路径 |
| 图片，经原附件入口 | 保留像素输入、预览、5 MiB 校验及模型能力路由 | 不把“给模型看图”降级为仅路径文本 |
| 手写路径 | 始终作为消息中的路径输入，由执行侧工具解释 | 不自动上传、提权、查本机文件或计入结构化附件数量 |

提示文案应表达「仅插入本机路径；未上传，读取受权限和执行环境限制」。路径可能发给用户选择的模型，不能宣传为路径信息永不离开电脑。

“固定副本”与“原位引用”保持不同语义；本轮不增加强制副本模式、外部文件卡片、远端路径选择器、自动挂载或跨主机同步功能。

## 3. 规格与不变量

| 项目 | 目标 |
| --- | --- |
| 普通 staged 附件单文件 | 50 MiB = 52,428,800 bytes，边界值包含在内 |
| 普通类别 | staged PDF、Office、经验证的 staged text、opaque；不是把所有 MIME 限额统一为 50 |
| 图片 / email / inline | 分别保持 5 MiB / 2,000,000 bytes / 2,000,000 bytes |
| 单轮附件内容总量 | 保持 60 MiB；C 不计已分类 live 源内容字节；未分类 native selection 的提前限额仅在 D 解决 |
| 结构化数量 | `attachments + workspaceFiles` 合计最多 16；本机路径输入不扫描计数 |
| 本机路径 picker | 单次最多 10 个常规文件；它是本机路径输入入口，不占结构化附件名额；只选择文件，不选择目录或递归枚举；受现有路径/文本长度约束 |
| 上传池 | 保持 300 MiB、临时 UUID 10 分钟；不是历史材料只有 10 分钟寿命 |
| 附件草稿 | 保持单份 60 MiB、总计 120 MiB、最多 20 份、24 小时；live 只存引用元数据 |
| B 的普通路径入口 | 无 30/50 MiB 上传门槛；不得整读、hash 或缓存源内容；空常规文件可引用 |
| 原结构化 workspace 入口 | A/C 仍有原准备阶段单文件限制；D 完成后才承诺不整读和不受上传源字节上限限制 |
| 工具预算 | 读取/解析/输出/上下文额度保持独立；可引用大文件不等于全文送入模型 |
| 现有配置 | 显式设置的更低 opaque cap 继续生效，不覆盖用户配置 |
| 严格附件配置 | `accept_opaque=false` 的副本上传保留旧可 stage 类别；普通文本仍为 2 MB，不顺手放宽 |

解析器现有 64 MiB 输入/包内解压总量、16 MiB XML、页数及输出长度等限制不变。48 MiB 压缩包能上传，不保证任意解压或解析成功。

live 的合法路径/权限决定可访问性，不使用上传 MIME 规则代替文件工具权限。`accept_opaque` 不是禁止 Agent 访问所有未知扩展本地文件的全局权限开关。live 图片的实际像素读取仍受图片限制。

## 4. PR A：50 MiB 副本附件

优先级：先实施。风险：低到中；规格改动小，但存在多次缓冲和跨入口回归。

六个生产文件的最小改动：

| 文件（相对仓库根） | 改动与原因 |
| --- | --- |
| `src/opensquilla/contracts/attachments.py` | 四项普通 staged 上限从 30 改为 50 MiB；让 ingest、CLI、channel、材料化共享同一限制 |
| `src/opensquilla/gateway/uploads.py` | `_DEFAULT_MAX_FILE_BYTES` 改 50 MiB；避免入口仍拒绝大文件 |
| `src/opensquilla/gateway/config.py` | 现有 `opaque_max_bytes` 默认改 52,428,800；不新增配置 |
| `src/opensquilla/gateway/native_attachments.py` | 格式预检使用与 UploadStore/ingest 一致的 staged 判定；不能直接无条件 `staged=True` |
| `opensquilla-webui/src/composables/chat/useChatAttachments.ts` | 四项普通前端 cap 同步；保持 inline/email/image 分支 |
| `desktop/electron/src/native-attachments.ts` | 普通副本最大值同步；保持图片、email 及完整性检查 |

同步 `docs/configuration.md` 默认值与规格文案。`gateway/app.py` 已继承 UploadStore 默认，不新增 wiring；不需要修改公开协议 shape 或重新设计生成合同。

不能批量替换所有“30”：输出 artifact、workbench preview、audio transcription、image clipboard、base64 草稿防御上限不是本次普通文件上传 cap。正常 50 MiB staged File 以 Blob 保存，不走无 File 的 base64 恢复分支。

验收必须跨过 HTTP 200：上传 UUID → 发送 admission → transcript material → workspace 工具路径 → 历史恢复。测试 fixture 中显式 30 MiB 要与“生产默认 50”区分；保留显式低上限的回归。

## 5. PR B：显式本机路径入口

优先级：P1，独立交付。风险：低到中；不改变现有 native-import 协议，重点验证本机所有权及异步结果不写入错误聊天。

### 开发任务

- `ChatComposerAddMenu.vue` / `ChatComposer.vue` 增加独立菜单动作与透传；`ChatView.vue` 通过现有 `appendComposerText` 插入路径，保留 main 的焦点保护。
- `PlatformFilesApi`、Desktop adapter、bridge 类型、`preload.cts`、main 增加窄 picker API。请求不接收 renderer 任意路径；只返回系统 picker 选择并经 canonical/regular-file 检查的路径和必要元数据。
- 复用可信 UI、ready/owned child、instance、profile、nonce 保护；nonce/凭据不出 main。窗口导航、销毁或连接变化取消操作。不要依赖 `localhost` 或拼接主机路径猜测执行环境。
- 此 API 不使用必须已有 durable session 的 `NativeAttachmentContext`。首条消息可有普通草稿，不调用 session.create，不制造临时 sessionId/epoch。
- picker 前捕获 sessionKey、会话意图、delivery identity/连接 generation、workspace/agent、run mode、composerRevision 及操作 generation；返回后逐一比较。任何变化丢弃迟到结果，不拼入另一聊天、不自动发送、不转上传。
- 新动作不读取源文件内容、不生成内容 hash、不预览或自动展开内容；空文件可给路径，目录/设备/链接替换失败。沿用路径长度与控制字符限制。
- Windows 中文、空格、引号、反引号、`#` 等以消息中的路径字符串准确表达；不创建未经支持的 `@` 语法，不将路径拼接成 shell 命令。超出 composer 长度应明确拒绝，不能截断路径。

普通路径沿用现有文本 draft/send/queue/retry/history/fork。这些文字没有文件 capability；草稿目前按 sessionKey 保存，不带文件来源主机证明。因此重开/换 host 后不保证路径有效，也不自动识别或重绑定到“同一文件”。异步防串会话不等于持久路径具备跨主机身份。

## 6. PR C：已知 live 引用及草稿计费修正

优先级：P1，独立正确性修复，不依赖 PR D。改动集中于 `useChatAttachments.ts`、`attachmentDrafts.ts` 及其测试。

- 汇总当前附件时，已返回 `kind=workspace` 的引用不按源文件大小占用60MiB内容额度；仍计入16个结构化文件上限。
- 尚未分类的 v1 selection 沿用现有保守检查，不为了这一小修提前读取 workspace 身份、增 RPC 或放开未知文件额度。因此 C 不能单独让80MiB文件通过旧 native picker，也不能消除“只剩少量额度时未分类大live被提前拒绝”的旧边界；这由 D 处理。
- 保留现有 in-flight 发送保护、取消、occurrence/local id；本 PR 不引入新 preparing kind、native token 或 fingerprint 持久化。
- `size` 保留用于显示和字段校验，不一律改为0。Live 原文件 size 可能变化，工具每次以实际文件为准。
- `attachmentDrafts.ts` 用一个统一的 payload-byte 计算函数：合法 workspace ref 为0内容字节，Blob按真实大小，其余沿用原保守口径。restore、save、consume、全局额度聚合都使用它。
- 全局聚合不能继续盲信旧 `record.bytes`；对旧记录按实际类型重算，保留结构校验、revision、scope、TTL和单事务跨标签额度。混有 Blob 的异常记录不能靠伪造 workspaceFile 获得免费字节。
- 不需要 IndexedDB schema 升级或全量历史迁移；元数据数量/字段长度保持有界。不新建配额系统。
- 只有通过字段长度、控制字符及相对路径校验的 workspace 引用免计内容字节。全局聚合发现损坏旧记录时明确失败并中止事务，保留记录，不以“修复计费”为由静默删除用户草稿。

## 7. 所有 PR 必须保持的合同

- `workspaceFiles` 仍只能是 workspaceId + canonical relativePath，不能塞绝对路径或 `../`。
- 读取时使用当前工具权限/敏感路径/实际执行后端；选择文件不是读授权，更不是写授权。403/409之后不能自动上传绕开。
- live 文件修改后读新内容，删除/移动则不可用；历史 envelope 不重写成当前内容。fork 的 live 引用仍指相同源文件，不自动生成独立副本。
- 文本路径没有结构化引用的自动失效标识/预览承诺。
- 上传附件仍是 immutable original + 会话 working copy；经上传材料路径编辑不是编辑用户原始文件。保留 reload/fork 的独立 working-copy 行为。
- 不把 live refs 加入 retained attachment manifest，不改变 main 新增的历史容量计量。模型不会因为收到文件引用就获得整个文件全文。

## 8. 验收矩阵：实现后必须逐项取证

按工作包执行，不以 D 的后置目标阻止 A/B/C 交付；每项交付只声明已通过对应门槛的能力。

| 编号 | 用例 | 通过证据 |
| --- | --- | --- |
| A1 | 默认48/50 MiB，50 MiB+1；四类普通 staged | HTTP/Native接受边界与拒绝一致；默认构造器/app factory覆盖，不只测自定义store |
| A2 | 50+10 MiB、再多1字节；16/17个结构化文件 | 保留60 MiB与16个限制；计数包含workspace refs；客户端组合超额在发起stage前阻止；绕过客户端的超额组合在send/admission拒绝，不能把stage成功当消息接受 |
| A3 | image5 MiB、email/inline2 MB、strict mode、显式低opaque cap | 旧限制及配置仍生效；live访问与副本MIME策略分开断言 |
| A4 | 大文件上传→UUID→发送→材料化→历史→reload/fork | 不向模型注入全文，原材料/工作副本合同保持；UUID重启后原TTL内可消费 |
| B1 | 新路径入口选择80MiB文件、空常规文件 | 零内容读取/hash/上传/副本；只有本机路径输入，无附件成功或已授权的假提示 |
| B2 | workspace内/外 × 首条/已有session | 新路径动作只插文字、零session.create；实际授权工具可读外部文件；拒绝时无扩权/上传 |
| B3 | 自有Gateway/远端/localhost隧道/Windows→WSL或容器 | 所有权与工具可达性分别检查；不猜映射，失败信息明确 |
| D1 | 旧结构化入口80MiB workspace文件；未知扩展文本/二进制 | 元数据准备零源内容整读/hash；live两次probe，不变四次；副本MIME及完整性保持 |
| D2 | v2两次请求间换session/epoch/workspace/root/origin/owner；文件替换、删除、junction/symlink | fingerprint/probe/identity失败；没有COPY_REQUIRED错误回退、copy→live漂移或stage副作用 |
| D3 | 过期/重放/并发兑付/新ID延长expiry/旧Gateway响应 | 单次性及原TTL有效；401/403/409不触发字节上传；缺SHA snapshot被拒绝 |
| 共用1 | 原附件图片输入及材料化 | 像素、5MiB限制、模型能力路由与capacity回归通过；B仅插路径不替代图片附件 |
| B4 / 共用3 | native picker未返回时取消、发送、切会话/项目/agent/runMode/Gateway/profile、窗口销毁；同文件移除再添加 | 旧结果不回填、旧额度释放、不会写到新草稿；新路径动作不覆盖已变化的composer |
| C1 | 已知live引用+真上传组合，16/17个结构化文件；未分类v1 selection | 只扣副本字节，数量限制保持；未分类仍保守校验，不借此放宽副本 |
| C2 | 真实IndexedDB保存/恢复/部分consume/旧record.bytes/并发标签页，live+Blob混合 | 已知大live不虚占60/120MiB；真正副本仍计费；无原生token持久化；无需数据库升级；大元数据用直接存储fixture，不假称v1 picker能选入 |
| 共用2 | live修改/删除/历史/fork/compaction，上传working-copy编辑/reload/fork | 旧历史不被重写；live当前语义及上传副本独立性均保持 |
| B5 | 普通路径草稿重开/队列/重试/换host，Windows特殊文件名及超长路径 | 只恢复文字不恢复权限；无错误自动映射、截断或命令执行 |
| N1 | Windows独立profile的实际packaged应用，48/50MiB、本机picker、真实File拖拽、首条/后续、受控远端 | 记录具体构建SHA、环境、耗时、峰值内存及通过/失败；Electron fixture通过不能代替packaged证据 |

现有小文本 native 测试只断言 UploadStore 为空，不能证明未整读/hash；需要对内容读取函数加 spy。元数据 HMAC/fingerprint 允许使用摘要算法，不得用“任何 SHA 调用都禁止”的错误断言。

上传HTTP接口不知道之后同轮会组合哪些UUID，不能要求它预先执行60MiB消息总量检查；上传池预算保持独立。当前后端在解析UUID并可能写入材料后才校验组合总量，本轮不为此新增事务上传协议。

## 9. 实施步骤、测试命令与交付物

实施应从已核实的 origin/main 基线开始，保留工作区其他改动，不在旧 HEAD 上实现后假称 main 已验证。提交划分建议：

1. 基线准备：刷新main，记录SHA和dirty状态，保留本计划及无关改动；实施使用最新main为基线的适用工作区，不重置当前工作树。
2. PR A：先补边界与strict-mode测试，再六处上限/预检及文档；跑端到端材料化与Windows大文件验收。
3. PR B：先测可信picker和迟到结果，再接菜单/platform/preload/main；测试真实首条发送及外部路径工具读权限；不改native-import。
4. PR C：先构造已知live、混合Blob和旧record.bytes测试，再统一payload计费；扩展现有真实IndexedDB fixture，不引入新DB依赖。
5. A/B/C分别review、分别可交付；交叉文件整合后重跑组合测试。每PR记录实际SHA、改动清单、命令结果、Windows证据、已知限制；失败门槛不能用“未复现”替代通过。
6. PR D先完成第10节的基线与go/no-go，再决定启动协议实现；若启动，私有v2、分类前预算和全部竞态/性能门槛作为完整优化交付，不能只删hash。

现有测试与 package scripts 已在 main 核对存在。以下为验证入口，实际运行结果见第 13 节；依赖安装仅使用本 checkout 的锁文件，已向用户说明，不借真实用户 profile。Windows pytest 使用独立、较短的 TEMP basetemp，避免测试夹具路径自身超过 MAX_PATH。

```powershell
uv run --no-sync python -m pytest -q tests/test_contracts/test_attachment_policy.py tests/test_gateway/test_uploads_endpoint.py tests/test_gateway/test_native_attachment_import.py tests/test_gateway/test_attachment_ingest.py tests/test_attachment_workspace.py tests/test_cli/test_chat_file_command.py tests/test_channels/test_channel_attachment_metadata.py

uv run --no-sync python -m pytest -q tests/test_workspace_files.py tests/test_gateway/test_gateway_workspace_files.py tests/test_engine/test_attachment_replay_ownership.py tests/test_engine/test_attachment_capacity_readmission.py tests/test_session/test_attachment_working_fork.py tests/test_gateway/test_transcript_attachment_persistence.py tests/functional/test_gateway_non_image_attachment_materialization_e2e.py

npm --prefix opensquilla-webui run test:unit -- src/composables/chat/useChatAttachments.test.ts src/composables/chat/useChatAttachments.native.test.ts src/composables/chat/useAttachmentDraftPersistence.test.ts src/composables/chat/useChatSend.attachments.test.ts src/platform/native-attachments.test.ts src/utils/chat/attachments.test.ts src/adapters/gateway/privateArtifactHttpTransport.test.ts

npm --prefix opensquilla-webui run typecheck
npm --prefix desktop/electron run test:native-attachments
npm --prefix desktop/electron run test:attachment-drafts
```

新路径动作新增小的controller/broker单测及菜单可访问性/i18n测试，加入对应suite。真实IndexedDB优先扩展现有 `desktop/electron/scripts/test-attachment-drafts-electron.mjs`；不为这一需求引入另一套数据库测试依赖。上述两个Electron scripts使用隔离fixture，不等于packaged产品测试。

## 10. PR D：后置元数据优化及进入条件

### 为什么独立

现有workspace附件本来就不上传源内容、不写副本，也不将全文放入模型；浪费发生在准备时Electron两次整读/hash、Gateway再整读/hash。D省去的是约3N应用层读量和3N内容哈希输入，不是固定3N峰值内存或物理磁盘I/O。

它不影响B的本机原位能力，却要将每文件native请求由1次变2次。workspace准备probe应由旧2次保持为每阶段1次、总2次；外部副本则从1次增加为2次且不省整读。Windows Safe probe会启动runner/worker、参与共享ACL执行锁，上下文还含同步SQLite和路径访问。因此风险为中等，不承诺所有附件更快。

### D0：先取基线，再决定是否实现

在独立Windows profile及同构建配置下，以ABC完成后的版本对比D候选；不触碰真实用户profile，不新增常驻监控或worker池：

- Full/Safe × workspace内/外 × 首次/重复选择；小文件、30MiB和A后的48/50MiB。80MiB作为新能力验证，不能与当前提前拒绝的路径伪作提速对比。
- 分段记录dialog确认→selection返回→import结束；主进程/Gateway内存峰值、内容读取/hash次数、HTTP请求数、helper启动次数及ACL等待。
- 无附件时新进程首次启动/同配置重启：进程启动→Gateway ready→可用聊天；同时确认没有新增启动探测、setup或worker。对同环境重复样本报告中位数及尾延迟，不以一次启动比较得出结论，也不将新进程启动冒称为操作系统冷缓存测试。
- 单文件/10文件批量、正常Safe命令并行、SQLite写入、慢路径及取消时，记录轻量RPC/心跳延迟、超时、断连/误重连、残留worker与资源回收。

Go条件：确有原附件入口大文件/内存/延迟需求，或明确需要该入口突破上传门槛；基线已定位成本，且不需要扩大为沙箱/会话/DB架构改造。若B已满足需求、收益不足或副本路径代价不可接受，保留D为后置项，不阻塞A/B/C。

### D1：获准实施时的技术边界

1. 同一私有native-import endpoint新增严格v2及独立签名域，区分reference/snapshot，v1与图片原链保留；不改公开chat/history schema。
2. select先canonical/stat，经Gateway权威session/epoch/owner/root及实际工具probe分类。合法inside返回ref；只有合法outside/正常unbound且权限通过才返回成功COPY_REQUIRED；异常或403/409不是回退许可。
3. Gateway单边生成binding fingerprint，覆盖实际context/owner/file identity；main仅缓存/echo，不跨语言重算，不下发nonce或持久授权。
4. live不整读/hash；snapshot在select返回前沿用bounded full-read/hash/MIME sniff。未知扩展live只用保守hint，不能用空Buffer误判文本。
5. import使用新单次请求ID但不延长原120秒期限。live重验只接受ref，不能转副本；snapshot复读/hash并核对binding后永远stage，不能转live。已有会话切换/失败清理保持。
6. inside每阶段仅一次实际probe，复用validate自带检查，不再在外层重复probe；最终send admission重新验权保留。
7. selection返回workspace/snapshot类型后，前端同步预留副本额度再import；未知pending只占数量，不持久化bytes/token/fingerprint。并发intake不能重复使用剩余额度。
8. 请求失败不得自动降为v1/上传；旧Gateway可能403/409，提示匹配版本，不另建协商系统。取消/超时须测后台回收，不能只丢弃UI结果；已有40/2048限制不是并发限流，不直接扩大。

### D2：合入门槛

- 第8节D1–D3及共用回归全部通过；权限、snapshot内容完整性、TTL和绑定不放宽。
- live整读/内容hash为0；准备probe=2，不能=4；snapshot无少验完整性，额外固定成本有量化结果。
- 无新增启动文件I/O、权限探测、setup调用、常驻进程或readiness依赖；实际启动无超出同环境基线波动的可重复退化。
- 小文件/副本路径不出现新的稳定超时或用户可感知卡顿；并发文件操作不引发Gateway心跳误断连/重启、worker泄漏或持续资源增长。
- 不预填无实测依据的毫秒、内存或提速百分比。D0报告中锁定目标环境的容许波动和测量方法，再以同方法验收；不能事后放宽标准掩盖退化。

## 11. 共用风险、回退与范围控制

- 50 MiB在多层存在完整buffer、sniff、base64中间副本；上传池300 MiB不是并发请求内存上限。记录主进程/Gateway/renderer耗时与峰值内存，测试连续上传及取消后资源释放；不得以“改常量所以无性能风险”结案。
- 普通WebUI上传目前15秒、native30秒、CLI60秒。50 MiB在15秒内仅传输就需约3.33 MiB/s。验证要记录受控链路条件；不承诺任意慢网络成功。若稳定撞到15秒门槛，只调整attachment upload的超时并补取消/重试测试，不全局延长HTTP超时。
- 不预先重构流式上传、分块续传、全局并发调度或自动压缩；若实测证明现缓冲路径不可接受，应补最小定向修复，不能带已证实的资源故障发布。
- 不新增文件授权数据库、跨host路径映射、外部引用持久schema、全局状态机、workspace权限放宽、全文自动注入或富编辑器。
- “原位可引用”只证明路径输入/结构化引用可建立；“实际可读”须工具证据；“可完整解析”须解析器证据；“能原位编辑”须单独写权限与源文件变化证据。

回退按独立PR执行，不新增运行时feature-flag系统：

- A：可回退新增接收上限，但不能声称整包降级后仍可重放所有已接收的50MiB材料；发布前必须覆盖已存大附件、暂存UUID、未发送草稿及队列重试。若旧版本不能消费，则保留大文件读取兼容、仅收紧新上传入口；已接收数据不删除。
- B：撤回入口/bridge接线即可，已发送路径仍是消息中的路径输入；不删除聊天或改权限。
- C：数据库格式不变，可独立回退逻辑，但旧算法可能重新误计大live额度；优先保留读取兼容，验证既有草稿仍可展示/移除，保留记录、不清空草稿。
- D：先退客户端的新v2使用，确认无v2在途后再退服务端；保留v1兼容，不在运行中将拒绝静默转上传。公共workspaceFiles及材料格式不变，既有引用不转副本；恢复旧native准备限制需明确告知。

实现与验证记录集中在本文件第 13 节。未测量的启动性能、远端链路及 Safe 并发稳定性不得由源码或单元测试结果替代。

## 12. 参考依据

参考取舍来自此前已核读的本地 reference，本轮重新核对 HEAD 和关键源码；不将本地快照当作最新版或闭源桌面完整行为。本方案采用行为边界，不移植这些项目的权限强度或整套输入架构。

| Reference 本地版本 | 借鉴与不能误读的地方 | 源码定位 |
| --- | --- | --- |
| DeepSeek `d347e703908d` | 路径文本与上传分离；chip不必成为新durable协议；补全限workspace不等于文件工具只能读workspace | `packages/client/ui-reference/src/client/index.ts:77`、`:112`；`packages/fs/fs-local/src/index.ts:58` |
| Hermes `463292351fec` | 区分客户端文件与执行端路径；workspace外附件实际上复制，不是外部live范例 | `tui_gateway/prompt_attachments.py:136`；`apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts:89` |
| Codex开源TUI `1b1835f751ebd` | 非图片选中路径插入文本；不据此推断闭源Desktop所有附件内部实现 | `codex-rs/tui/src/bottom_pane/chat_composer.rs:2774` |
| Pi `c1449660c83f` | 交互补全路径与CLI启动 `@file` 整读注入是不同流程；不照搬其无内置权限模型 | `packages/tui/src/autocomplete.ts:446`；`packages/coding-agent/src/cli/file-processor.ts:74` |

OpenSquilla关键依据均以第1节SHA为准：`workspace_files.py:118`（实际工具probe）、`gateway/native_attachments.py:188`（绑定快照及整读时序）、`desktop/electron/src/native-attachments.ts:156`（提前整读/hash）、`useChatAttachments.ts:208`及`:249`（分类前配额）、`attachmentDrafts.ts:76/157/176/214`（四个计费位置）、`engine/runtime.py:5146/5219`（live图片及历史）、`tools/builtin/filesystem.py:442/465`（原件和working copy）；另有`gateway/app.py:920`（注册不触发文件probe）、`sandbox/backend/windows_default.py:184`和`windows_default_runner.py:346`（Safe worker及ACL锁）、`sandbox/policy_store.py:157`（同步policy读取）。这些是取证定位，不要求修改全部这些模块。

计划结论：A/B/C 已按上述范围实施并通过本轮定向及本机打包验收；D 不增加协议、worker 或启动探测，保留现有 v1 校验。第 13 节区分已通过的测试和仍待验证的发布门槛。

## 13. 2026-09-30 实施记录

### Windows JPG 导入 409

- 已复现跨运行时身份不一致：同一 NTFS 文件的 Node/libuv `stat.dev` 为 `0x24ab720b`，Python 3.12/3.13 `st_dev` 为 `0x2624aba024ab720b`。原实现直接比较，合法文件也被判为替换并返回 409。
- 修复仅在 Windows 的 Desktop 签名元数据比较中使用 Python 卷标识低 32 位；Python 内部 before/open/after/path 比较仍保留完整设备号与 inode，不放宽文件替换检测。
- 新建聊天首次发送前没有 durable session，原附件入口走普通内容路径，不经过该 native 校验；已有聊天才进入 native 导入。因此用户“新聊天不复现”与此缺陷一致，不是 Desktop 目录本身具有不同待遇。
- Electron 对已知图片扩展名按文件头校正 PNG/JPEG/GIF/WebP MIME；例如 PNG 内容命名为 `.jpg` 不再在 native 入口被错当 JPEG。仍由 Gateway 严格解码；损坏图片不降为普通文件，5 MiB 上限不变。
- native 错误返回固定、可辨识的安全错误码；不把本机路径、nonce、capability 或原始异常回传 UI，403/409 不触发自动上传回退。
- 原报错 JPG 尚未提供；以上是真实运行时的可复现缺陷及修复，不等于对该原始文件和官方签名 0.5.5 的完整复测。

### 已实现的工作包

| 工作包 | 实际结果 | 保留边界 |
| --- | --- | --- |
| A | 普通 staged 默认 50 MiB；后端、前端和 Desktop 一致，配置文档同步 | 图片 5 MiB、email/inline 2 MB、总量 60 MiB、结构化数量 16、显式低 cap 不变 |
| B | 主进程可信 picker + 菜单 + 本机路径输入；首条/后续、workspace 内外；同步捕获输入及连接 generation 防迟到回填 | 仅 owned/ready Desktop Gateway；不授权工具，不映射 WSL/容器，不创建 session，不读/hash/上传源内容 |
| B2 | Desktop 原生非图片拖拽先确认 owned-child binding，再由 preload 解析路径并直接插入本机路径输入；图片、浏览器/合成 File 继续原附件路径；不创建 pending attachment | 不把 `localhost` 当作远端可达性证明；外部/非 owned Gateway、无原生路径或路径解析失败时不插入本机路径；真实 Windows 鼠标拖放仍需 packaged 验收 |
| C | 已知 live 源大小不扣内容额度；草稿 save/restore/consume/global scan 统一计算，Blob 始终计费；旧 bytes 重算 | 数量、TTL、revision 和单事务不变；损坏记录保留并报错；未分类 v1 selection 仍保守 |
| D | No-go：本轮不做 v2 协议，B 已提供大文件不搬运的显式入口 | 原附件 v1 仍整读/hash并受单文件门槛；未声称完成 D0 全矩阵或任何启动提速 |

### 验证记录

- Backend 输入/上传/合同/CLI/channel：189 passed，5 skipped。
- Native Windows 运行时与拒绝路径：56 passed，1 skipped；包含真实 Node/Electron 元数据进入 Python ASGI，不只用 Python 构造身份。
- 50 MiB working-copy 编辑、fork、reload 及原件保全：17 passed。
- 最终历史/workspace/ownership/capacity/transcript/materialization/fork 联合回归：192 passed，6 skipped，104.92 秒。5 条 warning 为既有 sklearn 1.8.0 序列化模型在 1.9.1 加载的版本提示，不是附件测试失败。
- WebUI 11 个附件/草稿/发送/菜单/路径相关文件：最终 446 passed、完整 typecheck 通过。C 最终覆盖新保存拒绝和旧 `size=0 + workspaceFile.extra Blob` 记录拒绝且保留。
- B2 定向 WebUI 单元：路径解析/图片回退/16 个结构化附件边界共 53 passed；全量 WebUI 单元当前 7,996 passed。
- Electron native broker：21 passed，2 skipped；真实 Electron File/native fixture 和真实 IndexedDB fixture 使用独立 profile 通过。受限沙箱中 Electron 子进程崩溃，放行隔离进程后通过，不能把最初环境失败隐藏为通过。
- 本机路径 broker：7 passed；controller/menu/platform：31 passed。
- UI：仓库 Playwright Chromium 7 passed，1280×720 / 390×844；真实渲染 + 模拟 Desktop bridge/Gateway。验证 URL/title、非空页面、无框架错误覆盖、console error 为 0；新聊天/已有聊天路径回填、reload、编辑/切会话迟到丢弃、browser/外部 loopback 隐藏。Browser plugin not available，使用项目已有 Playwright。
- WebUI production build/typecheck、冻结 Gateway 构建及 `verify:prepared` / `verify:package` 全部通过；独立 Windows packaged 候选交互 10/10 checks 通过，详见下方。
- 暂未完成：原报错 JPG、官方签名安装版；真实受控远端/慢网络、Safe 并发/WSL 可达性、同配置重复启动及内存回归基准。已有单次采样不代替这些门槛。真实 File 对象的拖放入口有 Electron fixture 证据，但未自动操作 Windows 桌面鼠标拖放；本轮 packaged 自动化走真实菜单/broker，仅将系统选择器返回值替换为合成文件，没有声称覆盖原视频全部鼠标拖放操作。
- 本轮新增的 Playwright 拖拽用例未在当前机器完成：默认 18791 端口已有服务，切换临时端口后页面并行启动阶段未稳定挂载 composer；因此不把该用例标成通过，不能替代 packaged/真实鼠标拖放验收。

本地打包候选：Windows 11 x64（10.0.26200）、Electron 42.11.4、Python 3.12.13；基线 `c4b1dd8368e5ed2050147219370e9e6d6a0bbfdc` 加本分支未提交修改。`electron-builder --dir --publish never --config.win.signExecutable=false`，未签名、不安装、不发布。

- EXE：`dist/desktop-electron/win-unpacked/OpenSquilla.exe`，SHA256 `ff007cc1246dc7b1b1cb0bc5431d07fdd6da5d2e162c4f468cc7d30514502caa`。
- Desktop 代码包：`resources/app.asar`，SHA256 `6b2fc374ca129ecf820dc140f53a057c056d6c746f277eb2a409e4b49360f959`。
- 冻结来源/输出清单：包内 `resources/runtime/gateway/gateway-build.json`，SHA256 `1030932e8c261391f21d30148b6aea798dc102e420f30a6adbf811d9ef4cc2c9`，逐文件绑定本地源码与产物；不是把基线 SHA 当成未提交改动的身份。
- 验收使用全新 TEMP profile、合成文件及 loopback fake Ollama，清除继承的 provider secret 环境字段。未读取或修改实际用户 profile。

### 独立 Windows 打包交互验收

最终记录：`<temp>/opensquilla-jpg-409-<run-id>/packaged-attachment-<id>/report.json`，`ok=true`，进程 exit 0。运行时为真实 Electron 42.11.4 / Node 24.19.0 → 冻结 Python 3.12.13，而不是模拟 native HTTP 回包。

| 场景 | 结果 |
| --- | --- |
| 首条消息前选择空文件、80 MiB 文件、中文/空格/反引号/# 路径 | 本机路径输入回填；无结构化附件，无 session.create、无上传 |
| 已有会话：真实 JPEG、PNG 内容但 `.jpg` 后缀 | native-import 均 HTTP 200，MIME 分别为 image/jpeg / image/png，无 byte-upload 回退 |
| 已有会话：48/50 MiB 普通文件 | native-import 均 HTTP 200；50 MiB 实际发送成功 |
| 50 MiB 发送→材料化→历史→页面重载 | 历史 size=52,428,800、retained ref 存在；模型仅收材料路径及有界元数据，未收二进制全文 |
| 50 MiB 源/工作区材料/retained copy | 三者 size 与流式 SHA256 一致，SHA256=`8565a714dca840f8652c5bae9249ab05f5fb5a4f9f13fbe23304b10f68252da2`；源未变 |
| 图片 5 MiB+1、普通文件 50 MiB+1 | UI 拒绝，无新 native-import 或 byte-upload |
| Full Access 下 workspace 外路径 | 合成 provider 仅发出一次真实 read_file 调用，返回测试文件标记；无上传、源未修改。没有证明 Safe 模式或其他执行文件系统也自动可读 |
| 退出及错误检查 | 测试客户端/Gateway 正常退出；无未预期 renderer 错误。两条 Playwright 注入 sandbox 日志被既有 QA 分类器识别，不宣传整个日志绝对为零 |

最终本机 HTTP native-import 段观测：JPEG 62 ms、改后缀 PNG 63 ms、48 MiB 147 ms、50 MiB 168 ms。它们**不含**系统 picker、人为等待和 Electron 准备阶段，文件为合成/稀疏样本，不能代表远端网络或普遍性能。

本轮新进程/新 profile 启动到首次 connected 8.610 秒；每 200 ms 采样的 Electron 各进程工作集之和峰值 707,194,880 bytes（约 674 MiB），Gateway 工作集峰值 574,996,480 bytes（约 548 MiB，ready 后开始采样）。工作集求和可能重复计入共享页，不等同于独占物理内存，也不是 OS 绝对峰值。没有同构旧版 A/B 对照或冷缓存控制，不能得出“启动提速/无内存回归”。50 MiB 路径仍有完整 buffer，保留第 11 节资源风险。

前两轮 TEMP 自动化的失败证据保留：第一轮错误地用全局 workspace_dir 寻找任务材料；第二轮试图点击正在自动消失的 toast。修正测试脚本后第三轮全过，未因此改产品代码或放宽验收条件。截图在同一目录 `packaged-50mib-history.png`；原始合成资料及报告不写入仓库。

### D0 方向性观测及 No-go 决策

Windows x64 / Node 24，实际写满 48/50 MiB，3 次中位数：本机路径 broker 分别 1.173/1.092 ms，3 次 lstat + 2 次 realpath，零源内容 open/read/hash/fetch；旧 native select + mock import 分别 69.128/69.909 ms，2 次整读、2 次内容 hash，应用层读量分别 96/100 MiB，ArrayBuffer 粗略峰增亦为 96/100 MiB。

这只证明两条输入路径的成本差异：mock fetch，没有 Python Gateway/Safe/packaged/UI，OS 缓存未受控，也没有同构 D 候选。不能解释为 Gateway 峰值或 D 提速百分比，更不能替代第 10 节的启动/并发门槛。当前 B 满足显式大文件原位路径输入，D 缺少值得增加两阶段协议的必要性，故本轮 No-go，保留 v1。若后续要求“原拖拽入口也要零整读且超过 50 MiB”，再按第 10 节测量与立项。

## 14. 本分支 B2 实施方案：新聊天 Desktop 拖拽原位输入

本节覆盖当前用户问题，优先级高于第 2 节中“新会话原附件保留副本”的现状描述；它不把“没有 durable session”解释成技术上不能原位处理。

### 14.1 本分支交付目标

- Desktop 自有本机 Gateway 下，用户在新聊天首次发送前从 Windows 文件管理器拖入普通非图片文件时，不自动读取、hash、上传或创建 durable session。
- 拖拽文件异步解析为本机路径输入，直接复用 B 的“引用本机路径（不上传）”语义；消息中传递路径字符串，不建立 pending attachment 状态。
- workspace 内外均可表达本机路径；workspace 外不伪装成 `workspaceFile`，而是非结构化路径引用。结构化附件仍按 16 个计数，路径输入不计入该数量。
- 图片、剪贴板 Blob、浏览器 File、无法取得原生文件路径的拖拽，以及远端/跨文件系统执行环境继续走内容附件或现有副本策略。
- 这条 V1 路径不承诺结构化附件 chip、历史 `workspaceFile`、自动失效检测或视觉输入；它解决的是 local agent 原位读写而不是附件预览。

### 14.2 技术实现边界

1. **拖拽捕获**：`ChatView.onChatDrop` 不再一律直接调用 `addAttachments`。Desktop 原生 File 先确认非 secret 的 Desktop-owned Gateway binding（这是进程归属，不是 session/workspace binding），再交给 preload 的 `webUtils.getPathForFile(file)` 做路径解析；该调用只取得拖拽对象对应的本机路径，不读取内容。浏览器、粘贴和合成 `File` 没有原生路径时保持原分支。
2. **路径输入**：原生非图片文件复用 `appendComposerText`/现有路径草稿，不调用 `native-import`，不需要 `NativeAttachmentContext`，不调用 `session.create`。应明确提示“消息中传递本机路径，未上传；实际可读性由执行环境和现有工具权限决定”。
3. **发送时序**：不尝试在首条消息 accept 后再回填结构化附件；当前 `prepareAttachments → turn acceptance → materialize session` 顺序不支持这样做。B2 只提交路径文本，因此无需新增公开 chat wire 或 durable attachment schema。
4. **执行环境分类**：只在本机执行端能够使用该路径时提供该行为。Gateway URL 是 loopback 不是充分条件；remote、SSH、Docker、WSL/Windows→POSIX、容器和未知 backend 继续使用副本或明确失败。
5. **图片边界**：图片拖拽始终保留 bytes/预览/vision 路由；本机图片路径文本不能替代模型需要的像素输入。

### 14.3 代码工作包

| 工作包 | 主要位置 | 内容 |
| --- | --- | --- |
| B2-1 | `opensquilla-webui/src/views/ChatView.vue`、`useLocalPathPicker.ts` | 区分 Desktop 原生拖拽、浏览器 File、粘贴 Blob；native non-image 直接异步追加路径，其他保持原附件路径 |
| B2-2 | `desktop/electron/src/preload.cts`、`platform/types.ts` | 增加窄的 File→native-path 调用；不复用会整读/hash 的 `selectAttachmentFile`，不接受 renderer 任意路径字符串 |
| B2-3 | 草稿/发送链 | 只复用本机路径输入的取消、切聊天和重试语义；不增加 pending attachment、Blob 配额或持久化状态 |
| B2-4 | 单元、Electron、Playwright/Windows packaged | 覆盖首条/后续、workspace 内外、80 MiB、图片、浏览器 File、远端/WSL/容器和拖拽取消竞态 |

### 14.4 验收门槛

- 新聊天首次拖入 80 MiB 普通文件：无 `session.create`、无 upload、无源内容 read/hash；发送后消息包含路径输入。
- 一次拖拽最多追加 10 个路径文件，单次追加及 composer 总文本仍受 100,000 字符限制；超限整批拒绝，不截断路径。该 10 是本机路径输入入口限制，不是结构化附件 16 个名额。
- Full Access 本机执行环境能对原文件执行一次 read（需要写入时另测写权限）；源文件未被复制到 workspace。
- workspace 内外均能表达；workspace 外不会生成 `workspaceFile` 或 staged copy。
- 图片仍走 bytes；浏览器 File/粘贴 Blob 仍走现有附件路径。
- Gateway 为远端、SSH 隧道、WSL、Docker 或未知 backend 时，不把 Windows 本机路径当作可达路径。
- 发送中切换聊天、Gateway 重启、取消拖拽或文件被替换时，不把旧 pending 输入写入新消息。

### 14.5 不在本分支追加的工作

如果未来必须让首条消息拥有结构化 `workspaceFile`/external-live attachment，必须另立协议工作：在 `turn_acceptance` 中携带 pending token 并由 Gateway 原子创建 session、绑定和解析，或新增私有 prepare/bind 两阶段接口。不能简单在 accept 后调用现有 `native-import`，也不能伪造 `sessionId/epoch`。这属于后续 P2，不阻塞 B2。
