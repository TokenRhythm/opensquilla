#!/usr/bin/env bash
# 探测：每把中转 Key 分别能用哪些模型（不打印完整 Key）
# 支持 Key1…Key20（有多少探测多少）
#
# 用法：
#   source ~/.opensquilla/relay-keys.env
#   bash scripts/probe-relay-models.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "${HOME}/.opensquilla/relay-keys.env" ]; then
  # shellcheck disable=SC1090
  . "${HOME}/.opensquilla/relay-keys.env"
fi
if [ -z "${OPENSQUILLA_RELAY_KEY_1:-}" ] && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  export OPENSQUILLA_RELAY_KEY_1="$ANTHROPIC_AUTH_TOKEN"
fi

BASE_URL="${OPENSQUILLA_RELAY_BASE_URL:-https://a.99cy.edu.kg/v1}"
BASE_URL="${BASE_URL%/}"
export OPENSQUILLA_PROBE_BASE_URL="$BASE_URL"

# 把已设置的 KEY_1..20 全部 export 给 python
MAX_KEYS=20
for i in $(seq 1 "$MAX_KEYS"); do
  eval "val=\${OPENSQUILLA_RELAY_KEY_$i:-}"
  if [ -n "$val" ]; then
    export OPENSQUILLA_RELAY_KEY_$i="$val"
  fi
done
export OPENSQUILLA_PROBE_MAX_KEYS="$MAX_KEYS"

PY="python3"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
fi

exec "$PY" - "$ROOT" <<'PY'
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("OPENSQUILLA_PROBE_BASE_URL", "https://a.99cy.edu.kg/v1").rstrip("/")
MAX_KEYS = int(os.environ.get("OPENSQUILLA_PROBE_MAX_KEYS", "20"))
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

KEYS: list[tuple[int, str, str]] = []
for i in range(1, MAX_KEYS + 1):
    name = f"OPENSQUILLA_RELAY_KEY_{i}"
    val = (os.environ.get(name) or "").strip()
    if val:
        KEYS.append((i, name, val))


def mask(k: str) -> str:
    if not k:
        return "(空)"
    if len(k) <= 10:
        return k[:2] + "***" + k[-2:]
    return k[:6] + "…" + k[-4:]


def http_json(method: str, url: str, key: str, body: dict | None = None, timeout: float = 25.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, raw
    except Exception as e:
        return "ERR", f"{type(e).__name__}: {e}"


def list_models(key: str) -> tuple[str, list[str], str]:
    code, raw = http_json("GET", f"{BASE}/models", key, timeout=20)
    if code != 200:
        return f"失败 HTTP {code}", [], raw[:300]
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return "失败 JSON", [], raw[:300]
    ids: list[str] = []
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        for item in j["data"]:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    elif isinstance(j, list):
        for item in j:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    return "OK", sorted(set(ids)), ""


def smoke_chat(key: str, model: str) -> str:
    code, raw = http_json(
        "POST",
        f"{BASE}/chat/completions",
        key,
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回复: pong"}],
            "max_tokens": 16,
            "temperature": 0,
        },
        timeout=45,
    )
    if code != 200:
        return f"对话失败 HTTP {code}: {raw[:160].replace(chr(10), ' ')}"
    try:
        j = json.loads(raw)
        content = j.get("choices", [{}])[0].get("message", {}).get("content", "")
        return f"对话OK → {content!r}"
    except Exception:
        return f"对话OK(解析粗略) len={len(raw)}"


CHEAP_HINTS = ("flash", "mini", "haiku", "lite", "small", "nano", "instant", "fast", "air")
STRONG_HINTS = ("opus", "max", "pro", "ultra", "reasoner", "r1", "o1", "o3", "thinking")
VISION_HINTS = ("vision", "vl", "image", "gpt-4o", "gemini", "grok-4")
IMAGE_GEN_HINTS = ("imagine", "dall", "image-gen", "flux", "sdxl", "draw")


def guess_role(mid: str) -> str:
    m = mid.lower()
    if any(h in m for h in IMAGE_GEN_HINTS):
        return "生图(一般不当聊天档)"
    if any(h in m for h in VISION_HINTS) and "imagine" not in m:
        return "可能看图"
    if any(h in m for h in CHEAP_HINTS):
        return "建议 c0 便宜"
    if any(h in m for h in STRONG_HINTS):
        return "建议 c2/c3 强"
    return "建议 c1 默认"


print("=" * 56)
print("  中转模型探测（每把 Key 单独问中转站）")
print("=" * 56)
print(f"中转: {BASE}")
print(f"已加载 Key 数量: {len(KEYS)}")
print("说明: Key 本身看不出模型；必须用这把 Key 调 /v1/models")
print()

