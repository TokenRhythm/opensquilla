# Gateway 恢复验收记录

记录日期：2026-09-21。集成基线为 `c31abea3184938ff6877f4e5b92092138755922b`。本记录针对下列源码哈希，不把历史 675、8c7 或 v0.5.4 的实验当作最终源码验收。复现命令见 [README](README.md)。

当前完成：最终候选与 c31 对照的 Vue 代理浏览器真实时钟矩阵 300 次、writer 真实时钟矩阵 210 次、Windows source Electron wake 复测，以及下述自动化检查。**真实睡眠、物理远程网络和 packaged Windows 的 30 次业务循环仍未验证。**

## 源码与证据来源

浏览器实验开始于 `2026-09-20T23:50:53.298Z`，结束于 `2026-09-20T23:58:03.527Z`；记录的 HEAD 为 `534eeed7c13a752bf098f7819c5ac840d33c1a9e`，包含随后提交为 `b139b7d87` 的未提交修复。writer 实验开始于 `2026-09-20T23:40:55.660247Z`，记录的 HEAD 同为 `534eeed7c13a752bf098f7819c5ac840d33c1a9e`。因此以运行快照的 SHA256 识别实现，不仅以 HEAD 识别。

浏览器快照与当前 `rpc.ts` 的唯一差异是将数值字面量 `20_000` 写为 `20000`，语义相同；逐文件比较已确认。writer 运行期间所记录的全部源码未变化；当前 `websocket.py`、`transport_flow.py` 和 `uv.lock` 与运行快照一致。

| 文件或运行输入 | SHA256 |
|---|---|
| 当前 `opensquilla-webui/src/lib/rpc.ts` | `fcd5715bdae8e1995706330d0ae6966b91f256e648ef7aef3041c848abcb0a80` |
| 浏览器运行 `source-candidate20.ts` | `997b407e0b9be060118b6f1f8bdce37ef1b8c8890e5af8fc2e2d3c84d688af0e` |
| 浏览器运行 `source-baselinec31abea3.ts` | `bc757343ca6a3905d9b692a72d027ab06c2f17ccf55c4d72d85c00ef6a4c5938` |
| `src/opensquilla/gateway/websocket.py` | `a92ac34aaf8d9a8ca06b8f6d768ddb1bf750eb59ac77a421030c28766072033b` |
| `src/opensquilla/gateway/transport_flow.py` | `6301a7ee050c839d3da21cd2210c7c44c733d583443ac08c430a78bcdac496cd` |
| `opensquilla-webui/package-lock.json` | `4367ec128a5f962e44b5fcb5933b28549686f46186a2f348c6972feb9fc3104f` |
| `uv.lock` | `718db1e630fb14df0d7e5a2f74de3037a82b613d878b09ed913181c668e33783` |
| `scripts/gateway_wake_real_clock.mjs` | `cce60af0316afe822cf1a7dd772d6a3084d08c9764ec8af6b11476878005cef7` |
| `scripts/gateway_writer_real_clock.py` | `d1c3b2dd432fb576ebf0952bc6e31d0d958877b9e9eb58e415132f3b05b13201` |
| `scripts/verify_gateway_wake_real_clock.mjs` | `9434156a80d320093c1d56709825c4e76383e61d0147597d4eb8da736c19a7ca` |

原始数据保存在执行机器的系统临时目录，未复制进 Git。以下目录名作为本次运行标识；临时目录不是公开或永久下载地址。对外移交时应另行归档并公布归档校验值和取回方式。

| 运行标识和文件 | SHA256 |
|---|---|
| `opensquilla-gateway-wake-fzn6wT/results.json` | `261e78ab4f5d22ecacd26167c4c4579d1d7ecb949881cd3ae95d08c895e2f1ce` |
| `opensquilla-gateway-verify-gI2LvC/verification.json` | `6a2fe4458add953c59443e68c272b477e945af888eabdeba1753794eb1176e87` |
| `opensquilla-gateway-writer-cskf5q5f/manifest.json` | `4bb4ba617f5391612636fce7d64968e520f65a311cbd5f525f801d7a7639d8e5` |
| `opensquilla-gateway-writer-cskf5q5f/results.json` | `30ea8ed4b46b8b8f5406bda3cabe40045d1a8de148c72af14e93ad9447835e61` |
| `gateway-wake-native-diagnostic-2.log` | `823b436b442475e0d35fde6ae63603fe00f815e53ecb7b068e52ad5fd5776a03` |

