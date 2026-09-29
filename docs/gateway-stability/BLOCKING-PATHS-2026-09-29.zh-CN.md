# 客户端断线后续：阻塞入口与生命周期修补

本轮目标是让慢工具、慢磁盘和进程登记不再占住 Gateway 的事件循环，并保留取消、退出和连接代际约束。没有调整连接健康阈值、重试时长、数据库持久化级别，没有新增常驻服务、遥测或视觉 UI。

## 现场证据与归因边界

9 月 29 日 22:52 的 Windows 现场，两条连接分别记录约 140.5 秒和 136.2 秒的事件循环迟滞，随后断开；同窗口 `web_fetch` 总耗时约 161.7 秒。Gateway 进程身份延续，之后重新连接。它支持“共享事件循环长期不能及时处理连接”这一定位，但没有记录阻塞时的调用栈，不能证明这 140 秒全部由 DNS、HTML 解析或某一条 SQL 导致。

该用户实际运行的旧安装包仍含同步进程登记路径；不能用旧包再次断线，直接推断本分支修复失效。同样，源码测试通过不表示旧安装已经获得修复，也不能据此关闭 issue #1821。

## 修改和预期收益

| 入口 | 原有问题 | 修改 | 收益与限制 |
|---|---|---|---|
| 网页抓取、图片下载、技能包下载 | async 函数内同步解析 DNS；代理与 TLS 准备也可能同步等待 | 有界 worker 执行验证和连接参数准备，HTTP 请求仍异步；保留每跳验证和地址 pinning | 慢解析不再直接卡住所有连接；不保证外部服务变快 |
| 网页正文提取 | readability、html2text、文本转换直接在事件循环运行 | 复用上述 worker | 普通解析等待不再独占事件循环；不是对任意大正文或持 GIL 扩展的硬 CPU 隔离 |
| PTY、Windows 显式 stdin 启动 | 同步辅助进程等待和 owner SQLite 登记阻塞事件循环 | worker 启动，主循环接回完成监控；取消后等待真实启动结果并精确清理 | 启动遇慢磁盘时连接仍能响应；目标进程保持“先登记、后放行” |
| 进程完成与取消并发 | 先标记关闭、登记删除仍在进行，其他清理调用可能提前返回 | 同一 owner 共享一个删除 Future；POSIX EMPTY 后取消仍 RELEASE 并回收 anchor | 避免漏清理、迟到登记和退出期间失去进程责任 |
| 删除准备、reset 后内部 artifact 清理 | 部分文件检查、删除仍同步执行 | 文件检查移入只读 worker；复用已有提交后清理 worker | 慢文件系统不拖住连接；检查仍在原事务内，不缩短该事务持锁时间 |
| 连接 checking/suspect 时目录读取 | 被默认写请求门禁立即拒绝 | 使用现有最多 8 项、5 秒的读取等待队列，绑定连接 generation | 短暂探测恢复后可继续读取；不伪造健康，不跨连接误发旧请求 |
| checking/suspect 时已知任务 Stop | 与普通 mutation 一同拦截 | 仅精确 task/session、同 generation 的 `chat.abort` 单次直发，恢复期最多等 5 秒 | 可请求停止已知任务；不排队重放，不把超时当成功；未知提交的 Stop 流程保持 |
| usage identity 索引准备 | 实时预约依赖的索引排在两大历史索引之后，后者失败可长期留下扫描路径 | 将现有 `idx_sessions_id_key` 排第一 | 后续历史索引失败时实时查询仍可用索引；不是数据库长事务排队修复 |

## 资源和取消约束

fetch worker 最多 4 个实际工作线程，独立于 Gateway 默认 executor。同一个 loop 的名额在真实 worker 完成时释放，不能因为调用方取消便释放名额继续堆积任务。上下文随调用复制；取消或超时后，迟到 DNS 结果不会继续发 HTTP 或填充缓存。同步 SSRF API 保留，原有地址、代理、SNI、敏感 URL 规则保持。

系统 DNS 没有可安全强杀的 Python 线程取消接口。复查发现，直接使用 `ThreadPoolExecutor` 会在解释器退出时 join 未返回的 DNS，即使异步清理已完成仍可能延长退出。因此最终实现使用最多 4 个按需启动的私有 daemon worker 和公开 Future API，只允许可放弃的 DNS、解析和未联网 TLS 准备；没有使用标准库私有属性。取消后的只读工作可以随解释器结束，不绕过数据库、文件删除或进程回收的正常收尾。两个真实子进程测试覆盖 worker 永不返回、调用者取消/超时后进程自行正常退出。

