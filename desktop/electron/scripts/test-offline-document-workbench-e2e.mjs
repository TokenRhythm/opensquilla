import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { runCommandWithTelemetry } from './ci-case-telemetry.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const electronRoot = join(scriptDir, '..')
const repoRoot = join(electronRoot, '..', '..')

async function run(caseName, command, args, options = {}) {
  const result = await runCommandWithTelemetry({
    caseName,
    os: process.env.RUNNER_OS || process.platform,
    shard: process.env.OPENSQUILLA_DESKTOP_E2E_SHARD
      || process.env.CI_E2E_SHARD
      || 'offline-document-workbench',
    attempt: process.env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT
      || process.env.GITHUB_RUN_ATTEMPT
      || '1',
    outputPath: process.env.OPENSQUILLA_CI_CASE_TELEMETRY_PATH,
    command,
    args,
    cwd: options.cwd || repoRoot,
    env: options.env || process.env,
  })
  if (result.exitCode !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with exit code ${result.exitCode}`)
  }
}

// The release gate retains the focused boundary fixtures and adds one complete
// user journey through the real Vue surface, native Electron selection, owned
// Gateway, agent loop, and durable document commit:
//   1. one owned-Gateway WebSocket lifecycle proves the durable resource,
//      EditSession, exact4 agent mutation, and immutable publication contract;
//   2. one real Electron process proves the native preview/selection/editor
//      surface, including keyboard and IME input under an explicit foreground
//      precondition;
//   3. one real Vue-to-Electron-to-Gateway fixture generates and adopts one
//      HTML deliverable, opens its Source directly, commits a manual revision,
//      exits annotation mode after an accepted answer-only turn, commits a
//      document_patch revision, and verifies restart recovery without resource
//      duplication.
// Both fixtures are loopback-only and use synthetic bytes and model replies.
const uv = process.platform === 'win32' ? 'uv.exe' : 'uv'
await run(
  'owned-gateway-html-workbench-lifecycle',
  uv,
  [
    'run',
    'pytest',
    '-q',
    'tests/test_live_artifact_prompt_annotations_e2e.py::test_owned_gateway_html_workbench_lifecycle_is_offline_and_immutable',
  ],
)

await run(
  'native-workbench-v2-electron',
  process.execPath,
  [join(scriptDir, 'test-native-workbench-v2-electron.mjs')],
  {
    cwd: electronRoot,
    env: {
      ...process.env,
      OPENSQUILLA_REQUIRE_ELECTRON_FOREGROUND: '1',
    },
  },
)

await run(
  'v1-html-agent-edit-electron',
  process.execPath,
  [join(scriptDir, 'test-v1-html-agent-edit-e2e.mjs')],
  {
    cwd: electronRoot,
    env: {
      ...process.env,
      OPENSQUILLA_REQUIRE_ELECTRON_FOREGROUND: '1',
    },
  },
)

console.log('offline document Workbench full Electron user-journey gate passed')
