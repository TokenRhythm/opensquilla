#!/usr/bin/env bash
# 桥爷专用：只添加第 7 把 Key（TokenRhythm 官方）
# 在系统「终端.app」里执行：
#   bash "/Users/linlang/04 AI鼓捣/Opensquilla/opensquilla-src/scripts/add-key7-tokenrhythm.sh"

set -euo pipefail

STORE="${HOME}/.opensquilla/relay-keys.env"
BASE_URL="https://tokenrhythm.studio/v1"

echo "=========================================="
echo "  添加第 7 把 Key（TokenRhythm）"
echo "  地址: ${BASE_URL}"
echo "=========================================="
echo ""

if [ ! -t 0 ]; then
  echo "请用 Mac「终端」App 打开再运行，不要在别的地方跑。"
  exit 1
fi

# 先加载已有 1～6（如果有）
if [ -f "$STORE" ]; then
  # shellcheck disable=SC1090
  . "$STORE"
  echo "已读到旧钥匙文件。"
else
  echo "还没有旧钥匙文件，将只保存 Key7（建议以后把 1～6 也再 setup 一次）。"
fi

echo ""
echo "请粘贴 TokenRhythm 的 API Key，然后按回车。"
echo "（粘贴时屏幕可能不显示字符，正常）"
printf "Key7: "
IFS= read -r KEY7 || true
KEY7="$(printf '%s' "$KEY7" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

if [ -z "$KEY7" ]; then
  echo "✗ 没有输入 Key，已取消。"
  exit 1
fi

echo "✓ 收到 Key7，长度 ${#KEY7}"
echo ""

_escape() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

mkdir -p "${HOME}/.opensquilla"
chmod 700 "${HOME}/.opensquilla" 2>/dev/null || true
umask 077

{
  echo "# 桥爷 Keys — 勿发给别人"
  echo "# 更新时间: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "export OPENSQUILLA_RELAY_KEY_COUNT='7'"
  for i in 1 2 3 4 5 6; do
    eval "v=\${OPENSQUILLA_RELAY_KEY_$i:-}"
    echo "export OPENSQUILLA_RELAY_KEY_${i}='$(_escape "$v")'"
  done
  echo "export OPENSQUILLA_RELAY_KEY_7='$(_escape "$KEY7")'"
  echo "export OPENSQUILLA_TOKENRHYTHM_BASE_URL='${BASE_URL}'"
  echo 'export CUSTOM_LLM_API_KEY="$OPENSQUILLA_RELAY_KEY_1"'
  echo 'export TOKENRHYTHM_API_KEY="$OPENSQUILLA_RELAY_KEY_7"'
} > "$STORE"
chmod 600 "$STORE"

echo "✓ 已保存到: $STORE"
echo ""

# 自动拉模型列表（不打印 Key）
echo "正在向 TokenRhythm 查询模型列表……"
TMP="$(mktemp)"
HTTP_CODE="$(
  curl -sS -o "$TMP" -w "%{http_code}" \
    -H "Authorization: Bearer ${KEY7}" \
    -H "Content-Type: application/json" \
    "${BASE_URL}/models" 2>/dev/null || echo "ERR"
)"

echo "HTTP 状态: ${HTTP_CODE}"
if [ "$HTTP_CODE" = "200" ]; then
  echo "---------- 模型列表（复制下面整段发给瓜娃子）----------"
  # 尽量只抽出 id
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$TMP" <<'PY'
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
try:
    j = json.loads(raw)
except Exception:
    print(raw[:3000])
    raise SystemExit
ids = []
if isinstance(j, dict) and isinstance(j.get("data"), list):
    for x in j["data"]:
        if isinstance(x, dict) and x.get("id"):
            ids.append(str(x["id"]))
elif isinstance(j, list):
    for x in j:
        if isinstance(x, dict) and x.get("id"):
            ids.append(str(x["id"]))
print(f"Key7 TokenRhythm 模型共 {len(ids)} 个：")
for i, m in enumerate(ids, 1):
    print(f"{i}. {m}")
if not ids:
    print(raw[:2000])
PY
  else
    head -c 4000 "$TMP"
    echo
  fi
  echo "----------------------------------------------------------"
  # 另存一份无 Key 报告
  REPORT="${HOME}/.opensquilla/tokenrhythm-models.txt"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$TMP" "$REPORT" <<'PY'
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
out = Path(sys.argv[2])
try:
    j = json.loads(raw)
    ids = []
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        for x in j["data"]:
            if isinstance(x, dict) and x.get("id"):
                ids.append(str(x["id"]))
    lines = [f"Key7 TokenRhythm 模型共 {len(ids)} 个："] + [f"{i}. {m}" for i,m in enumerate(ids,1)]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
except Exception:
    out.write_text(raw[:5000], encoding="utf-8")
print(f"也已保存到: {out}")
PY
  fi
else
  echo "查询失败。可能是 Key 不对，或网络不通。"
  echo "返回内容前几行："
  head -c 500 "$TMP" 2>/dev/null || true
  echo
  echo "Key 已保存。你可以稍后把后台的 12 个模型名手抄发给瓜娃子。"
fi
rm -f "$TMP"

echo ""
echo "下一步："
echo "  1) 把上面「模型列表」整段复制，发给瓜娃子"
echo "  2) 不要发送完整 Key"
echo "  3) 等瓜娃子给路由建议表，你确认后再改配置"
