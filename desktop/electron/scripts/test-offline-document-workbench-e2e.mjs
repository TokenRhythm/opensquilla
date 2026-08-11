import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const electronRoot = join(scriptDir, '..')
const repoRoot = join(electronRoot, '..', '..')

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || repoRoot,
    env: options.env || process.env,
    stdio: 'inherit',
  })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with exit code ${result.status}`)
  }
}

// The release gate deliberately composes two real boundaries instead of
// coupling UI timing to Gateway persistence:
//   1. one owned-Gateway WebSocket lifecycle proves the durable resource,
//      EditSession, exact4 agent mutation, and immutable publication contract;
//   2. one real Electron process proves the native preview/selection/editor
//      surface, including keyboard and IME input under an explicit foreground
//      precondition.
// Both fixtures are loopback-only and use synthetic bytes and model replies.
const uv = process.platform === 'win32' ? 'uv.exe' : 'uv'
run(
  uv,
  [
    'run',
    'pytest',
    '-q',
    'tests/test_live_artifact_prompt_annotations_e2e.py::test_owned_gateway_html_workbench_lifecycle_is_offline_and_immutable',
  ],
)

run(
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

console.log('offline document Workbench owned-Gateway + real Electron gate passed')
