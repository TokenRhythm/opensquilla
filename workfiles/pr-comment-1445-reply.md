收到了，感谢细审。四个点全部处理完毕，逐条回复：

---

### ① 封顶值 + extras 重复计数

**已修复。** 重构为单值选择：`_reconciled_persisted_token_count` 在持久化消息级计数与条目自身投影估算之间取 `max()`，两个值永不叠加。两个估算器（`estimate_entry_replay_tokens` / `estimate_entry_model_replay_tokens`）统一走此函数，"封顶值当 content token 再加 extras"的结构性问题在机制上已消除。

历史存量虚高行留给一次性修正脚本；写端 B1（finalizer 的 4× 挡板）已防复发——读端不再玩读时魔法。

回归测试：`test_persisted_count_is_never_stacked_on_top_of_projection_extras` 使用确定性 tokenizer（`len//4`）断言结果精确落在投影估算值附近 ±5% 内，而非数量级下界。

### ② 1000 字符阈值 ≠ 结构化摘要

**已修复。** 阈值守卫整体删除，改为"逐字幸存"校验：fallback 的 `rolling_summary` 本就通过 `_merge_rolling_fallback` 包含 `prev_summary` 全文；提交前调用 `_fallback_preserves_previous_summary` 验证旧摘要是否逐字幸存于合并产物中。幸存则提交，否则拒绝。短结构化摘要与长摘要同权。

回归测试：`test_fallback_merge_preserves_prior_summary_verbatim`（富摘要合并幸存后提交）、`test_fallback_with_short_structured_summary_still_preserves`（短结构化摘要同样被保护）、`test_fallback_refuses_when_merged_overflow_destroys`（超窗裁剪导致幸存失败时拒绝提交）。

### ③ 真实会话标识入库

**已修复。** `src` 与 `tests` 共 5 处真实会话标识全部替换为合成描述。`git grep f4d2b4dc` 归零。PR 正文同步清理。

### ④ 测试矩阵

本地已跑受影响面全量：

- 守卫回归套件：**9 passed**
- compaction + provider runtime + turn_runner + t3 + preflight：**599 passed**
- ruff：零告警
- 全量 sweep 中 3 个失败（`test_compaction_dispatcher` 边界 / `test_delete_session_material_cleanup` 清理 / `test_storage_transactions` busy 计时）均已用 `git worktree` 在纯 upstream/main（c3558b8b3）上复跑证实为**存量问题**，与本次改动无关
- 完整跨平台 CI 因 fork PR workflow 审批门未触发，可在贵方流水线执行

---

修订提交 `1fb608a06` 已推到此 PR 分支，请审阅。如果测试矩阵需要补充其他族，请告知。