## 浏览器真实时钟矩阵

Windows 上 Node `24.15.0`、Chromium `153.0.8010.12`，每格 30 次，最多并发 30 个隔离浏览器上下文。两版均经实际 Vue `ref()` 代理 `RpcClient`，通过代理调用 `notifyResume()`，使用原生 WebSocket 和 loopback TCP 字节中继；上游只实现握手、nonce pong 和合成 echo RPC。黑洞只影响旧连接，新连接可用。

下面的恢复耗时从故障注入算到首次成功 echo RPC，单位为秒。p50 使用排序后第 16 个样本，p95 使用第 29 个样本。独立 verifier 重读逐次 JSON、检查源码哈希，并重算所有统计；300 次无夹具失败，harness 在运行期间未改变。基线的 90 次观察期内未恢复属于测试结果，不能记为恢复成功。

| 场景 | n | 恢复 | 保留原连接 | p50 | p95 | 最大值 | 最小值 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c31 单次 wake，旧连接黑洞 | 30 | 30 | 0 | 47.4431 | 47.5718 | 47.6576 | 47.2549 |
| 修复版单次 wake，旧连接黑洞 | 30 | 30 | 0 | 20.4167 | 20.5288 | 20.5612 | 20.2788 |
| c31 每 3/14/40 秒重复 wake（每格） | 30 | 0 | 0 | >90 | >90 | >90 | >90 |
| 修复版每 3 秒重复 wake | 30 | 30 | 0 | 20.3965 | 20.4953 | 20.5078 | 20.2800 |
| 修复版每 14 秒重复 wake | 30 | 30 | 0 | 20.3859 | 20.5681 | 20.5843 | 20.2867 |
| 修复版每 40 秒重复 wake | 30 | 30 | 0 | 20.4135 | 20.4904 | 20.5099 | 20.2773 |
| c31 TCP 缓冲 13 秒后释放 | 30 | 30 | 30 | 13.0753 | 13.0959 | 13.0960 | 13.0666 |
| 修复版 TCP 缓冲 13 秒后释放 | 30 | 30 | 30 | 13.0695 | 13.0790 | 13.0825 | 13.0581 |

修复版四组黑洞均以 `wake_incident_timeout` 退役。单次 wake 的 close 耗时 p50/p95/最大值为 `20.0101/20.0159/20.0163` 秒；重复 3/14/40 秒的 close p50 分别为 `20.0087/20.0081/20.0068` 秒。单次 wake 的 `probe_timeout` 指标 p50 为 `15.9772` 秒，不能把 20 秒总预算理解为 UI 一直保持健康的时间。

单次黑洞 p50 减少 `27.0264` 秒，即 `56.97%`。重复信号的基线结果仅支持“90 秒观察期内未恢复”，不是无限等待时长的实测。修复版 40 秒组在首次重复信号到达前已恢复，因此验证的是及时退役，不是跨过 40 秒后继续去重。13 秒恢复是脚本指定的停顿，不是自然恢复概率，也没有在这个浏览器实验中启用生产 Gateway flow control。

## Windows source Electron

最初的最终源码 native 检查发现实际缺陷：store 的 Vue `ref()` 会代理 incident，新增的原始对象引用检查误判，deadline 回调提前返回，未回收旧连接。两处检查改为 incident ID、incident generation 和 socket generation，保留观察者重入保护。新增 Vue 测试在修复前失败，修复后 RPC/store/连接集成共 187 项通过。

重建 WebUI 后，Electron `38.8.6` 与真实 Python Gateway 的 source 测试通过：约 `15.938` 秒观测到 suspect UI，`20.011` 秒触发 deadline，`20.413` 秒观测到替代连接成功 RPC。7 次 resume 共用同一 incident，只有 1 条替代连接，renderer、草稿、窗口和 socket ownership 检查通过，正常退出耗时 `2.368` 秒。这里只运行了 1 次修复后 native 场景，不能计算有意义的 p95。

故障通过 Playwright application-frame routing 注入，resume 通过 Electron powerMonitor 事件发出；它没有执行 Windows 物理睡眠，也不是 packaged EXE。早期 raw-client 浏览器 150 次通过仍漏掉了 Vue 代理缺陷，所以该批 `pon8CL` 不再作为最终前端验收。最终表格采用补齐 Vue 代理后的独立 300 次矩阵。

