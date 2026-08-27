# 上下文水位治理方案

> 版本 v1.0 · 2026-08-24 · 针对 webchat:h8m7rtg1（SystemError 撞墙）与 webchat:7bf7fefe（running 卡死）两起事故的统一修复方案

---

## 一、问题定义

两个会话、两种死法、同一个病理。

| | h8m7rtg1（8/22） | 7bf7fefe（8/24） |
|---|---|---|
| 体量 | 单轮输入 178 万 token | 历史 64 万字符，累计输入 3147 万 |
| 死法 | **急性**：超过链上最大窗口（100 万），provider 拒收 | **慢性**：窗口内但巨大，外网中转首 token 延迟分钟级，HTTP 超时掐断回合 |
| 表象 | `SystemError: request is too large` 连报两次 | 状态卡 `running`，新消息排队，催不动 |

**统一病理：会话历史只增不减，且增长过程中没有任何刹车机制。** 窗口小的链路急性炸，窗口大但有中转的慢性瘫。长任务会话的终点必然是不可用，区别只是死法。

### 三个放大器（均有代码实证）

1. **历史 tool_result 全量内联回放**
   `tool_result_projection` 只压缩本轮新产生的结果（`agent.py` 的 `_tool_result_projection_for_provider`，约 5499 行）；从库回放的老结果原文照搬。两会话 60 万+ 字符的主体都是这个。
2. **fork 全量继承**
   会话链 `(2)→(3)→(4)`，每次 fork 把全部历史背走，肥胖代际遗传。
3. **压缩只在撞墙后触发一次**
   自动压缩是事后抢救（`max_overflow_retries=1`），且当「受保护的近期尾巴 + 系统提示词」本身超过最小可用窗口时数学上不可能成功（拒绝原因：`provider_request_budget_exhausted` / `recent_tail_too_large` / `compaction_not_smaller`，agent.py 3171-3181）。

---

## 二、方案：三层治水 + 两个兜底

核心思路：**把"事后抢救"变成"事前治水"。**

### P1 · 水位告警 + 自动压缩（最先落地，改动最小）

每轮结束检查 `turn_usage.input_tokens`（数据已入库，纯现成）：

| 水位 | 动作 |
|---|---|
| > 60% 窗口 | 轻量提示一次："上下文较大，建议 /compact 或新开会话" |
| > 80% 窗口 | 强提醒 + 自动调度 `sessions.contextCompact` |
| > 95% 窗口 | 压缩失败则阻断新消息，人话引导新开会话 |

> 为什么 80% 就自动压缩而不是 95%：压缩调用本身也要把历史塞进摘要模型的窗口，等到 95% 再压大概率压不动。

**改动点**：
- `src/opensquilla/engine/agent.py`：turn 结束后的 admission 阶段加水位检查（复用已入库的 turn_usage）。
- `src/opensquilla/gateway/rpc_sessions.py`：复用已有 `sessions.contextCompact`（9738 行），加自动触发入口。
- WebChat 客户端：渲染 warning 事件为非阻塞提示条。

**效果**：下次膨胀在 40 万 token 时就收到提醒，而不是 178 万时收到 SystemError。
**风险**：极低，纯观测 + 已有 RPC。

### P0 · 历史回放投影（治本，改动最大）

现状缺陷：投影只管本轮，历史全量内联。

**做法**：
- 构建发往 provider 的消息列表时，对**历史** tool_result 超过阈值（复用现有 `tool_result_projection_max_inline_chars` 配置）的，替换为「摘要 + handle」信封。
- handle 指向 message id；取回细节复用现成 `retrieve_tool_result` 基建。
- **原始数据在库里一字不丢**，只是不再每次请求都塞原文。
- 保护期：最近 K 轮（建议 3-6 条）不投影，保证 agent 对近期动作有完整上下文。

**改动点**：`agent.py` 历史消息序列化路径，新增 `historical_tool_result_projection()`，与现有 `_tool_result_projection_for_provider` 对称。
**效果预估**：7bf7fefe 这类会话投影后 prompt 降至几十分之一，178 万会话大概率压回 20 万内。
**风险**：动消息构建主路径，需回归测试护住（现有投影相关测试是基线）。

### P1.5 · fork 治理（防遗传）

- fork 时若源会话历史超过阈值，先警告体量，再执行。
- 提供「摘要 fork」选项：带 compact 后的摘要上下文，而不是全量历史。
- 挂点待定位（fork 实现处），实施时确认。

### P2 · 路由感知体量（体验补丁）

- `squilla_router.py` 决策输入加 `estimated_input_tokens`：超出某 tier 窗口就跳过该档。
- 超出链上最大窗口 → 前置拒绝 + 中文人话提示（"本会话已 X token，请新开会话"），不再等 provider 打回英文报错（该文案在 `session/terminal_reply.py:177`）。

### P3 · 僵尸 running 收割（兜底）

- 回合因 HTTP 超时/中断后，自动把会话标 `failed` 并释放队列，不让状态机吊在 `running`。
- 这条解决 7bf7fefe 那种"催也没反应"的体验黑洞。
- 挂点待定位（turn runner / session manager）。

---

## 三、实施顺序与验收

| 顺序 | 项 | 工作量 | 验收标准 |
|---|---|---|---|
| 1 | P1 水位告警 | 半天 | 构造/模拟超阈值会话 → 警告事件触发、自动 compact 被调 |
| 2 | P0 历史投影 | 1-2 天 | 现有回归全绿 + 新增测试：含大块历史 tool_result 的会话，provider payload 为信封而非全文 |
| 3 | P1.5 + P2 | 1 天 | fork 警告触发；超窗口路由前置拒绝出中文提示 |
| 4 | P3 收割 | 半天 | 模拟超时 → 会话不再卡 running |

**开发纪律**：全部在 main 上做；每个 P 项一个 commit，推 origin 备份；realfork/PR 等主人发话。

---

## 四、修复落地前的临时纪律（今天就能用）

1. **`/compact` 命令现在就存在**（`engine/commands.py:335`）——长任务会话感觉变慢/变贵时，主动敲一下，别等撞墙。
2. 长任务按子阶段切分会话，每完成一个阶段新开。
3. fork 前问一句：真需要全部历史吗？
4. 已死的会话（h8m7rtg1、7bf7fefe）不要再发消息，留着当档案即可——数据都在库里，随时可查。

---

## 五、一句话

**修一处水位治理，两个症状一起消：急性撞墙（P0+P2 拦）、慢性卡死（P0 减负 + P3 收割）、反复发胖（P1 预警 + P1.5 防遗传）。**