这不保证强制停止单个系统调用或硬隔离持 GIL 的 C 扩展，也不能替代整个客户端验收。Desktop 的 `stopGateway` 有 75 秒硬终止阈值与后备清理；Quit 接受优雅关闭请求的分支先等待最多 80 秒，之后仍有强制清理等待，因此 75 秒不是所有退出流程的总上限。本轮没有缩短这些保护期限，不能据此声称正常退出、更新已普遍加速。

启动 worker 与只读 DNS worker 的取消语义不同：前者可能拥有新进程，必须等真实结果并完成精确清理；后者可放弃结果，但仍保留实际资源名额。已提交数据库删除后的材料清理也必须等待实际工作完成。没有把这三者合并成一个泛化生命周期框架。

## 验证

- Windows 真进程回归覆盖 PTY、stdin、慢登记、登记失败、取消、重复取消和 `asyncio.run` 退出；检查目标未放行、精确进程退出和登记清空。POSIX 协议模型覆盖 EMPTY 后取消、RELEASE 和回收，但本机不是 Linux/macOS 原生验收。
- 两条已握手的 `handle_ws_connection` 源码处理链在 DNS 被阻塞时仍收到各自 nonce pong；取消后 DNS 晚返回不发 HTTP。恢复同步 DNS 的负对照按预期失败。socket/HTTP 是测试替身，该测试不冒充真实 listener 或完整客户端。
- Web、图片、技能包原有 SSRF/重定向/代理测试与新增慢工作取消测试共同约束安全与响应性。
- reset/delete 的真实隔离数据库和材料测试覆盖提交前取消保留数据、提交后取消完成清理，以及相邻会话、新代次、链接和公开 artifact 的保留。
- 索引测试制造后续历史 DDL 失败，重开数据库后验证真实查询计划走 identity 索引、预约幂等、历史索引可继续完成。
- 前端相关 626 项测试、RPC 架构门禁、Vue 类型检查通过；完整 WebUI 构建和产物校验通过。

最终只读 worker 实现的网络相关回归分两组运行，分别 191、130 项通过；隔离解释器退出另 2 项通过。Gateway/材料清理/索引集成组 89 项通过、1 项平台跳过；数据库相关组 80 项通过。这些组存在部分交叉，不汇总为产品可靠性样本数。进程回归包含本机 Windows 真实 PTY/stdin，POSIX 协议追加修复后的相关组为 139 通过、16 跳过；平台跳过不能替代原生验收。

### Windows frozen 机制对照

用现有构建环境和 PyInstaller spec 在独立目录重建未签名 Gateway；构建前后 1567 个 Python 源文件摘要一致。收尾仅移除 worker 文件末尾空行，另验证最终源码编译的代码对象与包内模块一致。最终 EXE SHA-256 为 `31cd8e4902438ace04bfe2df4fc08817bc7e331b47f05a270a3392e29b6a6ca8`。没有覆盖用户安装或运行真实 profile。

两包使用同一探针，让一个指定虚构域名的 DNS 调用等待 500ms 后失败，不发真实网络请求；每包连续三次，观察同一事件循环的 10ms 定时器：

| 观测 | 修复前 frozen 包 | 最终 frozen 包 |
|---|---|---|
| 最长定时器间隔，各次范围 | 500.338–500.611ms | 16.417–22.938ms |
| DNS 所在线程 | 事件循环线程 | worker 线程 |
| 操作总耗时，各次范围 | 528.251–533.540ms | 529.675–543.899ms |

收益是等待不再阻塞事件循环，操作本身仍约 0.5 秒。它是有控制的机制复现，不是实际 DNS 故障复现、断线率统计或 140 秒现场根因证明。该探针没有 Gateway listener 或 Desktop UI。

最终 frozen EXE 另执行一次“worker 永久等待、调用者取消、`asyncio.run` 完成”探针，进程自行以 0 退出，包含启动/导入总计 4.272 秒，未调用强退。中间标准线程池机制的独立源码负对照在异步收尾后仍超过 5 秒不退出，因此没有交付这个中间方案。完整客户端重启/退出/安装更新仍有独立验收门槛。

退出单测最初包含整个 tools 包导入，在并行构建期间出现一次 5 秒超时；该失败保留在 `.cache/fetch-exit-final-tests.txt`。最终单测直接加载真实自包含 worker 文件，保留 5 秒预算和真实解释器退出要求；完整 frozen 模块导入由上述 EXE 探针补充，二者不混成同一种验收。

本地原始证据位于 `.cache/reliability-frozen-{baseline,candidate}-result.json`、`.cache/reliability-frozen-exit-result.json`、`.cache/reliability-final-build-inputs.json`、`.cache/reliability-final-code-equivalence.json`、`.cache/fetch-work-exit-comparison.md`。探针和独立工作目录也保留在 `.cache/`，不提交构建包和原始日志。

