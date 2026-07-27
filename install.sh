#!/usr/bin/env bash
# install.sh - OpenSquilla release installer for Linux and macOS.
#
# This script is safe to pipe from the public install URL. It installs uv if
# needed, installs a release wheel with uv tool, then prints the explicit next
# steps. It does not run onboarding or start the gateway.

set -euo pipefail

default_version="v0.5.0"
repo_slug="${OPENSQUILLA_REPOSITORY:-opensquilla/opensquilla}"
python_version="${OPENSQUILLA_PYTHON_VERSION:-3.12}"
original_path="${PATH:-}"

cli_version=""
cli_profile=""
cli_extras=""

usage() {
    cat <<HELP
Usage: bash install.sh [--version v0.5.0|latest] [--profile recommended|core] [--extras name[,name]]

Environment equivalents:
  OPENSQUILLA_VERSION=v0.5.0
  OPENSQUILLA_INSTALL_PROFILE=recommended|core
  OPENSQUILLA_INSTALL_EXTRAS=matrix
  OPENSQUILLA_INSTALL_DRY_RUN=1
HELP
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            cli_version="${2:?install.sh: --version requires a value}"
            shift 2
            ;;
        --version=*)
            cli_version="${1#*=}"
            shift
            ;;
        --profile)
            cli_profile="${2:?install.sh: --profile requires a value}"
            shift 2
            ;;
        --profile=*)
            cli_profile="${1#*=}"
            shift
            ;;
        --extras)
            cli_extras="${2:?install.sh: --extras requires a value}"
            shift 2
            ;;
        --extras=*)
            cli_extras="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: unknown argument '$1'." >&2
            usage >&2
            exit 1
            ;;
    esac
done

release_selector="${cli_version:-${OPENSQUILLA_VERSION:-${default_version}}}"
profile="${cli_profile:-${OPENSQUILLA_INSTALL_PROFILE:-recommended}}"
dry_run="${OPENSQUILLA_INSTALL_DRY_RUN:-0}"

is_release_version() {
    [[ "$1" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]]
}

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
for extra in ${raw_extras[@]+"${raw_extras[@]}"}; do
    [[ -n "${extra}" ]] || continue
    if [[ "${valid_extras}" != *" ${extra} "* ]]; then
        echo "install.sh: unsupported extra '${extra}'." >&2
        echo "install.sh: supported extras:${valid_extras}" >&2
        exit 1
    fi
    duplicate=0
    for existing in ${install_extras[@]+"${install_extras[@]}"}; do
        if [[ "${existing}" == "${extra}" ]]; then
            duplicate=1
            break
        fi
    done
    if [[ "${duplicate}" -eq 0 ]]; then
        install_extras+=("${extra}")
    fi
done

case "${profile}" in
    core|minimal)
        profile="core"
        target_extras=()
        ;;
    recommended)
        target_extras=(recommended)
        ;;
    *)
        echo "install.sh: unsupported OPENSQUILLA_INSTALL_PROFILE='${profile}'." >&2
        echo "install.sh: supported profiles: core, recommended" >&2
        exit 1
        ;;
esac