## Writer 真实时钟矩阵

Python `3.12.13`，Windows 11 build `26200`，实际生产 `WsConnection` 和 flow 代码，socket 为可响应取消的 `asyncio.Event` 应用夹具；每格 30 次，最大并发 96。日志经 Gateway 隐私处理桥接至 `NullHandler`，无日志文件 I/O。此实验没有真实 TCP/kernel 背压。

单位为秒。超时组测到夹具观察到 close 的时间；停顿组测到第一帧送出的时间，不是完整 RPC 或 UI 恢复时间。p50 取中间两样本平均值，p95 取第 29 个样本。单调时钟为 `GetTickCount64()`，分辨率 `15.625 ms`；因此 `12.985` 不表示能精确提前 `15 ms` 恢复。

| 场景 | n | 通过 | p50 | p95 | 最大值 | close reason |
|---|---:|---:|---:|---:|---:|---|
| direct send 超时 | 30 | 30 | 2.000 | 2.015 | 2.015 | `direct_send_timeout` |
| recovery credit 超时 | 30 | 30 | 30.000 | 30.000 | 30.000 | `recovery_credit_timeout` |
| queued writer send 超时 | 30 | 30 | 60.000 | 60.000 | 60.000 | `writer_send_failed` |
| flow ON，停顿 5 秒 | 30 | 30 | 5.000 | 5.000 | 5.000 | 无关闭 |
| flow ON，停顿 13 秒 | 30 | 30 | 12.985 | 12.985 | 12.985 | 无关闭 |
| flow ON，停顿 20 秒 | 30 | 30 | 20.000 | 20.000 | 20.000 | 无关闭 |
| flow ON，停顿 30 秒 | 30 | 30 | 29.985 | 30.000 | 30.000 | 无关闭 |

210 份逐次 JSON 的 SHA256 均与汇总一致。每次清理后连接预算均为 0；并发时剩余全局预算均等于其他活跃连接的预算。整批结束后全局 outbound budget、writer task 和 close task 数量均为 0。10 ms 间隔采样得到的峰值为 writer task `96`、close task `30`、全局预算 `11,840,898 bytes`；采样值不是连续观测的严格峰值。

这里确认了 2/30/60 秒分别作为资源保护边界的行为，不能用它们证明用户等待体验良好，也不能把它们相加为 92 秒。用户可见的状态变化、请求快速失败和重连由 RPC 状态机另行管理。

## 自动化检查与未完成项

以下套件结果由本次集成测试记录提供，与上面两组独立计时实验分开列出：

| 检查 | 结果 | 边界 |
|---|---|---|
| WebUI Vitest | 7,689 通过，510 个文件 | 单元/组件覆盖 |
| WebUI architecture、类型检查、构建 | 通过 | 源码和构建检查 |
| 浏览器 recovery/Goal/steer/hydration E2E | 44 通过 | 浏览器夹具业务路径，非 packaged 30 次循环 |
| Python Gateway 广泛本地测试 | 后一轮 5,512 通过、22 跳过、2 失败；此前已排除 4 项环境失败 | 两轮合计 5 项环境失败在 c31 main 同现；另 1 项旧测试 double 已修复并在 148 项专项中通过。不能写为本机全绿 |
| 后端独立专项 | 148 通过 | 连接稳定、关闭协调、flow 等专项 |
| Vue 代理修复后的 RPC/store/连接集成 | 187 通过，Vue 类型检查与构建通过 | 补测最后两处身份保护及重入 |
| source Electron wake | 通过 1 次 | 时间线和限制见上文 |
| c31 同条件浏览器对照 | 300 次完成并独立复算 | 使用 c31 源码，不以历史 675 数字替代 |

20 秒仍是候选预算。历史参数探索的受控 13 秒停顿中，15 秒已能保留连接；这些旧源码结果只能用于选择后续实验，不能单独证明 20 秒是生产最优值。并发样本共享同一主机，本表 p95 是夹具内统计，不能推广为真实用户网络分布。

仍需完成：Windows 真实睡眠/唤醒；实际远程、Wi-Fi/VPN/NIC 的故障和恢复；packaged Windows 的至少 30 次 Goal lease、session hydration、snapshot/replay/steer 与 mutation exactly-once 循环。source Electron 手动发出的 resume 信号、浏览器业务 E2E 和应用 writer 夹具不能替代这些验收。
