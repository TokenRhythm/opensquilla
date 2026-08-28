# MEMORY.md

Use this file for curated durable non-profile facts, preferences, decisions, and
constraints that are safe to include in private agent context.

# PowerShell 中文提交信息坑（2026-08-26 实证）
- PowerShell 内联传中文给 `git commit -m` 会产生乱码提交信息（gh 的 --title/--body-file 不受影响）
- 对策：提交信息写 UTF-8 文件，用 `git commit -F <file>`；显示乱码时先 `git cat-file commit HEAD` 验证真实存储内容再决定是否返修

# 子代理路由语义（2026-08-27 源码亲证，防误读）
- `[agents_defaults.subagents] max_tier` 是**死键**（全库零消费者，Pydantic 静默丢弃）——禁止引用"子代理钉 c0 / 天花板 c0"
- 子代理真实模型链（现状）：显式 spawn model > `subagents.model` > **继承父模型**（同 provider 约束，subagent.py:145）——**零折扣、裸继承**，c3 任务的子代理也按父价跑
- `:subagent:` 在 squilla_router.py:1239（附件容量门）/ :1809（分类器）仅跳过，无钉档
- **8/27 策略定稿（v2 实施项，暂不动代码）**：新增 `tier_policy` 键（取代死键 `max_tier`）：`step_down`（默认：子档 = 父档 − 1，**t<0 → c0 地板**，c0 父 → c0 子）| `fixed:c0,c1`（可选省成本模式）；链变为 显式 > subagents.model > tier_policy > 继承父；三道硬判断：① c0 地板 ② 跨 provider → 回退同 provider 最低档、再无继承父 ③ router 关/父无 tier → 跳过 policy；tier→model spawn 时读活配置
- 定稿 v1.2 快照规则：config 快照保留 8/27 10:08 首次参考，正文以 c0~c3 + 融合池代指档位，tier→model 以 config.toml 为准，快照不追漂移
- config 8/27 清理：ox-alpha/openrouter 死块已删（备份 `config.toml.bak-20260827`）；融合候选池 = 3 proposer + 1 aggregator（tier→model 以 config.toml 为准，当天多次翻动），**无 context_window 声明**（重开融合前需补）；`[llm_ensemble] enabled = false` 但 c3 档 `ensemble_enabled = true` → c3 融合分支仍活

# 主题分支备份矩阵（追加记录）
- 新增：`fix/compaction-accounting-guard`（=0a3731012，PR #1445 压缩记账守卫四刀，已推 origin 备份 + realfork PR 分支）
