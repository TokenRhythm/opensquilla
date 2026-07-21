#!/usr/bin/env bash
# 在「单模型智能路由」和「多模型集成」之间切换
#
#   ./scripts/switch-routing-mode.sh single   # 默认：省钱智能路由
#   ./scripts/switch-routing-mode.sh multi    # 多模型一起答再融合
#   ./scripts/switch-routing-mode.sh status

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$ROOT/opensquilla.toml"
MODE="${1:-}"

if [[ ! -f "$CFG" ]]; then
  echo "找不到 $CFG" >&2
  exit 1
fi

_py_toggle() {
  local mode="$1"
  python3 - "$CFG" "$mode" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
mode = sys.argv[2]
text = path.read_text(encoding="utf-8")

def set_in_section(text, section, key, value):
    # very small TOML patcher for boolean keys we own
    pat = re.compile(
        rf"(?ms)^(\[{re.escape(section)}\][^\[]*?)(^{re.escape(key)}\s*=\s*)(true|false)",
        re.MULTILINE,
    )
    def repl(m):
        return m.group(1) + m.group(2) + value
    new, n = pat.subn(repl, text, count=1)
    if n != 1:
        raise SystemExit(f"无法在 [{section}] 找到 {key}=true|false，请手改 opensquilla.toml")
    return new

if mode == "single":
    text = set_in_section(text, "llm_ensemble", "enabled", "false")
    text = set_in_section(text, "squilla_router", "enabled", "true")
elif mode == "multi":
    text = set_in_section(text, "llm_ensemble", "enabled", "true")
    text = set_in_section(text, "squilla_router", "enabled", "false")
else:
    raise SystemExit("mode must be single|multi")

path.write_text(text, encoding="utf-8")
print(f"已切换为 {mode}: {path}")
PY
}

_status() {
  python3 - "$CFG" <<'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
def grab(section, key):
    m = re.search(rf"(?ms)^\[{re.escape(section)}\][^\[]*?^{re.escape(key)}\s*=\s*(true|false)", text, re.M)
    return m.group(1) if m else "?"
ens = grab("llm_ensemble", "enabled")
rt = grab("squilla_router", "enabled")
print(f"llm_ensemble.enabled = {ens}")
print(f"squilla_router.enabled = {rt}")
if ens == "true" and rt == "false":
    print("当前模式: multi（多模型集成）")
elif ens == "false" and rt == "true":
    print("当前模式: single（单模型智能路由）")
else:
    print("当前模式: 自定义/混合（请检查 toml）")
PY
}

case "$MODE" in
  single|multi)
    _py_toggle "$MODE"
    echo "记得: opensquilla gateway reload"
    ;;
  status|"")
    _status
    if [[ -z "$MODE" ]]; then
      echo "用法: $0 single|multi|status"
    fi
    ;;
  *)
    echo "用法: $0 single|multi|status" >&2
    exit 1
    ;;
esac