if not KEYS:
    print("✗ 一个 Key 都没有。请先:")
    print("  bash scripts/setup-relay-keys.sh")
    sys.exit(1)

reports: list[dict] = []
key_by_idx = {i: k for i, _, k in KEYS}

for idx, env_name, key in KEYS:
    print("-" * 56)
    print(f"【Key{idx}】{env_name}")
    print(f"  指纹: {mask(key)}  长度: {len(key)}")
    status, models, err = list_models(key)
    if status != "OK":
        print(f"  列表: {status}")
        if err:
            print(f"  详情: {err}")
        reports.append({"idx": idx, "models": [], "ok": False, "smokes": {}})
        continue
    print(f"  列表: OK，共 {len(models)} 个模型")
    if not models:
        print("  （空列表 — 这把 Key 的 group 可能没开模型）")
    for mid in models:
        print(f"    · {mid:40s}  {guess_role(mid)}")
    chat_candidates = [
        m for m in models if not any(h in m.lower() for h in IMAGE_GEN_HINTS)
    ][:3]
    if not chat_candidates and models:
        chat_candidates = models[:1]
    print("  冒烟对话:")
    smokes: dict[str, str] = {}
    for mid in chat_candidates:
        result = smoke_chat(key, mid)
        smokes[mid] = result
        print(f"    · {mid}: {result}")
    reports.append({"idx": idx, "models": models, "ok": True, "smokes": smokes})

print("-" * 56)

all_models: list[str] = []
seen: set[str] = set()
for r in reports:
    for m in r.get("models") or []:
        if m not in seen:
            seen.add(m)
            all_models.append(m)

print()
print("=" * 56)
print("  汇总")
print("=" * 56)
print(f"全部 Key 合起来不重复模型数: {len(all_models)}")
for m in all_models:
    owners = [f"Key{r['idx']}" for r in reports if m in (r.get("models") or [])]
    print(f"  · {m:40s}  来自 {','.join(owners)}  | {guess_role(m)}")

model_sets = [tuple(r.get("models") or []) for r in reports if r.get("ok")]
if len(model_sets) >= 2 and len(set(model_sets)) > 1:
    print()
    print("⚠ 不同 Key 的模型列表不一致 → 可能是不同套餐/group。")
    print("  主路径默认用 Key1；Key2+ 目前是失败轮换，不会自动「按模型选 Key」。")
    print("  若某模型只在 Key5 有，告诉我，我再帮你设计映射。")

chat_models = [m for m in all_models if not any(h in m.lower() for h in IMAGE_GEN_HINTS)]
cheap = [m for m in chat_models if guess_role(m).startswith("建议 c0")]
strong = [m for m in chat_models if "c2/c3" in guess_role(m)]
mid = [m for m in chat_models if m not in cheap and m not in strong]
c0 = cheap[0] if cheap else (mid[0] if mid else (chat_models[0] if chat_models else ""))
c1 = mid[0] if mid else c0
c2 = strong[0] if strong else (mid[1] if len(mid) > 1 else c1)
c3 = strong[-1] if strong else c2
img = next(
    (
        m
        for m in all_models
        if "imagine" not in m.lower()
        and any(h in m.lower() for h in ("vision", "vl", "4o", "gemini"))
    ),
    c1,
)

print()
print("=" * 56)
print("  建议档位草稿（按名字猜，仅供参考）")
print("=" * 56)
if not chat_models:
    print("  没有可用聊天模型，无法建议。")
else:
    print(f"  c0 便宜: {c0}")
    print(f"  c1 默认: {c1}")
    print(f"  c2 较强: {c2}")
    print(f"  c3 最强: {c3}")
    print(f"  看图:   {img}")
    print()
    print("  → 把本终端从「Key1」到「汇总」整段复制发给瓜娃子，")
    print("    我按真实名单改 opensquilla.toml 路由档位。")

out = Path.home() / ".opensquilla" / "relay-models-report.json"
out.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "base_url": BASE,
    "key_count": len(KEYS),
    "keys": [
        {
            "index": r["idx"],
            "ok": r.get("ok"),
            "models": r.get("models") or [],
            "smokes": r.get("smokes") or {},
            "fingerprint": mask(key_by_idx.get(r["idx"], "")),
        }
        for r in reports
    ],
    "union_models": all_models,
    "suggested_tiers": {
        "c0": c0,
        "c1": c1,
        "c2": c2,
        "c3": c3,
        "image_model": img,
    }
    if chat_models
    else {},
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print()
print(f"报告已保存: {out}")
print("（不含完整 Key，只有指纹和模型名）")
PY