if (( ${#install_extras[@]} > 0 )); then
    target_extras+=("${install_extras[@]}")
fi

if (( ${#target_extras[@]} > 0 )); then
    package_name="opensquilla[$(IFS=,; echo "${target_extras[*]}")]"
else
    package_name="opensquilla"
fi

if [[ "${release_selector}" != "latest" && "${release_selector}" != "stable" ]] && ! is_release_version "${release_selector}"; then
    echo "install.sh: unsupported OPENSQUILLA_VERSION='${release_selector}'." >&2
    echo "install.sh: the release installer only supports latest, stable, or release versions like v0.5.0." >&2
    echo "install.sh: use git clone plus scripts/install_source.sh for main, dev, branch, or source installs." >&2
    exit 1
fi

case "${release_selector}" in
    latest|stable)
        latest_tag=""
        if command -v curl >/dev/null 2>&1; then
            latest_tag="$(
                curl -fsSL "https://api.github.com/repos/${repo_slug}/releases/latest" \
                    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                    | head -n 1
            )"
        elif command -v wget >/dev/null 2>&1; then
            latest_tag="$(
                wget -qO- "https://api.github.com/repos/${repo_slug}/releases/latest" \
                    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                    | head -n 1
            )"
        else
            echo "install.sh: curl or wget is required to resolve the latest release." >&2
            exit 1
        fi
        if ! is_release_version "${latest_tag}"; then
            echo "install.sh: failed to resolve latest release tag for ${repo_slug}." >&2
            exit 1
        fi
        release_version="${latest_tag#v}"
        wheel_url="https://github.com/${repo_slug}/releases/download/${latest_tag}/opensquilla-${release_version}-py3-none-any.whl"
        display_version="${latest_tag}"
        ;;
    v*)
        release_version="${release_selector#v}"
        wheel_url="https://github.com/${repo_slug}/releases/download/${release_selector}/opensquilla-${release_version}-py3-none-any.whl"
        display_version="${release_selector}"
        ;;
    *)
        release_version="${release_selector}"
        release_tag="v${release_version}"
        wheel_url="https://github.com/${repo_slug}/releases/download/${release_tag}/opensquilla-${release_version}-py3-none-any.whl"
        display_version="${release_tag}"
        ;;
esac

install_spec="${package_name} @ ${wheel_url}"

install_uv() {
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "install.sh: curl or wget is required to install uv." >&2
        exit 1
    fi
}

resolve_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    if [[ -f "${HOME}/.local/bin/env" ]]; then
        # shellcheck disable=SC1091
        . "${HOME}/.local/bin/env"
    fi
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH:-}"
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    return 1
}

if [[ "${dry_run}" == "1" ]]; then
    echo "install.sh: dry-run - would install OpenSquilla ${display_version}"
    echo "install.sh: dry-run - would run: uv tool install --python ${python_version} --force --reinstall-package opensquilla \"${install_spec}\""
    exit 0
fi

uv_bin="$(resolve_uv || true)"
if [[ -z "${uv_bin}" ]]; then
    echo "install.sh: uv not found; installing uv first."
    install_uv
    uv_bin="$(resolve_uv || true)"
fi

if [[ -z "${uv_bin}" ]]; then
    echo "install.sh: uv was not found after installation." >&2
    echo "install.sh: restart your terminal or run '. \"\$HOME/.local/bin/env\"', then retry." >&2
    exit 1
fi

echo "install.sh: installing OpenSquilla ${display_version} (${profile})"
"${uv_bin}" tool install --python "${python_version}" --force --reinstall-package opensquilla "${install_spec}"

tool_bin_dir="$("${uv_bin}" tool dir --bin 2>/dev/null || true)"

# Write an install receipt to aid `opensquilla uninstall`. Best-effort: never
# fail the install if this cannot be written.
write_install_receipt() {
    home="${OPENSQUILLA_STATE_DIR:-${HOME}/.opensquilla}"
    receipt="${home}/install-receipt.json"
    tool_dir="$("${uv_bin}" tool dir 2>/dev/null || true)"
    installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
    mkdir -p "${home}" 2>/dev/null || return 0
    cat >"${receipt}" 2>/dev/null <<RECEIPT || return 0
{
  "version": 1,
  "install_method": "uv-tool",
  "installed_at": "${installed_at}",
  "entrypoints": ["${tool_bin_dir}/opensquilla", "${tool_bin_dir}/gateway"],
  "owned_paths": ["${tool_dir}/opensquilla", "${tool_bin_dir}/opensquilla", "${tool_bin_dir}/gateway"],
  "data_root": "${home}"
}
RECEIPT
    chmod 600 "${receipt}" 2>/dev/null || true
}
write_install_receipt || true

cat <<DONE
----------------------------------------------------------------------------
OpenSquilla installed from ${display_version}.

Next steps:
  opensquilla onboard
  opensquilla gateway run

Default gateway bind: 127.0.0.1:18791 (loopback only).
Do not expose the gateway on 0.0.0.0 unless it is behind a trusted reverse
proxy or VPN.
----------------------------------------------------------------------------
DONE

if [[ -n "${tool_bin_dir}" && ":${original_path}:" != *":${tool_bin_dir}:"* ]]; then
    cat <<PATHNOTE

PATH note:
  Your current shell may not find 'opensquilla' until PATH is refreshed.
  Run one of these, then retry the next steps:

    . "\$HOME/.local/bin/env"
    # or open a new terminal

PATHNOTE
fi
