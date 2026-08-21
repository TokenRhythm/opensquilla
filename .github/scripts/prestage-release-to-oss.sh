#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 RELEASE_ASSETS_DIR TAG" >&2
  exit 2
fi

assets_dir="$(cd "$1" && pwd)"
tag="$2"
if [[ ! "${tag}" =~ ^v([0-9]+)[.]([0-9]+)[.]([0-9]+)((a|b|rc)[0-9]+)?$ ]]; then
  echo "release tag must be canonical (for example v0.5.4 or v0.5.4rc1): ${tag}" >&2
  exit 2
fi

: "${ALIYUN_OSS_BUCKET:?ALIYUN_OSS_BUCKET is required}"
: "${ALIYUN_OSS_PREFIX_NORMALIZED:?ALIYUN_OSS_PREFIX_NORMALIZED is required}"
: "${OSS_ADDRESSING_STYLE_NORMALIZED:?OSS_ADDRESSING_STYLE_NORMALIZED is required}"

if [[ "${ALIYUN_OSS_PREFIX_NORMALIZED}" != "releases" ]]; then
  echo "Draft updater assets must be prestaged under the canonical releases prefix" >&2
  exit 2
fi

version="${tag#v}"
python_bin="${PYTHON_BIN:-python3}"
ossutil_bin="${OSSUTIL_BIN:-ossutil}"
desktop_version="$("${python_bin}" - "${version}" <<'PY'
import re
import sys

print(re.sub(r"(?<=\d)(a|b|rc)(\d+)$", r"-\1\2", sys.argv[1]))
PY
)"
expected=(
  "OpenSquilla-${desktop_version}-mac-arm64.dmg"
  "OpenSquilla-${desktop_version}-mac-arm64.zip"
  "OpenSquilla-${desktop_version}-mac-arm64.dmg.blockmap"
  "OpenSquilla-${desktop_version}-mac-arm64.zip.blockmap"
  "latest-mac.yml"
  "OpenSquilla-${desktop_version}-win-x64.exe"
  "OpenSquilla-${desktop_version}-win-x64.exe.blockmap"
  "latest.yml"
  "opensquilla-${version}-py3-none-any.whl"
  "SHA256SUMS"
)

"${python_bin}" - "${assets_dir}/SHA256SUMS" "${expected[@]}" <<'PY'
from pathlib import Path, PurePosixPath
import re
import sys

checksum_path = Path(sys.argv[1])
expected_all = set(sys.argv[2:])
actual = {path.name for path in checksum_path.parent.iterdir() if path.is_file()}
if actual != expected_all:
    raise SystemExit(
        "Draft release asset set is not exact; refusing OSS prestage: "
        f"missing={sorted(expected_all - actual)}, unexpected={sorted(actual - expected_all)}"
    )
expected = expected_all - {"SHA256SUMS"}
seen: set[str] = set()
for line in checksum_path.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\\0]+)", line)
    if match is None:
        raise SystemExit(f"malformed SHA256SUMS line: {line!r}")
    name = match.group(2)
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or name in {".", ".."}:
        raise SystemExit(f"unsafe SHA256SUMS filename: {name!r}")
    if name in seen:
        raise SystemExit(f"duplicate SHA256SUMS filename: {name}")
    seen.add(name)
if seen != expected:
    raise SystemExit(
        f"SHA256SUMS asset set mismatch: missing={sorted(expected - seen)}, "
        f"unexpected={sorted(seen - expected)}"
    )
PY
(
  cd "${assets_dir}"
  sha256sum --strict -c SHA256SUMS
)

dest_prefix="oss://${ALIYUN_OSS_BUCKET}/${ALIYUN_OSS_PREFIX_NORMALIZED}/${tag}"
versioned_cache_control="public,max-age=31536000,immutable"
scratch="$(mktemp -d)"
cleanup() {
  rm -rf -- "${scratch}"
}
trap cleanup EXIT

object_exists() {
  local object="$1"
  local listing
  if ! listing="$("${ossutil_bin}" ls \
    --addressing-style "${OSS_ADDRESSING_STYLE_NORMALIZED}" \
    --short-format "${object}")"; then
    echo "Unable to list OSS object: ${object}" >&2
    return 2
  fi
  grep -Fxq "${object}" <<<"${listing}"
}

verify_immutable_object() {
  local source="$1"
  local name="$2"
  local object="$3"
  local remote_copy="${scratch}/${name}"
  "${ossutil_bin}" cp \
    --force \
    --addressing-style "${OSS_ADDRESSING_STYLE_NORMALIZED}" \
    "${object}" "${remote_copy}"
  local local_digest remote_digest
  local_digest="$(sha256sum -- "${source}" | awk '{print $1}')"
  remote_digest="$(sha256sum -- "${remote_copy}" | awk '{print $1}')"
  if [[ "${local_digest}" != "${remote_digest}" ]]; then
    echo "Refusing to replace immutable OSS Draft asset: ${object}" >&2
    echo "  local SHA256:  ${local_digest}" >&2
    echo "  remote SHA256: ${remote_digest}" >&2
    echo "Use a new application version/tag for corrected bytes." >&2
    return 1
  fi
}

put_immutable_object() {
  local source="$1"
  local name="$2"
  local source_path
  source_path="$(realpath -- "${source}")"
  "${ossutil_bin}" api put-object \
    --addressing-style "${OSS_ADDRESSING_STYLE_NORMALIZED}" \
    --bucket "${ALIYUN_OSS_BUCKET}" \
    --key "${ALIYUN_OSS_PREFIX_NORMALIZED}/${tag}/${name}" \
    --forbid-overwrite true \
    --body "file://${source_path}" \
    --cache-control "${versioned_cache_control}"
}

upload_immutable_object() {
  local source="$1"
  local name="$2"
  local object="${dest_prefix}/${name}"
  if object_exists "${object}"; then
    verify_immutable_object "${source}" "${name}" "${object}"
    echo "Immutable OSS Draft asset already matches: ${object}"
    return 0
  else
    local exists_status="$?"
    if (( exists_status != 1 )); then
      echo "Refusing to upload after an OSS existence-check error" >&2
      return "${exists_status}"
    fi
  fi
  if ! put_immutable_object "${source}" "${name}"; then
    if object_exists "${object}" \
      && verify_immutable_object "${source}" "${name}" "${object}"; then
      echo "Concurrent immutable OSS Draft asset already matches: ${object}"
      return 0
    fi
    echo "Immutable OSS Draft asset upload failed: ${object}" >&2
    return 1
  fi
  verify_immutable_object "${source}" "${name}" "${object}"
}

for name in "${expected[@]}"; do
  upload_immutable_object "${assets_dir}/${name}" "${name}"
done

# This script deliberately has no channel/latest alias code. The stable/latest
# manifests remain untouched until the human publishes the verified Draft and
# the separate post-publish mirror workflow runs.
"${ossutil_bin}" ls \
  --addressing-style "${OSS_ADDRESSING_STYLE_NORMALIZED}" \
  "${dest_prefix}/"
