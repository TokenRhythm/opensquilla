// Test-only preload: keep the packaged app's startup and app.isPackaged intact,
// but prevent this disposable candidate from claiming the user's URL scheme.
// No renderer, Gateway, recovery, credential, or persistence behavior is patched.
const { app } = require('electron')
if (!process.argv.includes('--opensquilla-isolated-recovery-gate')) {
  throw new Error('Packaged recovery isolation requires the explicit gate marker')
}
globalThis.__packagedRecoveryIsolation = { protocolRegistrationAttempts: 0 }
Object.defineProperty(app, 'setAsDefaultProtocolClient', {
  configurable: false,
  writable: false,
  value() {
    globalThis.__packagedRecoveryIsolation.protocolRegistrationAttempts++
    return false
  },
})
