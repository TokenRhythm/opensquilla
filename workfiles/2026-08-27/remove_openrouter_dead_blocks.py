# -*- coding: utf-8 -*-
"""删除 ox-alpha / openrouter 死配置块（2026-08-27，主人明确指令）
目标块：
  - [llm_profiles.openrouter]  (model = stealth/ox-alpha)
  - [models.openrouter."stealth/ox-alpha"]  (context_window = 1000000)
安全：先备份 → 前置 tomllib 校验 → 按表头匹配整块删除（不触碰其他行）
      → 后置 tomllib 校验 → 残留 token 断言 → 原子写回
"""
import os, shutil, tomllib, pathlib

p = pathlib.Path(r"C:\Users\chine\.opensquilla\config.toml")
text = p.read_text(encoding="utf-8")
tomllib.loads(text)  # 前置校验：当前文件本身必须合法
lines = text.splitlines(keepends=True)

HEADERS = [
    "[llm_profiles.openrouter]",
    '[models.openrouter."stealth/ox-alpha"]',
]

out, hits = [], set()
i, n = 0, len(lines)
while i < n:
    if lines[i].strip() in HEADERS:
        hits.add(lines[i].strip())
        i += 1
        # 消费到下一个表头（含块内空行）
        while i < n and not lines[i].lstrip().startswith("["):
            i += 1
        # 若紧跟下一表头还有多余空行，吞一个，避免双空行
        if i < n and lines[i].strip() == "" and i + 1 < n and lines[i + 1].lstrip().startswith("["):
            i += 1
        continue
    out.append(lines[i])
    i += 1

assert hits == set(HEADERS), f"块未全部命中: missing={set(HEADERS) - hits}"
new_text = "".join(out)
tomllib.loads(new_text)  # 后置校验：删除后仍必须合法

for tok in ("openrouter", "ox-alpha", "stealth"):
    assert tok not in new_text.lower(), f"残留引用: {tok}"

bak = p.with_name("config.toml.bak-20260827")
shutil.copy2(p, bak)
tmp = p.with_name(".config.toml.tmp-delete")
tmp.write_text(new_text, encoding="utf-8")
os.replace(tmp, p)

print("OK removed blocks:", sorted(hits))
print("backup:", bak)
print("new size:", len(new_text.encode("utf-8")), "bytes / lines:", new_text.count("\n") + 1)