## 仍未关闭的边界

1. **大历史原子删除造成数据库排队**：已有 10 万条历史实验出现其他写入等待超过约 2 秒；索引顺序和 worker 隔离不消除此问题。删除已有 session 索引，同时需要维护 FTS、其他索引和提交。随意分批提交会改变原子删除、代际隔离及中断恢复契约，不能作为低风险顺手修复。
2. **现场 140 秒完整归因**：修复覆盖已复现的阻塞机制；仍需修复包在隔离客户端场景和实际复发窗口的证据。没有调用栈的旧日志不能倒推出唯一根因。
3. **普遍启动、重启、完整更新提速**：本轮目标是忙碌时可响应和可靠收尾，未据此承诺这些流程普遍变快。此前性能测量的边界见 [PERFORMANCE-2026-09-29.zh-CN.md](PERFORMANCE-2026-09-29.zh-CN.md)。
4. **非常大的网络正文**：HTTPX 缓冲/解压、部分结果整理仍有随输入增加的成本；本轮没有引入新的大小截断策略或进程级解析服务。

数据库下一步应单独验证长事务处理方案，而非扩大 2 秒预算、降低同步持久化、关闭 FTS 或拆分账本预约。需要保留单一原子删除契约，或先明确设计可恢复的逻辑删除协议及所有读取/写入的 owner 边界，再讨论分批物理回收。两者不能混在本轮连接修补中宣称已完成。

## 2026-09-30 主分支 review

已合入 `origin/main=c4b1dd836`，三路复核覆盖 Desktop/WebUI、runtime 阻塞与数据库。保留有生命周期依据的 Future、取消收尾和有界 worker，未为减少行数删除这些保护。修复一个遗漏：`web_fetch` 每个 HTTP hop 的 DNS、代理和 TLS 准备共享 30 秒等待预算，初始 SSRF 检查不变；新增初始/重定向的超时、取消回归。原实现两个超时负控制失败，修后相关 109 项通过。未修改工具结果成功/失败分类。

验证：合并后 Python 相关回归 837 passed、17 skipped、5 failed；五个失败均在主分支已有测试创建符号链接时出现 WinError 1314，独立进程复查相同，未改系统权限或把失败改成 skip。独立复查的前置失败还遗留了 SQLite 测试线程，记录结果后精确结束该测试进程，不算自然退出通过。WebUI 定向 249 项、Desktop lifecycle 与 single-instance 脚本通过；本 session Python 文件 Ruff 与 diff check 通过。小修后的 109 项是定向复验，不能与前述数量相加作为去重总数。证据在 `.cache/review-main-pytest-20260930.log`、`.cache/review-symlink-check-20260930.log`、`.cache/review-fetch-prep-{negative,final}.txt`。

