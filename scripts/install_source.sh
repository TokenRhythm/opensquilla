#!/usr/bin/env bash
# install_source.sh - user-local OpenSquilla installer (no sudo).
#
# Installer contract:
#   - installs into a user-owned prefix (never /usr/local, /opt, or admin paths)
#   - prefers uv tool install; falls back to pip --user; errors clearly if neither exists
#   - requires the Node.js version pinned by opensquilla-webui/.node-version,
#     runs npm ci + npm run build, and packages that exact Web UI
#   - defaults to the "recommended" runtime profile (memory + bundled v4 router)
#     and allows `OPENSQUILLA_INSTALL_PROFILE=core` to opt back down
#   - prints a post-install banner documenting the default bind
#     (127.0.0.1:18791) and the explicit opt-in required to expose the gateway
#     on the network (--listen 0.0.0.0 or OPENSQUILLA_LISTEN=0.0.0.0)
#   - adds an extra WARNING when the operator requested network exposure at
#     install time via OPENSQUILLA_LISTEN=0.0.0.0
#
# Dry-run: export OPENSQUILLA_INSTALL_DRY_RUN=1 to print the install plan + banner
# without touching the system.

set -euo pipefail

cli_profile=""
cli_extras=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            cli_profile="${2:?install_source.sh: --profile requires a value}"
            shift 2
            ;;
        --profile=*)
            cli_profile="${1#*=}"
            shift
            ;;
        --extras)
            cli_extras="${2:?install_source.sh: --extras requires a value}"
            shift 2
            ;;
        --extras=*)
            cli_extras="${1#*=}"
            shift
            ;;
        -h|--help)
            cat <<HELP
Usage: bash scripts/install_source.sh [--profile recommended|core] [--extras name[,name]]

Environment equivalents:
  OPENSQUILLA_INSTALL_PROFILE=recommended|core
  OPENSQUILLA_INSTALL_EXTRAS=matrix
  OPENSQUILLA_INSTALL_DRY_RUN=1
HELP
            exit 0
            ;;
        *)
            echo "install_source.sh: unknown argument '$1'." >&2
            echo "install_source.sh: run 'bash scripts/install_source.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

# --- prefix resolution ------------------------------------------------------

if [[ -n "${OPENSQUILLA_PREFIX:-}" ]]; then
    prefix="${OPENSQUILLA_PREFIX}"
elif [[ -n "${XDG_DATA_HOME:-}" ]]; then
    prefix="${XDG_DATA_HOME}/opensquilla"
else
    prefix="${HOME}/.local"
fi

dry_run="${OPENSQUILLA_INSTALL_DRY_RUN:-0}"
profile="${cli_profile:-${OPENSQUILLA_INSTALL_PROFILE:-recommended}}"
webui_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/opensquilla-webui"
node_version_file="${webui_dir}/.node-version"
if [[ ! -f "${node_version_file}" ]]; then
    echo "install_source.sh: required Node.js version file is missing: ${node_version_file}" >&2
    exit 1
fi
minimum_node_version="$(tr -d '[:space:]' < "${node_version_file}")"
if [[ -z "${minimum_node_version}" ]]; then
    echo "install_source.sh: required Node.js version file is empty: ${node_version_file}" >&2
    exit 1
fi

valid_extras=" matrix matrix-e2e document-extras "
extras_csv="${OPENSQUILLA_INSTALL_EXTRAS:-}"
if [[ -n "${cli_extras}" ]]; then
    extras_csv="${extras_csv}${extras_csv:+,}${cli_extras}"
fi
extras_csv="${extras_csv// /,}"
raw_extras=()
if [[ -n "${extras_csv}" ]]; then
    IFS=',' read -r -a raw_extras <<< "${extras_csv}"
