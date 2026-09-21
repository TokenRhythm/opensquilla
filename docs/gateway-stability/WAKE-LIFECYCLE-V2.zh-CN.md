# Gateway Wake 生命周期与用户体验改进

本文记录当前实现相对于 `origin/main` 的可复核变更和测试边界。基线为
`10e9a4adba2e8d71491599016d6274c7298b6f7e`；PR #1780 的历史合入 SHA 为
`fcf0ecf8185dc32aae2d22168015caf8991032ac`，历史数据不计入本次通过数。

## 行为

一次 wake incident 由第一次有效 wake 信号创建，并固定 20 秒截止时间。普通导航产生的 `pageshow.persisted=false` 以及首个 Hello 前的 browser lifecycle 信号属于初始启动，不创建 incident；BFCache `pageshow.persisted=true`、`online`、Electron `desktop-resume` 和手动信号只增加信号计数，不能延长截止时间。

- `checking`：incident 创建后立即发布。UI 不再把仍在确认的连接显示为正常 connected。
- `suspect`：5 秒内没有当前 generation 的 nonce pong 或 Gateway `tick` 时发布。旧 socket 保留，mutation 直接失败；草稿和 session 页面保持不变。
- `reconnecting`：20 秒仍未得到当前 generation 的有效响应时退役旧 socket。旧 generation 的 response、pong 和 event 不能恢复健康。
- `healthy`：当前 generation 的有效 nonce、tick、Hello 或 RPC response 完成恢复。13 秒受控停顿仍可在 20 秒截止前自愈。

Electron resume 走 2 秒 nonce probe。成功沿用旧 generation；失败立即退役旧 generation 并建立新连接，同时保留同一个 incident 的截止时间。未确认的 Goal、steer、chat send 等 mutation 不会自动重放；已发送但未确认的请求继续以 `accepted=null` 的传输错误交给调用方处理。

明确标记为 safe-read 的读取请求在恢复期间最多排队 8 条，单条最多等待 5 秒；当前 session history、hydrate、snapshot 读取已显式标注，订阅、snapshot release、Goal 和发送操作仍按 mutation 处理。默认请求属于 mutation，checking/suspect/reconnecting 阶段不会写入旧 socket。双 WebSocket warm reconnect 未启用。

Gateway writer 保留已有 2 秒 direct-send、30 秒 recovery-credit、60 秒 queued-writer 和 512 槽边界。本次只增加 `queue_oldest_age_ms`、`last_inbound_age_ms`、`last_outbound_age_ms`、`probe_wait_age_ms` 和有限枚举 `writer_starvation_reason` 诊断，不改变 writer 调度或协议。

## 诊断字段

每个 RPC transport 记录 generation、incident ID、incident source、incident deadline、transport phase、probe timeout、suspect 时间、close reason 和 first successful RPC。Desktop bridge 传递 `desktop-resume` 来源；旧 preload 的 `power-monitor` 入参会在 renderer 边界归一化。

## 验证结果

- WebUI 全量 Vitest：513 个测试文件、7757 个测试通过。
- Wake/RPC 状态机和诊断清洗：181 个定向测试通过；新增覆盖初始 `pageshow`、native resume 的 CONNECTING/无 socket 路径和 close reason 保留。
- Gateway writer/close/flow/diagnostics 定向 pytest：87 个通过。
- Electron TypeScript：`npx tsc --noEmit -p desktop/electron/tsconfig.json` 通过。
- Electron source build：`npm run build` 通过。
- 静态 Gateway artifact、`OPENSQUILLA_TESTING` 环境下的 `history-hydration @session-hang-recovery` 通过；同一 loopback 场景本地连续 30/30 通过。
- Desktop background-flow harness 已更新以接受新 phase、source 和 generation 在同一 incident 内轮换；本环境执行时需要下载 Electron 二进制，下载未完成，因此没有把它冒充成 packaged/native 通过。

完整 `tests/test_gateway` 收集到一个环境阻塞：`test_rpc_selflearning_status.py` 需要未安装的 `numpy`；这不是本次改动产生的失败。远程 relay、真实 Windows 睡眠/唤醒和 packaged EXE 的 30 次循环仍需在具备 Electron 缓存、远程拓扑和物理睡眠能力的 runner 上执行，不能由 loopback 或 fake clock 代替。

## 用户可见改善

唤醒后半秒内会进入 checking，5 秒左右无响应会显示连接不稳定并暂停新的发送，用户不会继续把请求写入半开黑洞。正常唤醒只需一次 nonce/tick 即恢复，不会强制换 socket；真实失效时最多约 20 秒回收旧连接，Electron resume 的失败路径在约 2 秒开始重连。草稿、session 和未确认 mutation 的结果边界保持可见，不会静默重复发送。
