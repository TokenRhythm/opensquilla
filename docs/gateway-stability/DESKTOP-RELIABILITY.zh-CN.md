# Desktop 本地连接诊断与验收

目标是验证实际 Electron 页面与其拥有的 Gateway，在配置保存、重启和异常恢复后仍能读取历史、完成发送并正常退出。单独的 `/readyz` 成功、源码测试通过或外层 EXE 哈希，都不足以证明整个客户端可用。

## 隔离原生场景

使用与源码匹配的 Windows 解包目录，先完成 `verify:prepared` 和 `verify:package`。以下命令从仓库根目录执行；相对路径会按当前目录解析，`--workdir` 必须尚不存在，每次运行使用新目录，报告也写入新文件。

```powershell
node desktop/electron/scripts/test-packaged-gateway-reliability.mjs `
  --executable C:\audit\candidate\OpenSquilla.exe `
  --workdir C:\audit\restart-01 `
  --output C:\audit\restart-01.json `
  --scenario restart --disable-gpu --startup-timing
```

常用场景：

| 场景 | 检查的路径 |
|---|---|
| `configuration` | WebUI 保存 Provider 并热应用，保持原 Gateway、原页面，随后向新 endpoint 发送 |
| `fresh-onboarding` | 真实原生向导保存，记录实际 Gateway 是否替换，验证保存后的原主页面恢复及首次发送 |
| `restart` | 点击现有 Restart 控件，核对旧 child 退出、新 child 身份、原页面重连和历史 |
| `history-streaming-restart` | 通过 UI 形成长历史，流式输出中重新进入会话、读取多段快照，再在任务进行中重启 |
| `late-ready` | 合成 MCP 故意延迟 discovery，穿过真实前台等待期限后，检查原 child 迟到就绪能否接回 |

测试独立设置 userData、state、home 和环境，使用 loopback 合成 Provider/MCP，不需要真实凭据，也不运行安装器。`fresh-onboarding` 仅将测试 endpoint 放入向导已有的隐藏配置字段，保存动作仍走真实表单。长历史 fixture 显式声明较大的模型上下文；这是避免测到无关的重复输出或上下文容量错误，不是生产模型配置建议。

报告要求真实 renderer 协商 flow/recovery、同一用户提交不重复调用 Provider、目标会话成功读取、原页面没有被测试主动 reload，以及全部本次拥有的进程退出。先检查报告的 `ok` 与 `cleanup.verified`，再解释阶段耗时。上述五种场景都不注入 flow 编码或预算故障，因此 `ok` 还要求 `gatewayFlowFailures.available` 为 true 且 `failures` 为 0；后续连接恢复不能掩盖已记录的故障。旧版 harness 报告没有这项成功门槛，应结合 `harnessSha256` 单独核对该计数。失败报告中的 fixture 错误不能当作产品反例。`--disable-gpu` 只影响此次测试，结果不能推广为默认 GPU 配置已验证。

`history-streaming-restart` 中的多段快照在重启前完成安装；它不证明“快照传输中断后恢复”。持久历史由 `chat.history` 读取，活动 turn 快照由 `sessions.messages.snapshot.read` 读取，两者不能混称。

## 普通小 profile 的重复性能对照

固定版本的实测、失败记录和结论边界见 [2026-09-29 Windows 性能对照](PERFORMANCE-2026-09-29.zh-CN.md)。

`restart` 可显式增加 `--repeat-profile main` 或 `--repeat-profile candidate`，分别使用本仓库 `.cache/perf-ordinary-profile/main`、`candidate` 下的合成 profile。首次还需 `--initialize-repeat-profile`，且对应目录必须不存在。每次 `--workdir` 必须是 `.cache/perf-ordinary-runs/` 下的新目录，`--output` 必须在该次目录内。固定端口由首次合成 Provider 分配并记录；复用时不改配置或凭据，端口占用即失败，不换端点继续测量。

先为每版独立运行一次初始化、一次预热，随后至少 **10 对交错样本**，按 main→candidate、candidate→main 交替顺序串行执行。初始化和预热不计入正式样本。例如首次准备 main：

```powershell
node desktop/electron/scripts/test-packaged-gateway-reliability.mjs `
  --executable .cache/perf-main-source/dist/desktop-electron/win-unpacked/OpenSquilla.exe `
  --workdir .cache/perf-ordinary-runs/main-prepare `
  --output .cache/perf-ordinary-runs/main-prepare/report.json `
  --scenario restart --disable-gpu `
  --repeat-profile main --initialize-repeat-profile
