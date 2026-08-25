#!/usr/bin/env bash
set -euo pipefail

changed_files="${1:?usage: classify-ci-changes.sh <changed-files-list>}"
output_file="${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"

docs_only=true
runtime_changed=false
test_changed=false
ci_changed=false
dependency_changed=false
release_changed=false
windows_full_required=false
frontend_changed=false
tui_changed=false
desktop_changed=false
python_changed=false
python_full_required=false
platform_sensitive_changed=false
build_wheel_required=false
toolchain_artifact_changed=false
full_required=false
pytest_targets=""
seen_file=false

add_pytest_target() {
  local target="${1:?pytest target is required}"
  case ",${pytest_targets}," in
    *",${target},"*) ;;
    *)
      if [[ -n "${pytest_targets}" ]]; then
        pytest_targets+=","
      fi
      pytest_targets+="${target}"
      ;;
  esac
}

mark_non_docs_changed() {
  docs_only=false
}

mark_runtime_changed() {
  mark_non_docs_changed
  runtime_changed=true
  python_changed=true
  build_wheel_required=true
}

mark_python_full_required() {
  mark_runtime_changed
  python_full_required=true
}

mark_test_changed() {
  mark_non_docs_changed
  test_changed=true
  python_changed=true
}

mark_ci_changed() {
  mark_non_docs_changed
  ci_changed=true
  python_changed=true
}

mark_dependency_changed() {
  mark_runtime_changed
  dependency_changed=true
  release_changed=true
  windows_full_required=true
}

mark_release_changed() {
  mark_non_docs_changed
  release_changed=true
  windows_full_required=true
}

mark_frontend_changed() {
  # WebUI source becomes runtime wheel content, but it is neither Python nor
  # platform-native code. Build the exact wheel without waking unrelated
  # Python/Windows jobs for a frontend-only pull request.
  mark_non_docs_changed
  runtime_changed=true
  build_wheel_required=true
  frontend_changed=true
}

mark_tui_changed() {
  mark_runtime_changed
  tui_changed=true
}

mark_desktop_changed() {
  mark_non_docs_changed
  desktop_changed=true
}

mark_platform_sensitive_changed() {
  mark_non_docs_changed
  platform_sensitive_changed=true
  windows_full_required=true
}

mark_toolchain_artifact_changed() {
  mark_non_docs_changed
  toolchain_artifact_changed=true
}

mark_full_required() {
  docs_only=false
  runtime_changed=true
  test_changed=true
  ci_changed=true
  dependency_changed=true
  release_changed=true
  windows_full_required=true
  frontend_changed=true
  tui_changed=true
  desktop_changed=true
  python_changed=true
  python_full_required=true
  platform_sensitive_changed=true
  build_wheel_required=true
  toolchain_artifact_changed=true
  full_required=true
  pytest_targets="tests"
}

