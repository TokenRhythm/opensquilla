#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 CHANNEL_MANIFEST LABEL" >&2
  exit 2
fi

channel_manifest="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
label="$2"
if [[ ! "${label}" =~ ^[A-Za-z0-9._-]{1,80}$ ]]; then
  echo "label must contain only ASCII letters, digits, dot, underscore, or dash" >&2
  exit 2
fi

sandbox="${RUNNER_TEMP}/opensquilla-real-updater-${label}"
old_dir="${sandbox}/v0.5.3"
old_mount="${sandbox}/v0.5.3-mount"
user_data="${sandbox}/user-data/OpenSquilla"
profile="${user_data}/opensquilla"
probe="${GITHUB_WORKSPACE}/.github/scripts/verify-release-profile-preservation.py"
driver="${GITHUB_WORKSPACE}/desktop/electron/scripts/test-packaged-real-update-flow.mjs"
external_sentinels="${sandbox}/synthetic-system-tools"
installed_app="/Applications/OpenSquilla.app"
old_asset="OpenSquilla-0.5.3-mac-arm64.dmg"
expected_version="$(python3 - "${channel_manifest}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = manifest["version"]
assert manifest["tag"] == f"v{version}", manifest
assert manifest["prerelease"] is False, manifest
assert tuple(map(int, version.split("."))) > (0, 5, 3), manifest
print(manifest["version"])
PY
)"

if [[ -e "${installed_app}" ]]; then
  echo "Refusing to replace a pre-existing ${installed_app} on the release runner" >&2
  exit 1
fi
mkdir -p "${old_dir}" "${old_mount}" "${user_data}"

stop_installed_app() {
  local pid
  while IFS= read -r pid; do
    [[ -n "${pid}" ]] || continue
    kill "${pid}" >/dev/null 2>&1 || true
  done < <(pgrep -f '^/Applications/OpenSquilla[.]app/Contents/MacOS/OpenSquilla( |$)' || true)
}

wait_for_no_installed_processes() {
  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if ! pgrep -f '^/Applications/OpenSquilla[.]app/Contents/(MacOS/OpenSquilla|Resources/runtime/gateway/)' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "OpenSquilla left an installed-app process running after shutdown" >&2
  return 1
}

remove_installed_app() {
  python3 - "${installed_app}" <<'PY'
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1])
if target.as_posix() != "/Applications/OpenSquilla.app":
    raise SystemExit(f"refusing unsafe cleanup target: {target}")
if target.exists():
    shutil.rmtree(target)
PY
}

cleanup() {
  stop_installed_app || true
  hdiutil detach "${old_mount}" -quiet >/dev/null 2>&1 || true
  remove_installed_app
}
trap cleanup EXIT

gh release download v0.5.3 \
  --repo opensquilla/opensquilla \
  --pattern "${old_asset}" \
  --dir "${old_dir}"
old_dmg="${old_dir}/${old_asset}"
test -f "${old_dmg}"
hdiutil attach -nobrowse -readonly -mountpoint "${old_mount}" "${old_dmg}"
ditto "${old_mount}/OpenSquilla.app" "${installed_app}"
hdiutil detach "${old_mount}" -quiet

old_runtime="${installed_app}/Contents/Resources/runtime/developer/darwin-arm64"
test -x "${old_runtime}/python/bin/python3"
test -x "${old_runtime}/node/bin/node"
python3 "${probe}" seed --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

old_binary="${installed_app}/Contents/MacOS/OpenSquilla"
test -x "${old_binary}"
node "${driver}" \
  --executable "${old_binary}" \
  --user-data-dir "${user_data}" \
  --channel-manifest "${channel_manifest}" \
  --expected-version "${expected_version}" \
  --mode native

# quitAndInstall is asynchronous after the old client exits. Require both the
# on-disk swap and the updater-requested relaunch, not merely a downloaded ZIP.
deadline=$((SECONDS + 240))
actual_version=""
relaunch_pid=""
while (( SECONDS < deadline )); do
  if [[ -f "${installed_app}/Contents/Info.plist" ]]; then
    actual_version="$(/usr/libexec/PlistBuddy \
      -c 'Print :CFBundleShortVersionString' \
      "${installed_app}/Contents/Info.plist" 2>/dev/null || true)"
  fi
  relaunch_pid="$(pgrep -f '^/Applications/OpenSquilla[.]app/Contents/MacOS/OpenSquilla( |$)' | head -1 || true)"
  if [[ "${actual_version}" == "${expected_version}" && -n "${relaunch_pid}" ]]; then
    break
  fi
  sleep 1
done
if [[ "${actual_version}" != "${expected_version}" ]]; then
  echo "Official v0.5.3 updater did not install ${expected_version}" >&2
  exit 1
fi
if [[ -z "${relaunch_pid}" ]]; then
  echo "Official v0.5.3 updater installed but did not automatically relaunch" >&2
  exit 1
fi
stop_installed_app
wait_for_no_installed_processes

candidate_runtime="${installed_app}/Contents/Resources/runtime"
test ! -e "${candidate_runtime}/developer"
test -f "${candidate_runtime}/runtime-manifest.json"
test -f "${candidate_runtime}/runtime-pack-catalog.json"
python3 "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

# Once the updater proof is complete, boot the slim client without any Runtime
# Pack and with network-backed recovery disabled. Runtime Pack state must not be
# a prerequisite for either the Desktop shell or the Gateway.
candidate_binary="${installed_app}/Contents/MacOS/OpenSquilla"
OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE=1 \
OPENSQUILLA_RECOVERY_OFFLINE=1 \
  "${candidate_binary}" --use-mock-keychain "--user-data-dir=${user_data}" \
  >"${sandbox}/candidate-offline.log" 2>&1 &
candidate_pid=$!
sleep 8
kill -0 "${candidate_pid}"
kill "${candidate_pid}" || true
wait "${candidate_pid}" || true
wait_for_no_installed_processes

gateway_binary="$(find \
  "${candidate_runtime}/gateway" \
  -type f -name opensquilla-gateway -perm -111 -print -quit)"
test -x "${gateway_binary}"
OPENSQUILLA_RECOVERY_OFFLINE=1 "${gateway_binary}" recovery inspect \
  --home "${profile}" --json >"${sandbox}/candidate-inspect.json"
python3 - "${profile}" "${sandbox}/candidate-inspect.json" <<'PY'
import json
from pathlib import Path
import sys

home = Path(sys.argv[1]).resolve()
report = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert report["outcome"] in {"ready", "attention"}, report
assert Path(report["primary_home"]).resolve() == home, report
assert Path(report["effective_workspace"]).resolve() == home / "workspace", report
configured = [
    item
    for item in report["candidates"]
    if item["kind"] == "state" and item["configured"] and item["valid"]
]
assert len(configured) == 1, report
assert Path(configured[0]["path"]).resolve() == home / "state", report
PY
python3 "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"

remove_installed_app
test ! -e "${installed_app}"
python3 "${probe}" verify --home "${profile}" --label "${label}" \
  --external-root "${external_sentinels}"
