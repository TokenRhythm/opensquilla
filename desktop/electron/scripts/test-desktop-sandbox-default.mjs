import assert from 'node:assert/strict'

import { freshDesktopSandboxConfigLines } from '../dist/desktop-sandbox-default.js'

assert.deepEqual(
  freshDesktopSandboxConfigLines(null, 'darwin'),
  ['[sandbox]', 'run_mode = "safe"', ''],
  'a new macOS profile should start in Safe mode',
)

assert.deepEqual(
  freshDesktopSandboxConfigLines(null, 'win32'),
  ['[sandbox]', 'run_mode = "full"', ''],
  'a new Windows profile should stay in Full access until setup succeeds',
)

assert.deepEqual(
  freshDesktopSandboxConfigLines(null, 'linux'),
  ['[sandbox]', 'run_mode = "full"', ''],
  'unsupported desktop platforms should retain the compatibility default',
)

assert.deepEqual(
  freshDesktopSandboxConfigLines('[sandbox]\nrun_mode = "full"\n', 'darwin'),
  [],
  'an existing profile must keep its persisted sandbox mode during an update',
)

console.log('Desktop sandbox default policy tests passed.')
