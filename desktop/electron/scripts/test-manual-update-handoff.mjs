import assert from 'node:assert/strict'

import {
  manualHandoffBlocker,
  manualInstallerLaunchPlan,
} from '../dist/manual-update-handoff.js'

// --- launch plan: detached installer with electron-updater's --updated ---

const plan = manualInstallerLaunchPlan('C:/updater/OpenSquilla-0.5.3-win-x64.exe')
assert.equal(plan.command, 'C:/updater/OpenSquilla-0.5.3-win-x64.exe')
assert.deepEqual(plan.args, ['--updated'])
assert.deepEqual(plan.options, { detached: true, stdio: 'ignore' })

assert.throws(() => manualInstallerLaunchPlan(''), /path is empty/)

// --- eligibility: the ready state and every blocker, in precedence order ---

const ready = {
  installMode: 'manual',
  status: 'downloaded',
  verifiedInstallerPath: 'C:/updater/OpenSquilla-0.5.3-win-x64.exe',
  updateApplying: false,
  quitting: false,
  writersClosed: false,
}

assert.equal(manualHandoffBlocker(ready), null)

// Lifecycle blockers win over mode/state mismatches so callers can tell
// "retry later" apart from "wrong flow".
assert.equal(
  manualHandoffBlocker({ ...ready, updateApplying: true, installMode: 'native' }),
  'already-applying',
)
assert.equal(manualHandoffBlocker({ ...ready, quitting: true }), 'already-quitting')
assert.equal(manualHandoffBlocker({ ...ready, writersClosed: true }), 'writers-closed')

// Mode and state mismatches.
assert.equal(manualHandoffBlocker({ ...ready, installMode: 'native' }), 'not-manual-mode')
assert.equal(manualHandoffBlocker({ ...ready, installMode: 'none' }), 'not-manual-mode')
assert.equal(
  manualHandoffBlocker({ ...ready, verifiedInstallerPath: null }),
  'no-verified-installer',
)
assert.equal(manualHandoffBlocker({ ...ready, status: 'available' }), 'not-downloaded')
assert.equal(manualHandoffBlocker({ ...ready, status: 'applying' }), 'not-downloaded')
assert.equal(manualHandoffBlocker({ ...ready, status: 'error' }), 'not-downloaded')

console.log('manual update handoff tests passed')
