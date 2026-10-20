import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { launchWithProtocolRegistrationGuard } from './packaged-recovery-inspector.mjs'
import { requiredOption } from './packaged-smoke-helpers.mjs'

const executablePath = resolve(requiredOption('--executable'))
const outputDir = resolve(requiredOption('--output-dir'))
const guardPath = fileURLToPath(new URL('./fixtures/packaged-recovery-isolation.cjs', import.meta.url))
await mkdir(outputDir, { recursive: true })
await writeFile(resolve(outputDir, 'package.json'), JSON.stringify({
  name: 'recovery-inspector-guard-preflight', version: '0.0.0', main: 'main.cjs',
}))
await writeFile(resolve(outputDir, 'main.cjs'), `
const { app, BrowserWindow } = require('electron')
// A failed guard cannot invoke the real OS registration API in this fixture.
if (!globalThis.__packagedRecoveryIsolation) app.exit(87)
else app.whenReady().then(() => {
  if (app.setAsDefaultProtocolClient('opensquilla') !== false) app.exit(88)
  const window = new BrowserWindow({ show: false })
  window.loadURL('data:text/html,<title>Isolated guard fixture</title>')
})
`)
const results = []
for (let index = 0; index < 3; index++) {
  const { app, startupGuard } = await launchWithProtocolRegistrationGuard({
    executablePath,
    args: ['--opensquilla-isolated-recovery-gate', `--user-data-dir=${resolve(outputDir, 'profile-' + index)}`, outputDir],
    env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined },
    timeout: 60_000,
  }, guardPath)
  try {
    const attempts = await app.evaluate(() => globalThis.__packagedRecoveryIsolation.protocolRegistrationAttempts)
    assert.equal(attempts, 1)
    results.push({ startupGuard, attempts })
  } finally { await app.close() }
}
await writeFile(resolve(outputDir, 'inspector-guard-report.json'), JSON.stringify({ ok: true, results }, null, 2))
console.log(JSON.stringify({ ok: true, results }, null, 2))