```

后续去掉初始化参数、使用新 run 目录；candidate 换用当前 `dist/desktop-electron/win-unpacked/OpenSquilla.exe` 和对应 variant。两套 profile 各自迁移，避免在同一数据库上来回升降级。正式样本要求本轮两个 Gateway 的迁移计数都为零；小库的 schema/index 首次准备也应留在初始化与预热中，不能据此声称验证过大型历史库的全部布局。

`performance` 分别记录进程启动→页面已连接、→输入框可编辑，实际 Restart 点击→新实例连接、→输入恢复，重启后的首条发送→回答可见、→任务完成，以及侧栏点击→目标历史读取且显示成功。计时采用宿主单调时钟，包含 Playwright 调度和 100ms 轮询误差；此处的首条发送发生在重启之后。新连接须具有新的 descriptor instanceId 和实际 WebSocket Hello，旧连接外观不能满足重启测量。

每轮通过 UI 新增一个合成会话。`repeatProfile` 记录运行前后完成轮数及实际侧栏行数，并要求只增加一行。**配对时还必须检查两版的 `completedRunsBefore` 和 `sidebarRowsBefore` 相同**；数量不同、缺少阶段、配置变化、日志截断或任何业务/清理失败，都应停止批次并调查，不能仅剔除慢样本后继续。此方案是逐渐累积几十条历史的小 profile，不是每轮字节相同的数据库，也不代表真实模型、远程网络或冷磁盘启动。

脚本拒绝链接路径，以排他锁和已干净退出的合成标记控制复用，只用本轮日志后缀验证进程退出及 flow 故障。失败会保留锁和资料供检查，不自动删 profile 或绕过标记。执行前固定两版全部组件的构建来源；外层 EXE 哈希不等于 Gateway/WebUI 内容证明。两组采用同一 harness、GPU 和计时设置，并在没有其他构建或重负载时串行运行。性能批次关闭可选启动计时；开启计时的定位运行单独报告。结果保留全部样本、中位数、范围、每对 candidate−main 差值及改善对数。配对差值中位数不等于两组中位数之差；十对样本不能支持可靠 p95、产品失败率或普遍提速结论。

## 启动前段计时

启动进程环境中显式设置 `OPENSQUILLA_STARTUP_TIMING=1` 才会输出早期阶段计时。上述 harness 的 `--startup-timing` 会在隔离环境中设置它。默认关闭；profile 内 dotenv 不能迟到启用这个开关。

计时仅写本地 stderr，Desktop 将其收进本地 Gateway 日志，**不接 telemetry，也不需要上报后端适配**。事件名为 `gateway.startup_early`，内容仅包含固定阶段名、固定状态、PID 和数值时间；不加入配置、路径、参数、凭据、消息正文或异常文本。

阶段覆盖 frozen hook/CA 初始化、CLI 和 Gateway 导入、profile 锁、持锁检查、legacy 锁及进入 Gateway。按 PID 区分主 Gateway 与 helper；以墙钟关联 Desktop spawn/ready，用同一进程的单调时钟计算耗时。嵌套阶段不能相加。只有 start 没有 complete 表示没有到达成功边界，不能擅自把它归因为某个异常。

计时本身不是性能优化。保留同机、同包、同 profile 条件下开关关闭/开启的对照，并分别测量启动、历史读取、首条消息和退出，防止把初始化成本推迟到首次使用后称为提速。

## 单实例与生命周期边界

`test-packaged-single-instance.mjs` 使用同样的显式 `--executable`、新 `--workdir` 和新 `--output` 参数。`--scenario activation` 检查主窗口隐藏后二次打开是否聚焦同一窗口、保持同一 Gateway，并让第二进程自然退出；`--scenario relaunch` 检查退出立即重开。未实际碰到旧实例锁竞争的重开测试只能记为未覆盖该竞态。

签名安装升级、Windows 物理睡眠和原用户现场故障是独立验收项。组合测试、手动发出的 resume 事件及合成故障，不能替代这些结果。对 #1821，应保留“未复现”和“已修复”的区别；只有与原报告相关的根因和修复验证，才能支持关闭 issue。
