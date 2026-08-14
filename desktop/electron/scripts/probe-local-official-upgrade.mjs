import { strict as assert } from 'node:assert'
import { execFile as execFileCallback } from 'node:child_process'
import { mkdir, realpath } from 'node:fs/promises'
import { resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { promisify } from 'node:util'

import { _electron as electron } from 'playwright'

import { waitFor, writeSyntheticCredential } from './packaged-smoke-helpers.mjs'

const execFile = promisify(execFileCallback)

function requiredOption(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`Missing required option ${name}`)
  }
  return resolve(process.argv[index + 1])
}

const executablePath = requiredOption('--executable')
const userDataDir = requiredOption('--user-data-dir')
const isolatedHome = requiredOption('--home')
const stateDir = requiredOption('--state-dir')
const requireChatNew = process.argv.includes('--require-chat-new')
const sandboxModeIndex = process.argv.indexOf('--expect-sandbox-mode')
const expectedSandboxMode = sandboxModeIndex >= 0 ? process.argv[sandboxModeIndex + 1] : ''
if (expectedSandboxMode && !['safe', 'full'].includes(expectedSandboxMode)) {
  throw new Error('--expect-sandbox-mode must be safe or full')
}

async function probeSandboxSettings(page, expectedMode) {
  const sandboxTab = page.locator('#settings-rail-sandbox')
  if (!await sandboxTab.isVisible().catch(() => false)) {
    await page.locator('.sidebar-fn-item[data-icon="settings"]').click()
  }
  try {
    await sandboxTab.waitFor({ state: 'visible', timeout: 30_000 })
  } catch (error) {
    const diagnostics = await page.evaluate(() => ({
      href: window.location.href,
      settingsModalCount: document.querySelectorAll('.settings-modal').length,
      settingsRailIds: Array.from(
        document.querySelectorAll('[id^="settings-rail-"]'),
        element => element.id,
      ),
      bodyText: document.body.innerText.slice(0, 1_000),
    })).catch(() => ({ href: page.url(), diagnosticsUnavailable: true }))
    process.stderr.write(`Sandbox settings probe diagnostics: ${JSON.stringify(diagnostics)}\n`)
    throw error
  }
  await sandboxTab.click()

  const overview = page.getByTestId('sandbox-overview')
  await overview.waitFor({ state: 'visible', timeout: 30_000 })
  const capabilityStatus = page.locator('.sandbox-settings__status')
  await waitFor(
    async () => await capabilityStatus.evaluate(element => element.classList.contains('is-ready')),
    'live sandbox capability readiness',
    60_000,
  )

  const safeButton = page.getByTestId('sandbox-safe-mode')
  const fullButton = page.getByTestId('sandbox-full-mode')
  assert.equal(await safeButton.isDisabled(), false, 'Safe mode must be selectable')
  const expectedButton = expectedMode === 'safe' ? safeButton : fullButton
  const alternateButton = expectedMode === 'safe' ? fullButton : safeButton
  assert.equal(
    await expectedButton.evaluate(element => element.classList.contains('is-selected')),
    true,
    `Expected ${expectedMode} mode to be selected`,
  )

  await page.getByTestId('sandbox-open-files').click()
  await page.getByTestId('sandbox-detail').waitFor({ state: 'visible', timeout: 10_000 })
  await page.getByTestId('sandbox-detail-back').click()
  await overview.waitFor({ state: 'visible', timeout: 10_000 })

  await alternateButton.click()
  await waitFor(
    async () => await alternateButton.evaluate(element => element.classList.contains('is-selected')),
    `switch to ${expectedMode === 'safe' ? 'full' : 'safe'} mode`,
    30_000,
  )
  await expectedButton.click()
  await waitFor(
    async () => await expectedButton.evaluate(element => element.classList.contains('is-selected')),
    `restore ${expectedMode} mode`,
    30_000,
  )

  return {
    available: true,
    status: (await capabilityStatus.innerText()).trim(),
    expectedMode,
    safeSelectable: true,
    detailNavigation: true,
    modeRoundTrip: true,
  }
}

async function closePackagedApp(application, page) {
  const child = application.process()
  if (page) {
    await page.evaluate(() => {
      window.opensquillaDesktop.quitApp().catch(() => {})
      return true
    }).catch(() => {})
  }
  await delay(1_000)
  await Promise.race([
    application.close().catch(() => {}),
    delay(5_000),
  ])
  if (child.exitCode === null && child.signalCode === null) {
    if (process.platform === 'win32') {
      await execFile('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
        windowsHide: true,
        timeout: 15_000,
      }).catch(() => {})
    } else {
      child.kill('SIGKILL')
    }
  }
}

await mkdir(userDataDir, { recursive: true })
await mkdir(isolatedHome, { recursive: true })
await mkdir(stateDir, { recursive: true })
await writeSyntheticCredential(userDataDir, {
  model: 'opensquilla-local-upgrade-rehearsal',
})

let app
try {
  app = await electron.launch({
    executablePath,
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
    ],
    env: {
      ...process.env,
      HOME: isolatedHome,
      USERPROFILE: isolatedHome,
      OPENSQUILLA_STATE_DIR: isolatedHome,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    },
  })

  const runtime = await app.evaluate(({ app: electronApp }) => ({
    version: electronApp.getVersion(),
    userData: electronApp.getPath('userData'),
    home: process.env.HOME,
    userProfile: process.env.USERPROFILE,
    stateDir: process.env.OPENSQUILLA_STATE_DIR,
    pid: process.pid,
  }))

  assert.equal(await realpath(runtime.userData), await realpath(userDataDir))
  assert.equal(resolve(runtime.home), isolatedHome)
  assert.equal(resolve(runtime.userProfile), isolatedHome)
  assert.equal(resolve(runtime.stateDir), isolatedHome)

  const page = await app.firstWindow({ timeout: 90_000 })
  await page.waitForLoadState('domcontentloaded', { timeout: 90_000 }).catch(() => {})
  const gateway = await waitFor(async () => {
    const status = await page.evaluate(() => window.opensquillaDesktop.getGatewayStatus())
    return status?.status === 'ready' ? status : null
  }, 'owned Gateway readiness', 120_000)
  if (requireChatNew) {
    await waitFor(
      async () => page.url().includes('/control/chat/new'),
      'default /control/chat/new route',
      120_000,
    )
  }
  const sandboxSettings = expectedSandboxMode
    ? await probeSandboxSettings(page, expectedSandboxMode)
    : null
  const listeningAddresses = []
  if (process.platform === 'win32') {
    const { stdout } = await execFile('netstat.exe', ['-ano', '-p', 'tcp'], {
      windowsHide: true,
      timeout: 10_000,
    })
    const expectedPort = String(gateway.port)
    for (const line of stdout.split(/\r?\n/)) {
      const fields = line.trim().split(/\s+/)
      if (fields.length < 4 || fields[0] !== 'TCP' || fields[3] !== 'LISTENING') continue
      const local = fields[1]
      const separator = local.lastIndexOf(':')
      if (separator < 0 || local.slice(separator + 1) !== expectedPort) continue
      const address = local.slice(0, separator).replace(/^\[|\]$/g, '')
      if (!listeningAddresses.includes(address)) listeningAddresses.push(address)
    }
  } else {
    listeningAddresses.push(new URL(gateway.url).hostname)
  }

  process.stdout.write(`${JSON.stringify({
    ...runtime,
    route: new URL(page.url()).pathname,
    gateway,
    listeningAddresses,
    sandboxSettings,
  })}\n`)

  await closePackagedApp(app, page)
  app = undefined
} finally {
  if (app) await closePackagedApp(app).catch(() => {})
}
