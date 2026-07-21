#!/usr/bin/env bash
# 桥爷专用：一次性把「任意多个」中转 Key 存到本机
# 用法：在系统「终端.app」里执行
#   cd "/Users/linlang/04 AI鼓捣/Opensquilla/opensquilla-src"
#   bash scripts/setup-relay-keys.sh
#
# 会写入：~/.opensquilla/relay-keys.env（仅你可读写）
# 可以 1 个、3 个、10 个…… 有多少填多少；空行结束

set -euo pipefail

STORE_DIR="${HOME}/.opensquilla"
STORE_FILE="${STORE_DIR}/relay-keys.env"
# 安全上限，防止误操作狂贴；真要更多改这个数即可
MAX_KEYS=20

mkdir -p "$STORE_DIR"
chmod 700 "$STORE_DIR" 2>/dev/null || true

echo "=========================================="
echo "  OpenSquilla 中转 Key 设置向导"
echo "=========================================="
echo ""
echo "有多少把 Key 就填多少把（最多 ${MAX_KEYS} 把）。"
echo "某一把直接回车 = 结束输入。"
echo "粘贴时屏幕可能不显示字符，正常；输完按回车。"
echo ""

if [ ! -t 0 ]; then
  echo "当前不是交互终端，无法现场输入。"
  echo "请在系统「终端.app」里执行本脚本。"
  exit 1
fi

if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "检测到本机已有 ANTHROPIC_AUTH_TOKEN（Claude 在用的那把）。"
  echo "第 1 把若直接回车，会自动复用它。"
  echo ""
fi

# 读入多把 Key
KEYS=()
i=1
while [ "$i" -le "$MAX_KEYS" ]; do
  if [ "$i" -eq 1 ] && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    printf "第 %d 把 Key（必填；回车=复用 Claude 那把）: " "$i"
  elif [ "$i" -eq 1 ]; then
    printf "第 %d 把 Key（必填）: " "$i"
  else
    printf "第 %d 把 Key（没有了就直接回车结束）: " "$i"
  fi
  IFS= read -r val || true
  # 去掉首尾空白
  val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

  if [ -z "$val" ]; then
    if [ "$i" -eq 1 ] && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
      val="$ANTHROPIC_AUTH_TOKEN"
      echo "  → 已复用 ANTHROPIC_AUTH_TOKEN（长度 ${#val}）"
      KEYS+=("$val")
      i=$((i + 1))
      continue
    fi
    if [ "$i" -eq 1 ]; then
      echo "✗ 至少需要 1 把 Key" >&2
      exit 1
    fi
    echo "  → 结束输入（共 $((i - 1)) 把）"
    break
  fi

  KEYS+=("$val")
  echo "  → 已收下第 $i 把（长度 ${#val}）"
  i=$((i + 1))
done

n=${#KEYS[@]}
if [ "$n" -eq 0 ]; then
  echo "✗ 没有收到任何 Key" >&2
  exit 1
fi

_escape() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

umask 077
{
  echo "# 由 setup-relay-keys.sh 生成 — 勿提交 git、勿发给别人"
  echo "# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "# 共 ${n} 把 Key"
  echo "export OPENSQUILLA_RELAY_KEY_COUNT='${n}'"
  idx=1
  for k in "${KEYS[@]}"; do
    echo "export OPENSQUILLA_RELAY_KEY_${idx}='$(_escape "$k")'"
    idx=$((idx + 1))
  done
  # 清空可能残留的旧 KEY（上次填了 10 把、这次只填 4 把）
  for ((j = n + 1; j <= MAX_KEYS; j++)); do
    echo "unset OPENSQUILLA_RELAY_KEY_${j} 2>/dev/null || true"
  done
  echo 'export CUSTOM_LLM_API_KEY="$OPENSQUILLA_RELAY_KEY_1"'
} > "$STORE_FILE"
chmod 600 "$STORE_FILE"

echo ""
echo "✓ 已保存 ${n} 把 Key → $STORE_FILE"
echo "  权限: 仅你自己可读写"
echo ""
idx=1
for k in "${KEYS[@]}"; do
  echo "  Key${idx}: 长度 ${#k}"
  idx=$((idx + 1))
done

echo ""
echo "是否写入 ~/.zshrc，让以后每个新终端自动加载？(y/N)"
printf "> "
IFS= read -r ans || true
if [ "${ans:-}" = "y" ] || [ "${ans:-}" = "Y" ]; then
  MARKER="# OpenSquilla relay keys (auto)"
  ZSHRC="${HOME}/.zshrc"
  touch "$ZSHRC"
  if grep -Fq "$MARKER" "$ZSHRC" 2>/dev/null; then
    echo "· ~/.zshrc 里已有自动加载行，跳过"
  else
    {
      echo ""
      echo "$MARKER"
      echo "[ -f \"\$HOME/.opensquilla/relay-keys.env\" ] && source \"\$HOME/.opensquilla/relay-keys.env\""
    } >> "$ZSHRC"
    echo "✓ 已写入 ~/.zshrc（新开终端自动带 Key）"
  fi
fi

echo ""
echo "下一步（复制整段执行）："
echo "  source \"$STORE_FILE\""
echo "  cd \"/Users/linlang/04 AI鼓捣/Opensquilla/opensquilla-src\""
echo "  bash scripts/probe-relay-models.sh"
echo ""
echo "探测脚本会告诉你：第几把 Key → 能用哪些模型。"
echo "把终端输出发我，我帮你写进路由档位。"
