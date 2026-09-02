关联 PR：#1407（上下文水位治理）。两个 PR 联动解决同一问题域：#1407 治"历史怎么长胖"，本 PR 堵"账本准不准 + 压缩会不会伤人"。

## 问题

实测事故：一条 assistant 条目自身可回放内容仅约 22K token，持久化 `token_count` 却被记成 770,191（整轮累计输出被抄进了单条记录）。由这条错误记录触发了一串连锁：

1. **写端记账口径错误**——条目持久化时回退到 `done_event.output_tokens`（整轮累计），而非条目自身可回放体量
2. **读端无条件信任账面**——119 条归档对账：真实 169K 字符 ≈ 4 万 token，账面 165 万 token
3. **压缩 LLM 输出预算守卫掐断**——流式输出超部署 max_output 直接判失败（#921）
4. **兜底整体替换旧摘要**——LLM 全灭后 deterministic_fallback 用 68 token 存根替换 170K 检查点，全程静默

每个环节单独不致命，这次全中。同型事故至少 4 起（8/24、8/26）。

## 方案：四层守卫

### 1. 写端记账口径（`turn_finalizer_stage.py`）

`message_output_tokens` 保持最高权威。回退分支不再无脑抄 `output_tokens`：累计值超过条目自身回放体量 4× 时判为多腿虚高、改信条目回放估算；否则取两者较大值保精度。`message_output_tokens` 行为不变。

### 2. 读端估算器：单值选择（`compaction.py`）

`estimate_entry_replay_tokens` 和 `estimate_entry_model_replay_tokens` 统一走 `_reconciled_persisted_token_count`：在持久化消息级计数与条目自身投影估算之间做单值选择（`max(persisted, natural_projection)`）——两个值永不叠加，消除"封顶值+extras 重复计数"问题。历史存量虚高行留给一次性修正脚本，写端 B1 已防复发。

### 3. 兜底降级：逐字幸存守卫（`compaction.py`）

纯确定性 fallback 的 `rolling_summary` 本就通过 `_merge_rolling_fallback` 包含旧摘要全文。提交前校验旧摘要是否逐字幸存于合并产物中：幸存则提交，否则拒绝（`fallback_degraded_with_prior_summary`）。短结构化摘要与长摘要同权。LLM 失败导致的降级写 warning。

### 4. 事故链时序回溯

事故根因：写端虚高记账 → 预检压缩被账面提前触发 → kept 条目虚高 token_count 把摘要适配预算压到零 → 旧检查点被裁剪到 68 token 并落库。修复后：写端防复发，读端不叠加，兜底保留旧摘要——三线同时生效。

## 验证

- 守卫回归 9 例：写端精度（正常单腿 / 多腿虚高各 1 例）、读端单值选择（不叠加 2 例）、兜底逐字幸存（富摘要 / 短摘要 / 超窗拒绝 3 例）、降级 warning（1 例）、短摘要同权保护（1 例）
- 受影响族全覆盖：compaction + provider runtime + turn_runner + t3 + preflight = **599 passed**
- ruff 零告警
- 全量 sweep 中 3 个失败（dispatcher 边界 / delete_session 清理 / storage busy 计时）均已用 worktree 在纯 upstream/main 基线复跑证实为存量问题，与本次改动无关
- 跨平台 CI 因 fork PR workflow 审批门未触发，可在贵方流水线执行

## 与 #1407 的分工

| 层面 | #1407 | 本 PR |
|---|---|---|
| 关注点 | 历史怎么长胖 | 账本准不准 + 压缩会不会伤人 |
| 手段 | 工具结果水位投影 + 会话水位告警 | 写端记账修复 + 读端单值选择 + 兜底逐字幸存 + 降级可见 |
| 互补关系 | 无准确账本，告警阈值失真 | 无记账守卫，压缩过程可能摧毁数据 |