未重新构建安装包；前文 frozen 结果属于前一轮源码。还保留三个明确边界：共享累计 ACK 窗口的阻塞未完全消除；原有 managed/显式代理或部分 IP literal 路径仍会在事件循环构造默认 HTTPX TLS transport；会话 reset 的同步归档与大事务另见 [数据库复核第 11 节](DATABASE-DIAGNOSIS.zh-CN.md#11-主分支复核大历史删除的触发条件与解法)。这些不是本轮发现的新增回归，也没有被声明已全部修复。

## 多会话现场复核（2026-09-30）

用户明确：触发方式是同时启动几个会话对话；Mac 明显比 Windows 稳定，两边均为 0.5.5。不能继续把人工构造的大删除竞争当作该现场的首因。普通 UI 新会话使用新 key 和 `new_chat`，只有明确 reset 意图才进入重置；cron 清理另有独立条件。长对话压缩可能重写历史，但 preflight/rebound 日志不等于执行了压缩删除。

再次只读核查：22:26–22:30 原 bundle 的 46 条日志中有 exec_command，无 delete/reset/prune/archive 的 event/method/operation；22:48–22:54 留存 incident 的 179 条日志中有五次发送、搜索和抓取，同样未发现这些删除事件。后者是筛选快照，日志缺席不能证明操作绝未发生，但没有证据据此归因。22:52 两连接迟滞 140532/136172ms、未确认帧 0/2、web_fetch 总耗时 161719ms，仍将共享事件循环阻塞/调度失时排在删除或 ACK 满窗之前。附近普通读和 usage 写入持锁变慢，不足以解释分钟级 loop lag。

再次只读解析现场旧 EXE 的 PYZ，SHA-256 仍为 `2a009891c2fcad0c92b99cbf2ebd5e279e72975e8ef8e0909c84f7b181a44c3c`：`_prepare_private_directory` 调用 Windows DACL 设置没有 skip 参数，其实现也没有重用已有私有目录权限的分支；async owner 登记仍同步执行。Windows 设置 DACL 可能向已有子对象传播可继承 ACE，见 [Microsoft API 说明](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo)。这在普通工具子进程登记时也可发生，不局限于 Gateway 启动；Mac 的本目录 chmod 不走同一传播路径。当前代码已有 `skip_if_private_directory=True`，本 session 另补 executor 隔离。审计保存在 `.cache/windows-v055-code-audit.json`。

这是确定的旧版 Windows 特有风险，但不是两平台稳定性差异的完整实证。22:52 主要相关工具是 web_fetch，不能据上述证据指定那次停顿由 ACL 导致。Windows Job/helper、ConPTY/stdin 与 Mac POSIX 路径不同，需要对齐实际工具模式；Defender、磁盘、DNS、系统暂停没有匹配现场证据，不能作为既定原因。HTTPX 正常 verify 路径显式使用 CA 文件，不能误称每次 web_fetch 都枚举 Windows 系统证书库；无 CA 覆盖的 frozen 启动 hook 才可能进入系统证书加载。

下一优先验收改为同机旧/新 frozen 包、隔离相同 profile 下的 1/4/8 会话，分别跑纯对话、命令、网页抓取，再混合。保留相同输入和 Provider stub；不人工制造删除，不把注入慢 DNS/磁盘的机制测试冒充自然故障。同步核对 loop lag、nonce pong、目录/Stop 响应、当前存储 holder、工具具体阶段、进程身份和退出。若自然重现停顿，再在隔离进程捕获停顿栈，区分同步调用、CPU/GIL 与系统调度。Mac 对照还需同源构建及对应平台证据；目前没有 Mac 现场包，不能给平台故障率或唯一根因。

已有 DNS/解析、进程登记、PTY/stdin 的隔离修改直接服务普通多会话稳定性，保留并首先做这组验收。大删除回收协议、reset 长事务及 ACK 专项保留为独立问题，不阻塞当前现场闭环，也不替代它。

## 压力复现结果（2026-09-30）

使用本地确定性 provider 和隔离 profile，旧现场 Windows 0.5.5 包与本分支候选包均跑了聊天、命令、抓取的 1/4/8 并发，并持续观察 WS nonce、目录 RPC 和 `/healthz`。新 profile 两包矩阵均完成。使用 139,763,712 字节的隔离大历史数据库快照（约 2,106 sessions、220,208 transcript rows）后，旧包在 8 个并发命令中出现一次 `Session storage is temporarily busy`；候选同一快照的 9 组全部完成，8 并发 Stop 约 21.7 ms 返回且仅目标任务取消。候选存储诊断最高事务约 320 ms、其中排队约 314 ms，没有 loop-lag 记录。该结果证明旧包存在可复现的并发存储失败边界，也显示本 session 修改有收益；它不是大历史删除事务或真实现场百秒断线的唯一根因证明。

候选包另在完整 Gateway 内注入一次 5 秒 DNS 等待。等待位于 `fetch-work-0`，另一个运行中的会话在等待期间 Stop 约 25.3 ms 返回并正确取消；这是受控机制复现。旧包独立离线探针则显示 DNS/direct TLS/managed-proxy TLS 均会令主循环约 5 秒不调度；候选已隔离前两者，但 managed-proxy 的默认 TLS 构造仍是主线程遗漏路径。完整原始结果和边界见 [多会话压力结果](../../reports/gateway-scale-lab/concurrent-conversations-20260930-results.zh-CN.md)。

同一 225 万文件 ACL 目录的候选对照约 1.0 ms 完成，事件循环最大 tick 间隔约 31.6 ms，目录身份与 ACL 摘要未变；旧包则在 `set_protected_dacl` 连续采样停留。它支持权限复用与异步隔离的具体收益，但仍属于合成机制实验。

随后补上了候选包发现的 managed-proxy Transport 构造遗漏：`_web_fetch_httpx_client_kwargs` 已在现有 fetch worker 中构造显式 `AsyncHTTPTransport`，事件循环只接收已构造的 transport。源码和 fresh frozen 的 DNS、direct TLS、managed-proxy 5 秒探针最大 tick 间隔约 20–40 ms；31 项 managed-network 定向测试、Ruff、大历史 8 并发抓取及 Stop 通过。该修复没有改变代理/证书/SSRF/重定向语义；完整 Desktop 包和真实代理证书环境仍需验收。
