import { resolve } from 'node:path'
import { writeFile } from 'node:fs/promises'
import { launchPackagedCandidate, requiredOption, waitFor } from './packaged-smoke-helpers.mjs'
import { captureElectronProcessIdentity, cleanupPackagedFirstSend } from './packaged-first-send-cleanup.mjs'

const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const output = resolve(requiredOption('--output'))
let app
let processIdentity = {}
let error
try {
  app = await launchPackagedCandidate({ executablePath, userDataDir,
    model: 'opensquilla-release-session-recovery-smoke', scrubProviderSecrets: true,
    env: { GITHUB_ACTIONS: '0', OPENSQUILLA_TESTING: '0' } })
  processIdentity = await captureElectronProcessIdentity(app)
  const page = await app.firstWindow({ timeout: 60000 })
  await waitFor(() => page.url().startsWith('opensquilla-app://desktop/chat'), 'installed renderer', 120000)
  await waitFor(async () => (await page.evaluate(() => window.opensquillaDesktop?.getGatewayConnection?.()))?.status === 'ready', 'installed Gateway readiness', 120000)
  const url = new URL(page.url())
  url.pathname = '/chat'
  url.search = new URLSearchParams({ session: 'agent:main:webchat:release-recovery-long-session' }).toString()
  await page.goto(url.toString(), { waitUntil: 'domcontentloaded' })
  await waitFor(async () => await page.getByText('Synthetic retained history message 0320 (reinstall-retained)', { exact: true }).first().isVisible(), 'retained database history visible in installed app', 60000)
  await page.screenshot({ path: output + '.png' })
  await writeFile(output, JSON.stringify({ ok: true, rendererReady: true, gatewayReady: true, retainedHistoryVisible: true }, null, 2))
} catch (cause) {
  error = cause
} finally {
  try { await cleanupPackagedFirstSend({ app, processIdentity }) } catch (cause) { error ??= cause }
}
if (error) throw error
