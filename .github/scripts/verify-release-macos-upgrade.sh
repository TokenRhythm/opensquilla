#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 2 || "$#" -gt 3 ]]; then
  echo "usage: $0 CANDIDATE_DMG LABEL [BASELINE_VERSION]" >&2
  exit 2
fi

baseline_version="${3-0.5.3}"
if [[ "${baseline_version}" != "0.5.3" && "${baseline_version}" != "0.5.4" ]]; then
  echo "baseline version must be 0.5.3 or 0.5.4" >&2
  exit 2
fi
candidate_dmg="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
label="$2"
if [[ ! "${label}" =~ ^[A-Za-z0-9._-]{1,80}$ ]]; then
  echo "label must contain only ASCII letters, digits, dot, underscore, or dash" >&2
  exit 2
fi

sandbox="${RUNNER_TEMP}/opensquilla-release-preservation-${label}-${baseline_version}"
old_dir="${sandbox}/v${baseline_version}"
old_mount="${sandbox}/v${baseline_version}-mount"
candidate_mount="${sandbox}/candidate-mount"
install_root="${sandbox}/Applications"
user_data="${sandbox}/user-data/OpenSquilla"
profile="${user_data}/opensquilla"
probe="${GITHUB_WORKSPACE}/.github/scripts/verify-release-profile-preservation.py"
external_sentinels="${sandbox}/synthetic-system-tools"
session_recovery_smoke="${GITHUB_WORKSPACE}/desktop/electron/scripts/test-packaged-session-recovery.mjs"
old_asset="OpenSquilla-${baseline_version}-mac-arm64.dmg"
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

gh release download "v${baseline_version}" \
  --repo TokenRhythm/opensquilla \
  --pattern "${old_asset}" \
  --dir "${old_dir}"
old_dmg="${old_dir}/${old_asset}"
test -f "${old_dmg}"
test -f "${candidate_dmg}"

hdiutil attach -nobrowse -readonly -mountpoint "${old_mount}" "${old_dmg}"
ditto "${old_mount}/OpenSquilla.app" "${install_root}/OpenSquilla.app"
hdiutil detach "${old_mount}" -quiet
old_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
  "${install_root}/OpenSquilla.app/Contents/Info.plist")"
test "${old_version}" = "${baseline_version}"
# v0.5.3 bundles developer tools; v0.5.4 uses the slim Runtime Pack layout.
if [[ "${baseline_version}" == "0.5.3" ]]; then
  old_runtime="${install_root}/OpenSquilla.app/Contents/Resources/runtime/developer/darwin-arm64"
  test -x "${old_runtime}/python/bin/python3"
  test -x "${old_runtime}/node/bin/node"
else
  old_runtime="${install_root}/OpenSquilla.app/Contents/Resources/runtime"
  test ! -e "${old_runtime}/developer"
  test -f "${old_runtime}/runtime-manifest.json"
  test -f "${old_runtime}/runtime-pack-catalog.json"
fi

python "${probe}" seed --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

hdiutil attach -nobrowse -readonly -mountpoint "${candidate_mount}" "${candidate_dmg}"
mv "${install_root}/OpenSquilla.app" "${install_root}/OpenSquilla.v${baseline_version}.app"
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

node "${session_recovery_smoke}" \
  --executable "${app_binary}" \
  --user-data-dir "${user_data}" \
  --session-key agent:main:webchat:release-recovery-long-session \
  --switch-session-key agent:main:webchat:release-recovery-switch-session \
  --label "${label}"

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

python - "${install_root}/OpenSquilla.app" "${install_root}/OpenSquilla.v${baseline_version}.app" <<'PY'
import shutil
import sys

for app_path in sys.argv[1:]:
    shutil.rmtree(app_path)
PY
test ! -e "${install_root}/OpenSquilla.app"
test ! -e "${install_root}/OpenSquilla.v${baseline_version}.app"
python "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"
