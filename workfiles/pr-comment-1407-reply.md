收到了，感谢逐条核验——特别是用合成数据复现了 500/1000 报 800 的双计问题。四个点全部处理完毕，逐条回复：

---

### ① checkpoint / recoverable reference 字符数双计

**已修复，且确认了确切机制。** 核对代码后定位：调用方计算 `durable_history_tokens = checkpoint_tokens + sum(transcript 估算)` 时已包含 checkpoint 投影，而 `_emit_context_waterline_alert` 内部又执行了一次 `used = durable_history_tokens + checkpoint_tokens`——同一份 payload 加了两遍。与您的复现（500 报 800）完全吻合。

修复：告警 helper 直接使用调用方传入的 durable 值（每份 payload 只进一个口径），并在 docstring 写明该约定。

测试按您的要求补了精确数值断言：
- `test_waterline_alert_exact_numeric_boundaries`——699 静默 / 700 触发 / 849 触发（报告 85%）/ 850 落入自动压缩线静默；
- `test_waterline_alert_used_excludes_second_checkpoint_add`——判别性场景：单算 750/1000 报 75% 触发告警；双算 1250 已越过 85% 自动线反而静默——这正是双计的危害：不仅虚报水位，还会把该告警的区间错误地推进自动压缩。

### ② 失败 / error 结果不投影

**已修复，并按"引用已验证且可重新获取"做了往返验证。**

- 状态门：`is_error=True` 跳过；`execution_status.status` 存在且非 `success`（error/timeout/cancelled/unknown）跳过——失败输出常是诊断信息的唯一副本，永不替换为信封；
- 恢复源验证（round-trip）：投影信封生成后立即解析其 handle → 从 store 回读 → SHA256 比对，任一环节失败则放弃替换、原文保留内联。这同时覆盖了"引用已验证"与"可重新获取"两个条件；
- docstring 已同步改写，与实现严格一致（第一版确实存在描述承诺了 successful 但循环未过滤的偏差，感谢指出）。

测试覆盖：`never_projects_failed_results`（is_error 与 status=error 两形态）、`skips_non_success_execution_status`（timeout/cancelled/unknown）、`requires_verifiable_recovery_source`（store 回读失败 → 不投影）。

### ③ 共享 turn path 默认关 + kill switch

**已接受：投影默认关，告警保持默认开。**

- `AgentConfig.tool_result_history_projection_enabled` 默认 `False`；
- 网关 `[agent_token_saving]` 新增 `tool_result_history_projection_enabled` 与 `tool_result_history_projection_keep_recent_turns` 两键，装配链（gateway config → harness → bootstrap stage → AgentConfig）贯通，支持环境变量覆盖；
- 投影入口**每请求热读**开关——改配置即生效，无需重建 Agent，可作紧急关闭开关；
- 只读告警不受影响，保持默认开启。

### ④ 测试矩阵

- 新增 6 例：默认关、is_error、非 success 三态、恢复源不可验证、告警精确边界、checkpoint 单算判别场景；
- 受影响面本地全量：**729 passed**（水位两套件 15 + turn_runner 全部 + preflight + t3 + compaction 族）+ ruff 零告警；
- `tests/test_gateway` 中 3 个 pending-attachment 失败已用 `git worktree` 在纯 upstream/main（64e0a06c4）复现——为上游存量问题（Windows MAX_PATH 下测试断言裸读长路径，与生产路径的 `\\?\` 前缀写入不匹配），与本次改动无关，将另开独立 PR 修复测试；
- 完整跨平台 CI 因 fork PR workflow 审批门未触发，可在贵方流水线执行。

---

另：#1445 的姊妹修复已按前述各自独立守边界的原则推进，两 PR 互为补充但各自可独立验证。

修订提交 `caaf5b125` 已推到此分支（含最新 upstream/main 合并，39 个上游提交零冲突），请审阅。