fi
install_extras=()
if (( ${#raw_extras[@]} > 0 )); then
    for extra in "${raw_extras[@]}"; do
        [[ -n "${extra}" ]] || continue
        if [[ "${valid_extras}" != *" ${extra} "* ]]; then
            echo "install_source.sh: unsupported extra '${extra}'." >&2
            echo "install_source.sh: supported extras:${valid_extras}" >&2
            exit 1
        fi
        duplicate=0
        if (( ${#install_extras[@]} > 0 )); then
            for existing in "${install_extras[@]}"; do
                if [[ "${existing}" == "${extra}" ]]; then
                    duplicate=1
                    break
                fi
            done
        fi
        if [[ "${duplicate}" -eq 0 ]]; then
            install_extras+=("${extra}")
        fi
    done
fi

case "${profile}" in
    core|minimal)
        profile="core"
        target_extras=()
        ;;
    recommended)
        target_extras=(recommended)
        ;;
    *)
        echo "install_source.sh: unsupported OPENSQUILLA_INSTALL_PROFILE='${profile}'." >&2
        echo "install_source.sh: supported profiles: core, recommended" >&2
        exit 1
        ;;
esac
if (( ${#install_extras[@]} > 0 )); then
    target_extras+=("${install_extras[@]}")
fi
if (( ${#target_extras[@]} > 0 )); then
    joined_extras="$(IFS=,; echo "${target_extras[*]}")"
    install_target=".[${joined_extras}]"
else
    install_target="."
fi

check_squilla_router_assets() {
    local mode="${1:-strict}"
    if [[ "${profile}" != "recommended" ]]; then
        return 0
    fi

    local model_root="src/opensquilla/squilla_router/models"
    local pointer_line="version https://git-lfs.github.com/spec/v1"
    local required=(
        "${model_root}/v4.2_phase3_inference/lgbm_main.bin"
        "${model_root}/v4.2_phase3_inference/router.runtime.yaml"
        "${model_root}/v4.2_phase3_inference/mlp/model.onnx"
        "${model_root}/v4.2_phase3_inference/features/tfidf.pkl"
        "${model_root}/v4.2_phase3_inference/bge_onnx/model.onnx"
    )
    local missing=()
    local pointers=()
    local path=""
    for path in "${required[@]}"; do
        if [[ ! -f "${path}" ]]; then
            missing+=("${path}")
            continue
        fi
        if LC_ALL=C grep -q -m 1 -F -x "${pointer_line}" "${path}" 2>/dev/null; then
            pointers+=("${path}")
        fi
    done
    if (( ${#missing[@]} > 0 || ${#pointers[@]} > 0 )); then
        if [[ "${mode}" == "warn" ]]; then
            echo "install_source.sh: dry-run note — real recommended install would fail until bundled squilla-router v4 assets are available in this checkout." >&2
        else
            echo "install_source.sh: bundled squilla-router v4 assets are unavailable in this checkout." >&2
        fi
        if (( ${#missing[@]} > 0 )); then
            echo "install_source.sh: missing assets: ${missing[*]}" >&2
        fi
        if (( ${#pointers[@]} > 0 )); then
            echo "install_source.sh: Git LFS pointer files detected: ${pointers[*]}" >&2
        fi
        echo 'install_source.sh: run `git lfs install` once, then:' >&2
        echo 'install_source.sh:   git lfs pull --include="src/opensquilla/squilla_router/models/**"' >&2
        echo 'install_source.sh: or retry with OPENSQUILLA_INSTALL_PROFILE=core for the minimal runtime.' >&2
        if [[ "${mode}" == "warn" ]]; then
            return 0
        fi
        exit 1
    fi
}

build_webui() {
    if ! command -v node >/dev/null 2>&1; then
        echo "install_source.sh: Node.js >= ${minimum_node_version} is required to build the Web UI from source." >&2
        echo "install_source.sh: install Node.js, or use an official wheel/Desktop installer (no Node.js required)." >&2
        exit 1
    fi
    if ! node -e '
        const installed = process.versions.node.split(".").map(Number);
        const required = process.argv[1].replace(/^v/, "").split(".").map(Number);
        let comparison = 0;
        for (const index of [0, 1, 2]) {
          comparison = (installed[index] ?? 0) - (required[index] ?? 0);
          if (comparison !== 0) break;
        }
        process.exit(comparison >= 0 ? 0 : 1);
    ' "${minimum_node_version}"; then
        echo "install_source.sh: Node.js >= ${minimum_node_version} is required; found $(node --version 2>/dev/null || echo unknown)." >&2
        echo "install_source.sh: upgrade Node.js, or use an official wheel/Desktop installer (no Node.js required)." >&2
        exit 1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "install_source.sh: npm is required to build the Web UI from source." >&2
        echo "install_source.sh: install npm, or use an official wheel/Desktop installer (no npm required)." >&2
        exit 1
    fi
    if [[ ! -f "${webui_dir}/package-lock.json" ]]; then
        echo "install_source.sh: Web UI package lock is missing: ${webui_dir}/package-lock.json" >&2
        exit 1
    fi

    echo "install_source.sh: installing locked Web UI dependencies (npm ci)"
    (
        cd "${webui_dir}"
        npm ci
        npm run build
    )
}

# --- installer selection ----------------------------------------------------

installer=""
install_args=()
if command -v uv >/dev/null 2>&1; then
    installer="uv"
    install_args=(uv tool install --python 3.12 --force --reinstall-package opensquilla "${install_target}")
elif command -v python3 >/dev/null 2>&1 \
    && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    installer="pip"
    install_args=(python3 -m pip install --user "${install_target}")
else
    # No uv, and the ambient python3 is missing or older than 3.12. Do NOT
    # silently pip-install onto an unsupported interpreter: that leaves a
    # broken `opensquilla` on PATH and makes coding mode fall back to manual
    # edits. Fail loud and point at uv, which provisions its own 3.12.
    if command -v python3 >/dev/null 2>&1; then
        _ambient_py="$(python3 -V 2>&1)"
    else
        _ambient_py="none"
    fi
    echo "install_source.sh: cannot install - uv not found and python3 (${_ambient_py}) is older than 3.12." >&2
    echo "install_source.sh: OpenSquilla requires Python >= 3.12 (pyproject 'requires-python')." >&2
    echo "install_source.sh: easiest fix - install uv; it brings its own 3.12, no system Python needed:" >&2
    echo "install_source.sh:   curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "install_source.sh: then re-run: bash scripts/install_source.sh" >&2
    exit 1
fi
install_cmd="${install_args[*]}"

# --- banner -----------------------------------------------------------------

print_banner() {
    cat <<BANNER
----------------------------------------------------------------------------
OpenSquilla installed via ${installer} -> ${prefix} (profile: ${profile})
Extras: $(if (( ${#install_extras[@]} > 0 )); then IFS=,; echo "${install_extras[*]}"; else echo "none"; fi)

Default gateway bind: 127.0.0.1:18791 (loopback only)
Network exposure is opt-in only. To expose the gateway on the network you
must use one of:
  - CLI flag:  opensquilla gateway run --listen 0.0.0.0
  - Env var:   OPENSQUILLA_LISTEN=0.0.0.0 opensquilla gateway run

Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN. The
gateway's first-class auth assumes loopback-scope by default.
----------------------------------------------------------------------------
BANNER
}

print_listen_warning() {
    cat <<WARNING
WARNING: you have selected network-exposed default - ensure you
   understand the blast radius. The gateway will bind to 0.0.0.0 and be
   reachable from every interface on this host.
WARNING
}

verify_install() {
    # Catch a broken/partial install now, not mid-task. A non-runnable
    # code-task is exactly what makes coding mode silently degrade.
    # Prefer the JUST-installed binary over any stale `opensquilla` earlier
    # on PATH (uv tool / pip --user land outside the default PATH).
    local bin=""
    if [[ "${installer}" == "uv" ]]; then
        local uv_bin
        uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
        [[ -n "${uv_bin}" && -x "${uv_bin}/opensquilla" ]] && bin="${uv_bin}/opensquilla"
    fi
    if [[ -z "${bin}" && -x "${HOME}/.local/bin/opensquilla" ]]; then
        bin="${HOME}/.local/bin/opensquilla"
    fi
    if [[ -z "${bin}" ]] && command -v opensquilla >/dev/null 2>&1; then
        bin="opensquilla"
    fi
    # Coding mode requires `opensquilla code-task`, so verify THAT, not just --version.
    if [[ -n "${bin}" ]] && "${bin}" code-task --help >/dev/null 2>&1; then
        echo "install_source.sh: verified - 'opensquilla code-task' is runnable"
    else
        echo "install_source.sh: WARNING - 'opensquilla code-task' is not runnable yet." >&2
        echo "install_source.sh: run 'uv tool update-shell' (or open a new shell), then: opensquilla code-task --help" >&2
    fi
    command -v git  >/dev/null 2>&1 || echo "install_source.sh: WARNING - 'git' not found; code-task cannot clone repositories without it." >&2
    command -v node >/dev/null 2>&1 || echo "install_source.sh: WARNING - 'node' is no longer available; future source installs, Web UI rebuilds, and code-task build-mode apps require it." >&2
}

if [[ "${dry_run}" = "1" ]]; then
    echo "install_source.sh: dry-run — would require Node.js >= ${minimum_node_version} and npm"
    echo "install_source.sh: dry-run — would run in ${webui_dir}: npm ci"
    echo "install_source.sh: dry-run — would run in ${webui_dir}: npm run build"
    echo "install_source.sh: dry-run — would run: ${install_cmd}"
    echo "install_source.sh: dry-run — prefix: ${prefix}"
    check_squilla_router_assets warn
    print_banner
    if [[ "${OPENSQUILLA_LISTEN:-}" = "0.0.0.0" ]]; then
        print_listen_warning
    fi
    exit 0
fi

# --- execute ---------------------------------------------------------------

check_squilla_router_assets
build_webui

echo "install_source.sh: installing via ${installer} into prefix ${prefix}"
echo "install_source.sh: running: ${install_cmd}"
"${install_args[@]}"

verify_install

# Write an install receipt to aid `opensquilla uninstall`. Best-effort.
write_install_receipt() {
    home="${OPENSQUILLA_STATE_DIR:-${HOME}/.opensquilla}"
    receipt="${home}/install-receipt.json"
    installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
    if [[ "${installer}" == "uv" ]]; then
        method="uv-tool"
    else
        method="pip"
    fi
    mkdir -p "${home}" 2>/dev/null || return 0
    cat >"${receipt}" 2>/dev/null <<RECEIPT || return 0
{
  "version": 1,
  "install_method": "${method}",
  "installed_at": "${installed_at}",
  "entrypoints": [],
  "owned_paths": [],
  "data_root": "${home}"
}
RECEIPT
    chmod 600 "${receipt}" 2>/dev/null || true
}
write_install_receipt || true

print_banner
if [[ "${OPENSQUILLA_LISTEN:-}" = "0.0.0.0" ]]; then
    print_listen_warning
fi
