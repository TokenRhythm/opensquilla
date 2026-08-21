#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 CANDIDATE_DMG LABEL" >&2
  exit 2
fi

candidate_dmg="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
label="$2"
if [[ ! "${label}" =~ ^[A-Za-z0-9._-]{1,80}$ ]]; then
  echo "label must contain only ASCII letters, digits, dot, underscore, or dash" >&2
  exit 2
fi

sandbox="${RUNNER_TEMP}/opensquilla-release-preservation-${label}"
old_dir="${sandbox}/v0.5.3"
old_mount="${sandbox}/v0.5.3-mount"
candidate_mount="${sandbox}/candidate-mount"
install_root="${sandbox}/Applications"
user_data="${sandbox}/user-data/OpenSquilla"
profile="${user_data}/opensquilla"
probe="${GITHUB_WORKSPACE}/.github/scripts/verify-release-profile-preservation.py"
external_sentinels="${sandbox}/synthetic-system-tools"
old_asset="OpenSquilla-0.5.3-mac-arm64.dmg"
mkdir -p "${old_dir}" "${old_mount}" "${candidate_mount}" "${install_root}" "${user_data}"

cleanup() {
  hdiutil detach "${candidate_mount}" -quiet >/dev/null 2>&1 || true
  hdiutil detach "${old_mount}" -quiet >/dev/null 2>&1 || true
  if [[ -n "${app_pid:-}" ]]; then
    kill "${app_pid}" >/dev/null 2>&1 || true
    wait "${app_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

gh release download v0.5.3 \
  --repo opensquilla/opensquilla \
  --pattern "${old_asset}" \
  --dir "${old_dir}"
old_dmg="${old_dir}/${old_asset}"
test -f "${old_dmg}"
test -f "${candidate_dmg}"

hdiutil attach -nobrowse -readonly -mountpoint "${old_mount}" "${old_dmg}"
ditto "${old_mount}/OpenSquilla.app" "${install_root}/OpenSquilla.app"
hdiutil detach "${old_mount}" -quiet
old_runtime="${install_root}/OpenSquilla.app/Contents/Resources/runtime/developer/darwin-arm64"
test -x "${old_runtime}/python/bin/python3"
test -x "${old_runtime}/node/bin/node"

python "${probe}" seed --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

hdiutil attach -nobrowse -readonly -mountpoint "${candidate_mount}" "${candidate_dmg}"
mv "${install_root}/OpenSquilla.app" "${install_root}/OpenSquilla.v0.5.3.app"
ditto "${candidate_mount}/OpenSquilla.app" "${install_root}/OpenSquilla.app"
hdiutil detach "${candidate_mount}" -quiet
candidate_runtime="${install_root}/OpenSquilla.app/Contents/Resources/runtime"
test ! -e "${candidate_runtime}/developer"
test -f "${candidate_runtime}/runtime-manifest.json"
test -f "${candidate_runtime}/runtime-pack-catalog.json"
python "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

app_binary="${install_root}/OpenSquilla.app/Contents/MacOS/OpenSquilla"
test -x "${app_binary}"
OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE=1 \
  "${app_binary}" --use-mock-keychain "--user-data-dir=${user_data}" \
  >"${sandbox}/candidate-desktop.log" 2>&1 &
app_pid=$!
sleep 8
kill -0 "${app_pid}"
kill "${app_pid}" || true
wait "${app_pid}" || true
app_pid=""

gateway_binary="$(find \
  "${install_root}/OpenSquilla.app/Contents/Resources/runtime/gateway" \
  -type f -name opensquilla-gateway -perm -111 -print -quit)"
test -x "${gateway_binary}"
OPENSQUILLA_RECOVERY_OFFLINE=1 "${gateway_binary}" recovery inspect \
  --home "${profile}" --json >"${sandbox}/candidate-inspect.json"
python - "${profile}" "${sandbox}/candidate-inspect.json" <<'PY'
import json
from pathlib import Path
import sys

home = Path(sys.argv[1]).resolve()
report = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert report["outcome"] in {"ready", "attention"}, report
assert Path(report["primary_home"]).resolve() == home, report
assert Path(report["effective_workspace"]).resolve() == home / "workspace", report
configured_state = [
    candidate
    for candidate in report["candidates"]
    if candidate["kind"] == "state" and candidate["configured"] and candidate["valid"]
]
assert len(configured_state) == 1, report
assert Path(configured_state[0]["path"]).resolve() == home / "state", report
PY
python "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

python - "${install_root}/OpenSquilla.app" "${install_root}/OpenSquilla.v0.5.3.app" <<'PY'
import shutil
import sys

for app_path in sys.argv[1:]:
    shutil.rmtree(app_path)
PY
test ! -e "${install_root}/OpenSquilla.app"
test ! -e "${install_root}/OpenSquilla.v0.5.3.app"
python "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"