while IFS= read -r path || [[ -n "${path}" ]]; do
  path="${path%$'\r'}"
  [[ -z "${path}" ]] && continue
  seen_file=true

  case "${path}" in
    .ci/run-all)
      mark_full_required
      ;;
    pyproject.toml | uv.lock)
      mark_dependency_changed
      mark_toolchain_artifact_changed
      ;;
    opensquilla-webui/*)
      mark_frontend_changed
      ;;
    src/opensquilla/gateway/static/dist/*)
      # Generated WebUI files are forbidden in Git. Route a force-added file to
      # the frontend job, whose tracked-artifact guard reports the violation.
      mark_frontend_changed
      ;;
    src/opensquilla/cli/tui/opentui/package/* | packages/opensquilla-tui-host/* | scripts/build_tui_host_companion.py | scripts/smoke_tui_host_companion.py)
      mark_tui_changed
      ;;
    .github/workflows/ci.yml | .github/scripts/classify-ci-changes.sh | .github/scripts/check_ci_results.py | .github/scripts/windows_test_shards.py)
      # Changes to the gate itself must exercise every path it can suppress.
      mark_full_required
      ;;
    .github/scripts/windows_test_durations.json)
      # Historical timing data is scheduling metadata, not executable policy.
      mark_non_docs_changed
      ;;
    .github/scripts/windows_test_assignments.json)
      # Run every Windows shard; the runner validates duplicate-free inventory coverage.
      mark_non_docs_changed
      windows_full_required=true
      ;;
    .github/workflows/wheelhouse-release.yml)
      mark_ci_changed
      mark_release_changed
      ;;
    .github/workflows/*)
      mark_full_required
      ;;
    .github/scripts/verify-release-profile-preservation.py)
      mark_ci_changed
      mark_release_changed
      mark_platform_sensitive_changed
      ;;
    .github/scripts/*)
      mark_full_required
      ;;
    scripts/validate_managed_toolchain_artifacts.py | scripts/validate_managed_toolchain_artifacts_stdlib.py)
      mark_runtime_changed
      mark_platform_sensitive_changed
      mark_toolchain_artifact_changed
      ;;
    src/opensquilla/skills/toolchains/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      mark_toolchain_artifact_changed
      ;;
    src/opensquilla/skills/runtime_env.py | src/opensquilla/skills/bundled/meta-paper-write/* | src/opensquilla/skills/bundled/paper-*/* | src/opensquilla/skills/bundled/meta-short-drama/* | src/opensquilla/skills/bundled/subtitle-burner/* | src/opensquilla/skills/bundled/video-still-animator/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      mark_toolchain_artifact_changed
      ;;
    tests/test_skills/test_managed_toolchains.py | tests/test_skills/test_toolchain_runtime_integration.py | tests/test_skills/test_toolchain_state_scope.py | tests/test_skills/test_meta_paper* | tests/test_skills/test_paper_*)
      mark_test_changed
      mark_platform_sensitive_changed
      mark_toolchain_artifact_changed
      ;;
    tests/test_scripts/test_build_wheelhouse_zip.py | tests/test_install_scripts.py | tests/test_root_start_scripts.py | tests/test_release_consistency.py | tests/test_public_release_hygiene.py)
      mark_test_changed
      mark_release_changed
      ;;
    tests/test_tools/test_shell_* | tests/test_tools/test_path_* | tests/test_sandbox/* | tests/test_desktop/* | tests/test_compat/* | tests/test_recovery/* | tests/test_migration/* | tests/test_migrations/* | tests/test_persistence/* | tests/test_session/* | tests/test_scheduler/* | tests/test_uninstall/* | tests/test_packaging/*)
      mark_test_changed
      mark_platform_sensitive_changed
      ;;
    tests/test_onboarding/*)
      mark_test_changed
      add_pytest_target "tests/test_onboarding"
      ;;
    tests/test_provider/* | tests/test_provider*.py)
      mark_test_changed
      add_pytest_target "tests/test_provider"
      add_pytest_target "tests/test_provider*.py"
      ;;
    tests/test_gateway/*)
      mark_test_changed
      add_pytest_target "tests/test_gateway"
      add_pytest_target "tests/test_gateway*.py"
      add_pytest_target "tests/functional/test_gateway_*_e2e.py"
      ;;
    tests/test_engine/* | tests/test_engine*.py)
      mark_test_changed
      add_pytest_target "tests/test_engine"
      add_pytest_target "tests/test_engine*.py"
      ;;
    tests/test_channels/*)
      mark_test_changed
      add_pytest_target "tests/test_channels"
      ;;
    tests/test_memory/* | tests/test_memory*.py)
      mark_test_changed
      add_pytest_target "tests/test_memory"
      add_pytest_target "tests/test_memory*.py"
      ;;
    tests/test_skills/* | tests/test_skills*.py)
      mark_test_changed
      add_pytest_target "tests/test_skills"
      add_pytest_target "tests/test_skills*.py"
      add_pytest_target "tests/test_meta_skill*.py"
      ;;
    tests/test_cli/* | tests/integration/cli/*)
      mark_test_changed
      add_pytest_target "tests/test_cli"
      add_pytest_target "tests/integration/cli"
      ;;
    tests/functional/test_gateway_*_e2e.py)
      mark_test_changed
      mark_platform_sensitive_changed
      add_pytest_target "tests/functional"
      ;;
    tests/*)
      # New or unclassified tests fail closed to the Windows high-risk suite.
      mark_test_changed
      mark_platform_sensitive_changed
      ;;
    scripts/build_wheelhouse_zip.py | scripts/install_source.sh | scripts/install_source.ps1)
      mark_runtime_changed
      mark_release_changed
      ;;
    install.sh | install.ps1 | start.sh | start.ps1 | README.release.md | RELEASES.md)
      mark_release_changed
      ;;
    desktop/electron/scripts/test-packaged-update-policy.mjs)
      mark_desktop_changed
      mark_platform_sensitive_changed
      mark_release_changed
      ;;
    desktop/*)
      mark_platform_sensitive_changed
      mark_desktop_changed
      ;;
    src/opensquilla/uninstall/*)
      mark_runtime_changed
      mark_release_changed
      ;;
    src/opensquilla/recovery/* | src/opensquilla/migration/* | src/opensquilla/persistence/* | src/opensquilla/session/* | src/opensquilla/sandbox/* | src/opensquilla/tools/boundary.py | src/opensquilla/tools/builtin/code_exec.py | src/opensquilla/tools/builtin/filesystem.py | src/opensquilla/tools/builtin/git.py | src/opensquilla/tools/builtin/shell.py | src/opensquilla/tools/builtin/shell_policy.py | src/opensquilla/tools/path_* | src/opensquilla/tools/policy* | src/opensquilla/tools/write_*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      add_pytest_target "tests/test_recovery"
      add_pytest_target "tests/test_migration"
      add_pytest_target "tests/test_migrations"
      add_pytest_target "tests/test_persistence"
      add_pytest_target "tests/test_session"
      add_pytest_target "tests/test_sandbox"
      add_pytest_target "tests/test_tools"
      ;;
    src/opensquilla/onboarding/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      add_pytest_target "tests/test_onboarding"
      ;;
    src/opensquilla/provider/* | src/opensquilla/router_tiers.py)
      mark_runtime_changed
      add_pytest_target "tests/test_provider"
      add_pytest_target "tests/test_provider*.py"
      add_pytest_target "tests/test_*router*.py"
      add_pytest_target "tests/test_cross_provider_tiers.py"
      ;;
    src/opensquilla/squilla_router/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      ;;
    src/opensquilla/gateway/*)
      mark_runtime_changed
      add_pytest_target "tests/test_gateway"
      add_pytest_target "tests/test_gateway*.py"
      add_pytest_target "tests/functional/test_gateway_*_e2e.py"
      ;;
    src/opensquilla/engine/* | src/opensquilla/agents/* | src/opensquilla/agent/* | src/opensquilla/application/* | src/opensquilla/safety/*)
      # These shared-core surfaces fan out across gateway, channel, session,
      # CLI, and tool contracts. Run every offline Python shard without waking
      # unrelated frontend, Desktop, release, or toolchain matrices.
      mark_python_full_required
      ;;
    src/opensquilla/channels/*)
      mark_runtime_changed
      add_pytest_target "tests/test_channels"
      ;;
    src/opensquilla/memory/*)
      mark_runtime_changed
      add_pytest_target "tests/test_memory"
      add_pytest_target "tests/test_memory*.py"
      ;;
    src/opensquilla/scheduler/*)
      mark_runtime_changed
      add_pytest_target "tests/test_scheduler"
      ;;
    src/opensquilla/skills/*)
      mark_runtime_changed
      add_pytest_target "tests/test_skills"
      add_pytest_target "tests/test_skills*.py"
      add_pytest_target "tests/test_meta_skill*.py"
      ;;
    src/opensquilla/cli/*)
      mark_runtime_changed
      add_pytest_target "tests/test_cli"
      add_pytest_target "tests/integration/cli"
      ;;
    src/opensquilla/identity/templates/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      ;;
    src/opensquilla/identity/*)
      mark_runtime_changed
      add_pytest_target "tests/test_identity"
      ;;
    src/opensquilla/mcp/* | src/opensquilla/mcp_server/*)
      mark_runtime_changed
      add_pytest_target "tests/test_mcp"
      add_pytest_target "tests/test_mcp_server"
      ;;
    src/opensquilla/health/*)
      mark_runtime_changed
      add_pytest_target "tests/test_health"
      ;;
    src/opensquilla/observability/*)
      mark_runtime_changed
      add_pytest_target "tests/test_observability"
      ;;
    src/opensquilla/search/*)
      mark_runtime_changed
      add_pytest_target "tests/test_search"
      ;;
    migrations/*)
      mark_runtime_changed
      mark_platform_sensitive_changed
      ;;
    src/* | scripts/*)
      # Unknown runtime surfaces are high risk until explicitly proven otherwise.
      mark_runtime_changed
      mark_platform_sensitive_changed
      ;;
    docs/* | README.md | README.*.md | CHANGELOG.md | CODE_OF_CONDUCT.md | CONTRIBUTING.md | MIGRATION.md | SECURITY.md | SUPPORT.md | THIRD_PARTY_NOTICES.md | META_SKILL_GUIDE.md | .github/pull_request_template.md | .github/ISSUE_TEMPLATE/*)
      ;;
    *)
      # Fail closed for new non-documentation paths.
      mark_runtime_changed
      mark_platform_sensitive_changed
      ;;
  esac
done < "${changed_files}"

if [[ "${seen_file}" == "false" ]]; then
  mark_full_required
fi

# High-risk changes already run the exhaustive Windows matrix. Keep the targeted
# Ubuntu lane compact; full CI still owns the complete cross-platform suite.
if [[ "${full_required}" == "true" ]]; then
  pytest_targets="tests"
elif [[ "${platform_sensitive_changed}" == "true" ]]; then
  pytest_targets=""
fi

{
  printf 'docs_only=%s\n' "${docs_only}"
  printf 'runtime_changed=%s\n' "${runtime_changed}"
  printf 'test_changed=%s\n' "${test_changed}"
  printf 'ci_changed=%s\n' "${ci_changed}"
  printf 'dependency_changed=%s\n' "${dependency_changed}"
  printf 'release_changed=%s\n' "${release_changed}"
  printf 'windows_full_required=%s\n' "${windows_full_required}"
  printf 'frontend_changed=%s\n' "${frontend_changed}"
  printf 'tui_changed=%s\n' "${tui_changed}"
  printf 'desktop_changed=%s\n' "${desktop_changed}"
  printf 'python_changed=%s\n' "${python_changed}"
  printf 'python_full_required=%s\n' "${python_full_required}"
  printf 'platform_sensitive_changed=%s\n' "${platform_sensitive_changed}"
  printf 'build_wheel_required=%s\n' "${build_wheel_required}"
  printf 'toolchain_artifact_changed=%s\n' "${toolchain_artifact_changed}"
  printf 'full_required=%s\n' "${full_required}"
  printf 'pytest_targets=%s\n' "${pytest_targets}"
} >> "${output_file}"
