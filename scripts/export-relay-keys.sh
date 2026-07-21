#!/usr/bin/env bash
# 加载中转 Key 到当前终端（支持任意多把，不打印完整 Key）
#
# 用法：
#   source scripts/export-relay-keys.sh
# 或：
#   source ~/.opensquilla/relay-keys.env

STORE_FILE="${HOME}/.opensquilla/relay-keys.env"
MAX_KEYS=20

if [ -f "$STORE_FILE" ]; then
  # shellcheck disable=SC1090
  . "$STORE_FILE"
fi

# 还没有 KEY_1 时，尝试复用 Claude 的中转 Key
if [ -z "${OPENSQUILLA_RELAY_KEY_1:-}" ] && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  export OPENSQUILLA_RELAY_KEY_1="$ANTHROPIC_AUTH_TOKEN"
  export OPENSQUILLA_RELAY_KEY_COUNT="${OPENSQUILLA_RELAY_KEY_COUNT:-1}"
fi

_count=0
for i in $(seq 1 "$MAX_KEYS"); do
  eval "val=\${OPENSQUILLA_RELAY_KEY_$i:-}"
  if [ -n "$val" ]; then
    export OPENSQUILLA_RELAY_KEY_$i="$val"
    _count=$((_count + 1))
    echo "✓ OPENSQUILLA_RELAY_KEY_$i 已就绪（长度 ${#val}）"
  fi
done

export OPENSQUILLA_RELAY_KEY_COUNT="${OPENSQUILLA_RELAY_KEY_COUNT:-$_count}"

if [ -n "${OPENSQUILLA_RELAY_KEY_1:-}" ]; then
  export CUSTOM_LLM_API_KEY="$OPENSQUILLA_RELAY_KEY_1"
fi

if [ "$_count" -eq 0 ]; then
  echo "✗ 还没有 Key。"
  echo "  请先在系统终端执行："
  echo "    cd \"/Users/linlang/04 AI鼓捣/Opensquilla/opensquilla-src\""
  echo "    bash scripts/setup-relay-keys.sh"
  return 1 2>/dev/null || exit 1
fi

echo "共 ${_count} 把 Key 已加载"
echo "中转: ${OPENSQUILLA_RELAY_BASE_URL:-https://a.99cy.edu.kg/v1}"
if [ -f "$STORE_FILE" ]; then
  echo "来源: $STORE_FILE"
else
  echo "来源: 当前环境 / ANTHROPIC_AUTH_TOKEN"
fi
