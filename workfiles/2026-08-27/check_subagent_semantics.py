# -*- coding: utf-8 -*-
"""亲证 1：subagents.max_tier 是否有消费者 + spawn model 串格式"""
import os, re

BASE = r"D:\AIstudio\Harness\OpenSquilla-QinLuza-Studio"

cfg = open(os.path.join(BASE, "src/opensquilla/gateway/config.py"), encoding="utf-8").readlines()
print("--- AgentSubagentDefaults (1919-1941) 字段清单 ---")
for i in range(1918, 1942):
    print(i + 1, cfg[i].rstrip()[:115])

print("--- config.py 中 model_config / extra 设置 ---")
found = 0
for i, l in enumerate(cfg, 1):
    if "model_config" in l or re.search(r"extra\s*=", l):
        print(i, l.rstrip()[:115])
        found += 1
if not found:
    print("(无) → Pydantic 默认 extra='ignore'，未知键被静默丢弃")

sub = open(os.path.join(BASE, "src/opensquilla/engine/subagent.py"), encoding="utf-8").readlines()
print("--- subagent.py: model 串解析（split/rsplit/model_ref）---")
hits = 0
for i, l in enumerate(sub, 1):
    if ("split" in l or "model_ref" in l) and "model" in l.lower():
        print(i, l.rstrip()[:115])
        hits += 1
if not hits:
    print("(无 split 迹象)")

print("--- subagent.py 58-170（_clone_provider_for_subagent_model 全文扫 model 参数用法）---")
for i in range(57, 170):
    l = sub[i].rstrip()[:115]
    if "model" in l.lower() or "provider" in l.lower():
        print(i + 1, l)
