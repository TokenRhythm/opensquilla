---
name: history-explorer
description: "Query the per-turn DecisionEntry log for skill co-occurrence patterns, meta-skill usage stats, and the router fixture corpus. Returns a JSON summary suitable for downstream LLM consumption. Used by meta-skill-creator's harvest step but also useful standalone for 'which skills did I use most this week?'"
description_zh: "查询每轮的DecisionEntry日志，分析技能共现模式、元技能使用统计和路由器fixture语料库，返回适合下游LLM消费的JSON摘要。供meta-skill-creator的harvest步骤使用，也可单独用于'本周我最常用哪些技能？'之类的查询。"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/explore.py
  args:
    - --query
    - "{{ with.query | truncate(512) }}"
    - --window-days
    - "{{ with.window_days | default('30') }}"
    - --include
    - "{{ with.include | join(',') if with.include is sequence and with.include is not string else with.include | default('co_occurrences,meta_usage,router_fixtures') }}"
    - --top-k
    - "10"
  parse: json
  timeout: 30
---

# History Explorer

Lightweight read-only view over `~/.opensquilla/logs/decisions-*.jsonl`. Aggregates `DecisionEntry.skills_invoked` (SCHEMA_VERSION 10) into co-occurrence frequencies, joins with `SkillLoader.list_meta_specs()` for meta-skill usage stats, and surfaces the `tests/test_skills/router_fixtures/` corpus.

## Usage

```
uv run python {baseDir}/scripts/explore.py \
  --log-dir ~/.opensquilla/logs \
  --query "Co-occurring chains for PDF workflows" \
  --window-days 30 \
  --include co_occurrences,meta_usage,router_fixtures \
  --top-k 10
```

## Output

JSON to stdout with keys `co_occurrences`, `meta_usage`, `router_fixtures`, and a `placeholder` string when the log is empty.

## Fallback

If no decision-log exists, return an empty result with a placeholder string explaining "no history; downstream should rely on user intent only".
