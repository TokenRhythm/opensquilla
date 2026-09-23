import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import { desktopRouterConfigTomlLines } from '../dist/desktop-router-config.js'
import { DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS } from '../dist/gateway-lifecycle.js'
import { parse, stringify } from 'smol-toml'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const screenshotPath = String(process.env.OPENSQUILLA_DESKTOP_ONBOARDING_SCREENSHOT || '').trim()
const CASE_NAMES = ['submit', 'optional-probe', 'skip', 'close', 'slow-probe', 'provider-client']
const selectedCases = new Set(String(process.env.OPENSQUILLA_DESKTOP_ONBOARDING_CASES || '')
  .split(',').map(value => value.trim()).filter(Boolean))
const onboardingDiagnosticContexts = new WeakMap()
const reportedOnboardingFailures = new WeakSet()
// Match the existing orphan-recovery native harness's cold-start budget:
// Gateway readiness owns 120s, with 45s for the surrounding Desktop startup.
// Each launch consumes one absolute deadline; it is not renewed by assertions.
const INITIAL_DESKTOP_STARTUP_BUDGET_MS = DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + 45_000
for (const name of selectedCases) {
  assert.ok(CASE_NAMES.includes(name), `Unknown onboarding case: ${name}; choose ${CASE_NAMES.join(', ')}`)
}
function caseSelected(name) {
  return selectedCases.size === 0 || selectedCases.has(name)
}
async function runCase(name, verify) {
  if (!caseSelected(name)) return
  console.log(`RUN onboarding ${name}`)
  await verify()
  console.log(`PASS onboarding ${name}`)
}
const ONBOARDING_TELEMETRY_EVENTS = new Set([
  'onboarding_save_started',
  'onboarding_save_stage_started',
  'onboarding_save_stage_finished',
  'onboarding_save_finished',
])
const ONBOARDING_TELEMETRY_STAGES = [
  'primary_recovery_inspect',
  'pending_setup_read',
  'settings_persist',
  'local_finalize',
  'flow_handoff',
]

async function waitFor(check, label, timeoutMs = 60_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  const suffix = lastError ? ` Last error: ${lastError.message || lastError}` : ''
  throw new Error(`Timed out waiting for ${label}.${suffix}`)
}

async function fileExists(path) {
  try {
    await readFile(path)
    return true
  } catch (error) {
    if (error?.code === 'ENOENT') return false
    throw error
  }
}

async function readDirectoryOrEmpty(path) {
  try {
    return await readdir(path)
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw error
  }
}

async function startOnboardingProbeServer(initialMode = 'success') {
  let mode = initialMode
  const requests = []
  const server = createServer((request, response) => {
    const body = []
    request.on('data', (chunk) => body.push(chunk))
    request.on('end', () => {
      requests.push({
        method: request.method,
        url: request.url,
        authorization: request.headers.authorization || '',
        body: Buffer.concat(body).toString('utf8'),
      })
      if (mode === 'disconnect') {
        request.socket.destroy()
        return
      }
      if (mode === 'reject') {
        response.writeHead(401, { 'content-type': 'application/json' })
        response.end(JSON.stringify({
          error: {
            message: 'Synthetic credential rejected.',
            type: 'authentication_error',
            code: 'invalid_api_key',
          },
        }))
        return
      }
      response.writeHead(200, { 'content-type': 'text/event-stream' })
      response.end([
        'data: {"id":"chatcmpl-onboarding-test","object":"chat.completion.chunk","created":0,"model":"synthetic-model","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}',
        '',
        'data: {"id":"chatcmpl-onboarding-test","object":"chat.completion.chunk","created":0,"model":"synthetic-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
        '',
        'data: [DONE]',
        '',
      ].join('\n'))
    })
  })
  await new Promise((resolveListen, rejectListen) => {
    const onError = (error) => rejectListen(error)
    server.once('error', onError)
    server.listen(0, '127.0.0.1', () => {
      server.off('error', onError)
      resolveListen()
    })
  })
  const address = server.address()
  assert.ok(address && typeof address !== 'string')
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    requests,
    setMode(nextMode) {
      mode = nextMode
    },
    async close() {
      await new Promise((resolveClose, rejectClose) => {
        server.close((error) => (error ? rejectClose(error) : resolveClose()))
      })
    },
  }
}

async function setOnboardingBaseUrl(page, baseUrl) {
  await page.locator('#baseUrl').evaluate((input, value) => {
    input.value = value
  }, baseUrl)
}

function isManagedTelemetrySpoolEntry(name) {
  return name.endsWith('.ready')
    || name.includes('.processing.')
    || (name.startsWith('.') && name.endsWith('.tmp'))
}

async function readOnboardingTelemetry(userDataDir) {
  const source = await readFile(join(userDataDir, 'logs', 'desktop.log'), 'utf8')
  return source
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((record) => ONBOARDING_TELEMETRY_EVENTS.has(record.event))
}

function assertOnboardingTelemetrySchema(records, expectedSecret) {
  assert.equal(records.length, 12, 'one successful save must emit one bounded trace')
  const attempt = records[0]?.attempt
  assert.equal(Number.isInteger(attempt) && attempt > 0, true)
  assert.equal(records.every((record) => record.attempt === attempt), true)
  assert.deepEqual(
    Object.keys(records[0]).sort(),
    ['at', 'attempt', 'event', 'packaged'],
  )
  assert.equal(records[0].event, 'onboarding_save_started')
  assert.equal(records[0].packaged, false)

  const observedStages = []
  let cursor = 1
  for (const stage of ONBOARDING_TELEMETRY_STAGES) {
    const started = records[cursor]
    const finished = records[cursor + 1]
    cursor += 2
    assert.deepEqual(Object.keys(started).sort(), ['at', 'attempt', 'event', 'stage'])
    assert.deepEqual(
      Object.keys(finished).sort(),
      ['at', 'attempt', 'durationMs', 'event', 'outcome', 'stage'],
    )
    assert.equal(started.event, 'onboarding_save_stage_started')
    assert.equal(started.stage, stage)
    assert.equal(finished.event, 'onboarding_save_stage_finished')
    assert.equal(finished.stage, stage)
    assert.equal(finished.outcome, 'completed')
    assert.equal(Number.isFinite(finished.durationMs), true)
    assert.equal(Number.isInteger(finished.durationMs), true)
    assert.ok(finished.durationMs >= 0)
    observedStages.push(stage)
  }
  assert.deepEqual(observedStages, ONBOARDING_TELEMETRY_STAGES)

  const terminal = records[cursor]
  assert.deepEqual(
    Object.keys(terminal).sort(),
    [
      'at',
      'attempt',
      'event',
      'lastStage',
      'outcome',
      'settingsPersistedConfirmed',
      'totalDurationMs',
      'writerAdmitted',
    ],
  )
  assert.equal(terminal.event, 'onboarding_save_finished')
  assert.equal(terminal.outcome, 'ok')
  assert.equal(terminal.writerAdmitted, true)
  assert.equal(terminal.settingsPersistedConfirmed, true)
  assert.equal(terminal.lastStage, 'flow_handoff')
  assert.equal(Number.isFinite(terminal.totalDurationMs), true)
  assert.equal(Number.isInteger(terminal.totalDurationMs), true)
  assert.ok(terminal.totalDurationMs >= 0)
  assert.equal(
    JSON.stringify(records).includes(expectedSecret),
    false,
    'local onboarding timing records must never contain the submitted secret',
  )
}

function diagnosticUrl(raw) {
  if (String(raw).startsWith('data:')) return 'data:[onboarding document omitted]'
  try {
    const url = new URL(raw)
    return `${url.protocol}//${url.host}${url.pathname}`
  } catch {
    return '<unavailable>'
  }
}

function redactDiagnosticText(value) {
  return String(value)
    .replace(/synthetic-[A-Za-z0-9_-]*key\b/g, '[fixture-key]')
    .replace(/\bBearer\s+[A-Za-z0-9._~-]+/gi, 'Bearer [redacted]')
    .replace(/("(?:api[_-]?key|encryptedApiKey|authToken|authorization|token|secret|password)"\s*:\s*)"(?:\\.|[^"\\])*"/gi, '$1"[redacted]"')
}

async function diagnosticRead(read, fallback) {
  let timer
  try {
    return await Promise.race([
      Promise.resolve().then(read),
      new Promise(resolveRead => { timer = setTimeout(() => resolveRead(fallback), 1_500) }),
    ])
  } catch {
    return fallback
  } finally {
    clearTimeout(timer)
  }
}

async function reportOnboardingFailure(app, phase, error) {
  if (error && typeof error === 'object') {
    if (reportedOnboardingFailures.has(error)) return
    reportedOnboardingFailures.add(error)
  }
  const context = onboardingDiagnosticContexts.get(app)
  const readLog = name => context
    ? diagnosticRead(() => readFile(join(context.userDataDir, 'logs', name), 'utf8'), '<log unavailable>')
    : Promise.resolve('<profile unavailable>')
  const windows = await diagnosticRead(() => Promise.all(app.windows().map(async page => ({
    closed: page.isClosed(),
    url: diagnosticUrl(page.url()),
    title: await diagnosticRead(() => page.title(), '<title unavailable>'),
    connection: await diagnosticRead(() => page.evaluate(async () => {
      const connection = await window.opensquillaDesktop?.getGatewayConnection?.()
      // Never serialize the descriptor's authToken or any form/request values.
      return connection ? {
        status: connection.status,
        revision: connection.revision,
        error: connection.error,
      } : null
    }), { diagnosticError: 'connection snapshot unavailable' }),
  }))), [{ diagnosticError: 'window snapshot unavailable' }])
  const [desktopLog, gatewayLog] = await Promise.all([readLog('desktop.log'), readLog('gateway.log')])
  const report = JSON.stringify({
    event: 'onboarding_e2e_failure',
    fixture: context?.prefix ?? 'unknown',
    phase,
    startup: context ? {
      elapsedMs: Date.now() - context.startupStartedAt,
      budgetMs: INITIAL_DESKTOP_STARTUP_BUDGET_MS,
      readyConfirmed: context.startupReady,
    } : null,
    error: String(error?.stack || error),
    windows,
    desktopLogTail: desktopLog.slice(-24_000),
    gatewayLogTail: gatewayLog.slice(-24_000),
  }, (_key, value) => typeof value === 'string' ? redactDiagnosticText(value) : value, 2)
  console.error(report)
  const reportDir = String(process.env.CI_REPORT_DIR || '').trim()
  if (reportDir) {
    const fileName = `${context?.prefix ?? 'onboarding-'}${phase}-${Date.now()}`
      .replace(/[^A-Za-z0-9_.-]/g, '_') + '.json'
    await diagnosticRead(async () => {
      await mkdir(reportDir, { recursive: true })
      await writeFile(join(reportDir, fileName), report + '\n', 'utf8')
    }, null)
  }
}

async function setupWindow(app) {
  let phase = 'gateway-startup'
  try {
    // The optional invitation is published only after the client and Gateway
    // are ready. Profile preparation must not consume its separate UI deadline.
    await readyDesktopWindow(app)
    phase = 'setup-window'
    return await waitFor(async () => {
      for (const page of app.windows()) {
        if (page.isClosed()) continue
        await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
        if (await page.locator('#setup-form').count().catch(() => 0)) return page
      }
      return null
    }, 'desktop onboarding window')
  } catch (error) {
    await reportOnboardingFailure(app, phase, error)
    throw error
  }
}

async function bootWindow(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#phase, #timer').count().catch(() => 0) === 2) return page
    }
    return null
  }, 'desktop boot window')
}

async function loadBootContractWindow(app) {
  const desktopWindowId = await waitFor(async () => (
    await app.evaluate(({ BrowserWindow }) => (
      BrowserWindow.getAllWindows().find((candidate) => (
        !candidate.isDestroyed()
        && candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))?.id ?? null
    ))
  ), 'local Desktop renderer before the boot-page contract test')
  await app.evaluate(async ({ BrowserWindow }, payload) => {
    const window = BrowserWindow.fromId(payload.windowId)
    if (!window || window.isDestroyed()) throw new Error('Desktop renderer is unavailable.')
    await window.loadFile(payload.bootPath)
  }, {
    windowId: desktopWindowId,
    bootPath: join(packageRoot, 'src', 'boot.html'),
  })
}

async function sendBootEvent(app, channel, payload) {
  await app.evaluate(({ BrowserWindow }, event) => {
    const window = BrowserWindow.getAllWindows().find((candidate) => (
      !candidate.isDestroyed() && candidate.webContents.getURL().includes('boot.html')
    ))
    if (!window) throw new Error('Desktop boot window is unavailable.')
    window.webContents.send(event.channel, event.payload)
  }, { channel, payload })
}

async function bootElapsedSeconds(page) {
  const text = (await page.locator('#timer').innerText()).trim()
  const match = /^(\d+(?:\.\d+)?)s$/.exec(text)
  assert.ok(match, `unexpected boot timer text: ${text}`)
  return Number(match[1])
}

async function waitForBootProgress(page, expected) {
  return await waitFor(async () => {
    const progress = page.locator('#startupProgress')
    const value = Number(await progress.getAttribute('aria-valuenow'))
    const count = (await page.locator('#progressCount').innerText()).trim()
    const width = await progress.evaluate((element) => (
      element.style.getPropertyValue('--boot-progress').trim()
    ))
    return value === expected && count === `${expected}/4`
      ? { value, count, width }
      : null
  }, `boot progress to reach ${expected}/4`)
}

function boxesOverlap(left, right) {
  return left.x < right.x + right.width
    && left.x + left.width > right.x
    && left.y < right.y + right.height
    && left.y + left.height > right.y
}

async function assertSubmitActionsDoNotOverlap(page) {
  const boxes = {
    skip: await page.locator('#skip').boundingBox(),
    submitStatus: await page.locator('#submitStatus').boundingBox(),
    finish: await page.locator('#finish').boundingBox(),
  }
  for (const [name, box] of Object.entries(boxes)) {
    assert.ok(box, `${name} must have a visible bounding box`)
  }
  for (const [leftName, rightName] of [
    ['skip', 'submitStatus'],
    ['skip', 'finish'],
    ['submitStatus', 'finish'],
  ]) {
    assert.equal(
      boxesOverlap(boxes[leftName], boxes[rightName]),
      false,
      `${leftName} must not overlap ${rightName} in the default onboarding window`,
    )
  }
}

async function verifyBootPhaseTimer(app) {
  const page = await bootWindow(app)
  const phase = page.locator('#phase')
  const timer = page.locator('#timer')
  const progress = page.locator('#startupProgress')
  assert.equal(await phase.getAttribute('role'), 'status')
  assert.equal(await phase.getAttribute('aria-live'), 'polite')
  assert.equal(await phase.getAttribute('aria-atomic'), 'true')
  assert.equal(await timer.getAttribute('aria-hidden'), 'true')
  assert.equal(await page.locator('section.status').getAttribute('aria-live'), null)
  assert.equal(await progress.getAttribute('role'), 'progressbar')
  assert.equal(await progress.getAttribute('aria-labelledby'), 'phase')
  assert.equal(await progress.getAttribute('aria-valuemin'), '0')
  assert.equal(await progress.getAttribute('aria-valuemax'), '4')

  const stateBeforeReload = await page.evaluate(async () => (
    await window.opensquillaDesktop.getBootState()
  ))
  const persistedProgress = {
    profile: 0,
    'gateway-start': 1,
    'gateway-health': 2,
    control: 3,
    ready: 4,
  }[stateBeforeReload?.status?.phaseId] ?? 0
  // The boot window sits behind the setup invitation, where Chromium may throttle
  // its 100 ms timer. Drive elapsed time explicitly instead of racing a brief
  // wall-clock reset window, and install before reload so every timer is owned.
  const bootClockOrigin = Date.now()
  await page.clock.install({ time: bootClockOrigin })
  await page.clock.pauseAt(bootClockOrigin + 1_000)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitForBootProgress(page, persistedProgress)
  await waitFor(async () => (
    (await phase.innerText()).trim() === String(stateBeforeReload?.status?.label || '').trim()
      ? true
      : null
  ), 'boot progress snapshot to restore after a splash reload')

  async function bootTimestamp(offsetMs = 0) {
    return await page.evaluate((offset) => new Date(Date.now() + offset).toISOString(), offsetMs)
  }

  async function applyBootStatus(status) {
    await sendBootEvent(app, 'desktop:boot:status', status)
    await waitFor(async () => (
      (await phase.innerText()).trim() === status.label
    ), `boot status ${status.label} to render`)
  }
  // The non-blocking onboarding invitation now opens after real boot has
  // reached ready. A boot error arms the documented retry reset; a profile
  // status alone must not make completed progress go backwards.
  await sendBootEvent(app, 'desktop:boot:error', { message: 'Synthetic retry boundary.' })
  await waitFor(async () => page.locator('body').evaluate(body => body.classList.contains('errored')),
    'boot error to arm the retry progress reset')
  await applyBootStatus({
    phaseId: 'profile',
    label: 'Synthetic new boot sequence',
    at: await bootTimestamp(),
  })
  await waitForBootProgress(page, 0)

  const staleStatus = {
    phaseId: 'gateway-start',
    label: 'Synthetic gateway start',
    at: await bootTimestamp(-3_000),
  }
  await applyBootStatus(staleStatus)
  assert.equal((await waitForBootProgress(page, 1)).width, '25%')
  assert.equal(await bootElapsedSeconds(page), 3, 'boot timer must include elapsed phase age')

  const activeStatus = {
    phaseId: 'gateway-health',
    label: 'Synthetic gateway health',
    at: await bootTimestamp(),
  }
  await applyBootStatus(activeStatus)
  assert.equal((await waitForBootProgress(page, 2)).width, '50%')
  const resetElapsed = await bootElapsedSeconds(page)
  assert.equal(resetElapsed, 0, 'boot timer must reset for a new phase identity')

  await page.clock.fastForward(350)
  const beforeReplay = await bootElapsedSeconds(page)
  // Labels are not part of BootStatus identity. A distinct label acknowledges
  // renderer receipt without letting an unchanged DOM value satisfy the wait.
  await applyBootStatus({ ...activeStatus, label: 'Synthetic gateway health replay' })
  assert.equal(await bootElapsedSeconds(page), beforeReplay)
  await page.clock.fastForward(350)
  const afterReplay = await bootElapsedSeconds(page)
  assert.ok(
    afterReplay > beforeReplay && afterReplay > resetElapsed,
    'replaying one BootStatus identity must not reset its elapsed timer',
  )

  const repeatedPhaseWithNewTimestamp = {
    ...activeStatus,
    label: 'Synthetic gateway health restarted',
    at: await bootTimestamp(),
  }
  await applyBootStatus(repeatedPhaseWithNewTimestamp)
  assert.equal(
    await bootElapsedSeconds(page),
    0,
    'boot timer must reset for a repeated phase with a new timestamp',
  )

  const invalidTimestampLabel = 'Synthetic invalid timestamp'
  await applyBootStatus({
    phaseId: 'gateway-start',
    label: invalidTimestampLabel,
    at: 'not-a-date',
  })
  await waitForBootProgress(page, 2)
  assert.equal(await bootElapsedSeconds(page), 0, 'invalid boot timestamp must clamp to zero')

  const futureTimestampLabel = 'Synthetic future timestamp'
  await applyBootStatus({
    phaseId: 'control',
    label: futureTimestampLabel,
    at: await bootTimestamp(60_000),
  })
  assert.equal((await waitForBootProgress(page, 3)).width, '75%')
  assert.equal(await bootElapsedSeconds(page), 0, 'future boot timestamp must clamp to zero')

  const activeStepBeforeUnknown = await page.locator('.step.active').getAttribute('data-step')
  await applyBootStatus({
    phaseId: 'future-phase',
    label: 'Synthetic future phase',
    at: await bootTimestamp(),
  })
  await waitForBootProgress(page, 3)
  assert.equal(
    await page.locator('.step.active').getAttribute('data-step'),
    activeStepBeforeUnknown,
    'an unknown phase must not move the visible milestone state',
  )

  await applyBootStatus({
    phaseId: 'ready',
    label: 'Synthetic ready',
    at: await bootTimestamp(),
  })
  assert.equal((await waitForBootProgress(page, 4)).width, '100%')

  await sendBootEvent(app, 'desktop:boot:error', { message: 'Synthetic boot pause.' })
  await waitFor(async () => (
    await page.locator('body').evaluate((body) => body.classList.contains('errored'))
  ), 'boot error to render')
  await page.clock.fastForward(150)
  const frozenText = await timer.innerText()
  await page.clock.fastForward(350)
  assert.equal(await timer.innerText(), frozenText, 'boot errors must freeze the elapsed timer')
  await waitForBootProgress(page, 4)

  await applyBootStatus({
    phaseId: 'profile',
    label: 'Synthetic retry',
    at: await bootTimestamp(),
  })
  await waitForBootProgress(page, 0)
  await page.clock.fastForward(350)
  assert.notEqual(await timer.innerText(), frozenText, 'a new retry status must resume phase timing')
  await page.clock.resume()
}

async function launchIsolatedOnboarding(prefix, existingUserDataRoot) {
  const userDataRoot = existingUserDataRoot || await mkdtemp(join(tmpdir(), prefix))
  const userDataDir = join(userDataRoot, 'chromium-user-data')
  const isolatedHome = join(userDataRoot, 'home')
  await mkdir(isolatedHome, { recursive: true })
  const startupStartedAt = Date.now()
  const app = await electron.launch({
    ...(process.env.OPENSQUILLA_DESKTOP_TEST_ELECTRON_EXECUTABLE
      ? { executablePath: process.env.OPENSQUILLA_DESKTOP_TEST_ELECTRON_EXECUTABLE }
      : {}),
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
      packageRoot,
    ],
    env: {
      ...process.env,
      HOME: isolatedHome,
      USERPROFILE: isolatedHome,
      OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      OPENSQUILLA_TESTING: '1',
      OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION: '',
      LANG: 'en_US.UTF-8',
      LC_ALL: 'en_US.UTF-8',
    },
  })
  onboardingDiagnosticContexts.set(app, {
    prefix, userDataDir, startupStartedAt,
    startupDeadline: startupStartedAt + INITIAL_DESKTOP_STARTUP_BUDGET_MS,
    startupReady: false,
  })
  return { app, userDataDir, userDataRoot }
}

async function readyDesktopWindow(app) {
  const context = onboardingDiagnosticContexts.get(app)
  const initialStartup = context && !context.startupReady
  const timeoutMs = initialStartup ? context.startupDeadline - Date.now() : 60_000
  if (timeoutMs <= 0) {
    throw new Error(`Desktop startup exceeded its ${INITIAL_DESKTOP_STARTUP_BUDGET_MS}ms launch budget.`)
  }
  const result = await waitFor(async () => {
    const child = app.process()
    if (child.exitCode !== null || child.signalCode !== null) {
      return { error: `Electron exited before Gateway readiness (code=${child.exitCode}, signal=${child.signalCode}).` }
    }
    for (const page of app.windows()) {
      if (page.isClosed() || !page.url().startsWith('opensquilla-app://desktop/')) continue
      const connection = await page.evaluate(
        () => window.opensquillaDesktop?.getGatewayConnection?.(),
      )
      // Return terminal failures from the poll, then throw below: waitFor's
      // transient-observation catch must not swallow a real startup failure.
      if (connection?.status === 'error') return { error: connection.error || 'Desktop Gateway startup failed.' }
      if (connection?.status === 'ready') return { page }
    }
    return null
  }, 'ready Desktop Gateway without model configuration', timeoutMs)
  if (result.error) throw new Error(result.error)
  if (initialStartup && Date.now() >= context.startupDeadline) {
    throw new Error(`Desktop startup exceeded its ${INITIAL_DESKTOP_STARTUP_BUDGET_MS}ms launch budget.`)
  }
  if (context) context.startupReady = true
  return result.page
}

async function installPendingSaveStub(app) {
  await app.evaluate(({ ipcMain }) => {
    const state = {
      callCount: 0,
      lastPayload: null,
      pending: null,
    }
    globalThis.__opensquillaOnboardingSaveTest = state
    ipcMain.removeHandler('desktop:onboarding:save')
    ipcMain.handle('desktop:onboarding:save', (_event, payload) => {
      state.callCount += 1
      state.lastPayload = payload
      return new Promise((resolveSave, rejectSave) => {
        state.pending = { resolveSave, rejectSave }
      })
    })
  })
}

async function installPendingProbeStub(app) {
  await app.evaluate(({ ipcMain }) => {
    const requests = []
    globalThis.__opensquillaOnboardingProbeTest = requests
    ipcMain.removeHandler('desktop:onboarding:probe')
    ipcMain.handle('desktop:onboarding:probe', (_event, payload) => (
      new Promise(resolveProbe => requests.push({ payload, resolveProbe }))
    ))
  })
}

async function pendingProbeCount(app) {
  return await app.evaluate(() => globalThis.__opensquillaOnboardingProbeTest.length)
}

async function settlePendingProbe(app, index, result) {
  await app.evaluate((_electron, payload) => {
    const request = globalThis.__opensquillaOnboardingProbeTest[payload.index]
    if (!request?.resolveProbe) throw new Error('No synthetic onboarding probe is pending.')
    request.resolveProbe(payload.result)
    request.resolveProbe = null
  }, { index, result })
}

async function assertUnifiedTelemetryNotice(page) {
  assert.equal(await page.locator('input[name="reliabilityDiagnosticsEnabled"], input[name="productAnalyticsEnabled"]').count(), 0)
  assert.equal(await page.locator('[data-i18n="onboarding.telemetry.notice"]').count(), 1)
}

async function pendingSaveState(app) {
  return await app.evaluate(() => {
    const state = globalThis.__opensquillaOnboardingSaveTest
    return {
      callCount: state?.callCount || 0,
      hasPending: Boolean(state?.pending),
      lastPayload: state?.lastPayload || null,
    }
  })
}

async function settlePendingSave(app, outcome) {
  await app.evaluate((_electron, nextOutcome) => {
    const state = globalThis.__opensquillaOnboardingSaveTest
    if (!state?.pending) throw new Error('No synthetic onboarding save is pending.')
    const pending = state.pending
    state.pending = null
    if (nextOutcome.reject) {
      pending.rejectSave(new Error(nextOutcome.error))
      return
    }
    pending.resolveSave(nextOutcome.result)
  }, outcome)
}

// Dispatch the renderer click after the button is visible.  Hosted macOS and
// Windows runners can keep the onboarding card in a CSS transition long
// enough for Playwright's pointer hit-testing to miss the first click; the
// DOM event still exercises the same single-flight handler on every platform.
async function clickFinish(page) {
  const finish = page.locator('#finish')
  await finish.waitFor({ state: 'visible' })
  await finish.evaluate((button) => button.click())
}

async function assertSubmitPending(
  page,
  app,
  expectedCallCount,
  {
    initialStatus = 'Preparing desktop profile',
    savingLabel = 'Saving setup…',
  } = {},
) {
  const form = page.locator('#setup-form')
  const finish = page.locator('#finish')
  const cardBody = page.locator('.card-body')
  const locale = page.locator('#onboardingLocale')
  const skip = page.locator('#skip')
  const submitStatus = page.locator('#submitStatus')
  const providerSelectToggle = page.locator('#providerSelectToggle')
  const providerSelectPanel = page.locator('#providerSelectPanel')
  await waitFor(async () => {
    const state = await pendingSaveState(app)
    return state.callCount === expectedCallCount
      && state.hasPending
      && await finish.isDisabled()
      && await form.getAttribute('aria-busy') === 'true'
  }, 'visible single-flight onboarding submit state')
  assert.equal(await finish.isDisabled(), true)
  assert.equal(await finish.evaluate((button) => button.classList.contains('is-loading')), true)
  assert.equal(await form.getAttribute('aria-busy'), 'true')
  assert.equal(await cardBody.evaluate((card) => card.inert), true)
  assert.equal(await locale.isDisabled(), true)
  assert.equal(await skip.isDisabled(), false)
  assert.equal(await providerSelectToggle.getAttribute('aria-expanded'), 'false')
  assert.equal(await providerSelectPanel.isHidden(), true)
  assert.equal(await submitStatus.isVisible(), true)
  assert.equal(await submitStatus.getAttribute('role'), 'status')
  assert.equal(await submitStatus.getAttribute('aria-live'), 'polite')
  assert.equal(await submitStatus.getAttribute('aria-atomic'), 'true')
  assert.equal((await submitStatus.innerText()).trim(), initialStatus)
  assert.equal((await finish.innerText()).trim(), savingLabel)
}

async function assertSubmitRestored(
  page,
  expectedError,
  expectedApiKey,
  expectedFinishLabel = 'Save and enter',
) {
  const form = page.locator('#setup-form')
  const finish = page.locator('#finish')
  const cardBody = page.locator('.card-body')
  const locale = page.locator('#onboardingLocale')
  const errorBox = page.locator('#error')
  const submitStatus = page.locator('#submitStatus')
  await waitFor(async () => (
    await form.getAttribute('aria-busy') === 'false'
      && !await finish.isDisabled()
      && (await errorBox.innerText()).includes(expectedError)
  ), 'restored onboarding submit state')
  assert.equal(await finish.isDisabled(), false)
  assert.equal(await finish.evaluate((button) => button.classList.contains('is-loading')), false)
  assert.equal(await form.getAttribute('aria-busy'), 'false')
  assert.equal(await cardBody.evaluate((card) => card.inert), false)
  assert.equal(await locale.isDisabled(), false)
  assert.equal(await page.locator('#apiKey').inputValue(), expectedApiKey)
  assert.equal((await finish.innerText()).trim(), expectedFinishLabel)
  assert.equal((await submitStatus.innerText()).trim(), '')
  assert.match(await errorBox.innerText(), new RegExp(expectedError))
  assert.equal(
    await errorBox.evaluate((element) => document.activeElement === element),
    true,
    'submit failure must move focus to the global error',
  )
}

async function verifySubmitFeedbackAndSingleFlight() {
  const { app, userDataDir, userDataRoot } = await launchIsolatedOnboarding(
    'opensquilla-electron-onboarding-submit-test-',
  )
  try {
    const page = await setupWindow(app)
    // Normal startup now keeps the local Desktop renderer mounted while the
    // runtime is unavailable. Load the recovery document explicitly so its
    // timer/progress contract remains covered without restoring the old
    // gateway-owned shell lifecycle.
    await loadBootContractWindow(app)
    await verifyBootPhaseTimer(app)
    const submitClockOrigin = Date.now()
    await page.clock.install({ time: submitClockOrigin })
    await page.clock.pauseAt(submitClockOrigin + 1_000)
    const apiKey = page.locator('#apiKey')
    await apiKey.fill('synthetic-submit-key')
    await page.locator('#onboardingLocale').selectOption('de')
    await assertUnifiedTelemetryNotice(page)
    await installPendingSaveStub(app)

    await page.locator('#providerSelectToggle').click()
    assert.equal(await page.locator('#providerSelectToggle').getAttribute('aria-expanded'), 'true')
    assert.equal(await page.locator('#providerSelectPanel').isVisible(), true)
    await clickFinish(page)
    await assertSubmitPending(page, app, 1, {
      initialStatus: 'Desktop-Profil wird vorbereitet',
      savingLabel: 'Einrichtung wird gespeichert…',
    })
    const immediateSubmitStatus = (await page.locator('#submitStatus').innerText()).trim()
    await page.clock.fastForward(7_999)
    assert.equal(
      (await page.locator('#submitStatus').innerText()).trim(),
      immediateSubmitStatus,
      'slow feedback must not appear before the 8 second boundary',
    )
    await page.clock.fastForward(1)
    const slowSubmitStatus = await waitFor(async () => {
      const value = (await page.locator('#submitStatus').innerText()).trim()
      return value && value !== immediateSubmitStatus ? value : null
    }, 'slow onboarding feedback')
    assert.equal(
      slowSubmitStatus,
      'Das Speichern dauert länger. Du kannst im Client fortfahren.',
    )
    assert.equal(await page.locator('#submitStatus').isVisible(), true)
    assert.equal(await page.locator('#finish').isDisabled(), true)
    assert.equal((await page.locator('#finish').innerText()).trim(), 'Einrichtung wird gespeichert…')
    await assertSubmitActionsDoNotOverlap(page)
    const firstState = await pendingSaveState(app)
    assert.equal(firstState.lastPayload?.apiKey, 'synthetic-submit-key')
    assert.equal(Object.hasOwn(firstState.lastPayload, 'reliabilityDiagnosticsEnabled'), false)
    assert.equal(Object.hasOwn(firstState.lastPayload, 'productAnalyticsEnabled'), false)

    await page.locator('#finish').evaluate((button) => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    })
    await delay(100)
    assert.equal(
      (await pendingSaveState(app)).callCount,
      1,
      'a pending onboarding save must ignore repeated click events',
    )

    await settlePendingSave(app, {
      reject: false,
      result: { ok: false, error: 'Synthetic onboarding save was refused.' },
    })
    await assertSubmitRestored(
      page,
      'Lokales Speichern fehlgeschlagen. Wiederhole es oder richte den Dienst später ein.',
      'synthetic-submit-key',
      'Speichern und öffnen',
    )
    await page.clock.fastForward(8_000)
    assert.equal(
      (await page.locator('#submitStatus').innerText()).trim(),
      '',
      'a failed save must cancel stale slow-feedback timers',
    )
    await page.locator('#onboardingLocale').selectOption('en')

    await clickFinish(page)
    await assertSubmitPending(page, app, 2)
    await settlePendingSave(app, {
      reject: true,
      error: 'Synthetic onboarding save rejected.',
    })
    await assertSubmitRestored(page,
      'Could not save locally. Please retry, or continue in the client and configure it later.',
      'synthetic-submit-key')
    assert.doesNotMatch(await page.locator('#error').innerText(), /Synthetic|Error invoking remote method/,
      'raw IPC exceptions must not be shown as user-facing save errors')

    await clickFinish(page)
    await assertSubmitPending(page, app, 3)
    await settlePendingSave(app, {
      reject: false,
      result: { ok: true },
    })
    await delay(100)
    assert.equal(
      (await pendingSaveState(app)).callCount,
      3,
      'a successful onboarding save must not submit again',
    )
    assert.equal(await page.locator('#finish').isDisabled(), true)
    assert.equal(
      await page.locator('#finish').evaluate((button) => button.classList.contains('is-loading')),
      true,
    )
    assert.equal(await page.locator('#setup-form').getAttribute('aria-busy'), 'true')
    assert.equal(
      (await page.locator('#submitStatus').innerText()).trim(),
      'Preparing desktop profile',
    )
    await page.clock.fastForward(8_000)
    assert.equal(
      (await page.locator('#submitStatus').innerText()).trim(),
      'Preparing desktop profile',
      'a successful save must clear its slow-feedback timer while the window closes',
    )
  } catch (error) {
    await reportOnboardingFailure(app, 'submit-feedback', error)
    throw error
  } finally {
    await app.close().catch(() => {})
    await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
  }
}

await runCase('submit', verifySubmitFeedbackAndSingleFlight)

async function verifyOptionalProbeDoesNotBlockPersistence() {
  const probeServer = await startOnboardingProbeServer('reject')
  const { app, userDataDir, userDataRoot } = await launchIsolatedOnboarding(
    'opensquilla-electron-onboarding-probe-test-',
  )
  const credentialPath = join(userDataDir, 'desktop-credential.json')
  const configPath = join(userDataDir, 'opensquilla', 'config.toml')
  const syntheticKey = 'synthetic-probe-retry-key'
  try {
    const page = await setupWindow(app)
    await readyDesktopWindow(app)
    const initialConfig = await readFile(configPath, 'utf8')
    assert.equal(await fileExists(credentialPath), false)
    await page.locator('#providerSelectToggle').click()
    await page.locator('[data-provider-option="openai"]').click()
    await page.locator('#apiKey').fill(syntheticKey)
    assert.equal(await page.locator('#model').inputValue(), '', 'OpenAI does not receive a preset model')
    assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
    await page.locator('#model').fill('synthetic-user-selected-model')
    await setOnboardingBaseUrl(page, probeServer.baseUrl)
    await assertUnifiedTelemetryNotice(page)
    const submittedModel = await page.locator('#model').inputValue()

    await page.locator('#probe').click()
    const errorText = await waitFor(async () => {
      const text = [
        await page.locator('#apiKeyError').innerText(),
        await page.locator('#probeStatus').innerText(),
      ].join(' ').trim()
      return /rejected|authentication|credential|API key/i.test(text)
        && !await page.locator('#probe').isDisabled() ? text : null
    }, 'optional provider probe to report rejected credentials')
    assert.equal(await page.locator('#apiKeyError').innerText(), 'The key was rejected. Check it and paste it again.')
    assert.doesNotMatch(errorText, /Synthetic|401|Error invoking remote method/)
    assert.equal(await page.locator('#finish').isDisabled(), false)
    assert.equal(await page.locator('#skip').isDisabled(), false)
    assert.notEqual(await page.locator('#setup-form').getAttribute('aria-busy'), 'true',
      'an optional probe must not make the form busy')
    assert.equal(errorText.includes(syntheticKey), false, 'probe errors must redact the submitted key')
    assert.equal(await page.locator('#apiKey').inputValue(), syntheticKey)
    assert.equal(await fileExists(credentialPath), false, 'an optional probe must not persist credentials')
    assert.equal(await readFile(configPath, 'utf8'), initialConfig, 'a probe must not mutate the empty startup config')
    assert.deepEqual(await readOnboardingTelemetry(userDataDir), [], 'testing is not a save attempt')
    assert.equal(probeServer.requests.length, 1)
    assert.equal(probeServer.requests[0].method, 'POST')
    assert.equal(probeServer.requests[0].url, '/v1/chat/completions')
    assert.equal(probeServer.requests[0].authorization, `Bearer ${syntheticKey}`)
    assert.equal(JSON.parse(probeServer.requests[0].body).model, submittedModel)

    probeServer.setMode('disconnect')
    await page.locator('#probe').click()
    await waitFor(async () => {
      const text = await page.locator('#probeStatus').innerText()
      return /connect|network|unavailable|reach|连接|网络/i.test(text)
        && !await page.locator('#probe').isDisabled()
    }, 'optional probe to report network failure')
    assert.equal(await page.locator('#probeStatus').innerText(), 'Unable to connect right now. You can still save and enter.')
    assert.equal(await page.locator('#apiKey').getAttribute('aria-invalid'), null,
      'a network error must not mark the API key as invalid')
    assert.equal(await page.locator('#finish').isDisabled(), false)
    assert.equal(await page.locator('#skip').isDisabled(), false)
    if (screenshotPath) await page.screenshot({ path: screenshotPath.replace(/\.png$/i, '') + '-retry.png' })
    const requestsBeforeSave = probeServer.requests.length
    await page.locator('#finish').click()
    const saved = await waitFor(async () => {
      if (!await fileExists(credentialPath) || !await fileExists(configPath)) return null
      return JSON.parse(await readFile(credentialPath, 'utf8'))
    }, 'save after failed optional probes to persist onboarding settings')
    assert.equal(saved.provider, 'openai')
    assert.equal(saved.model, submittedModel)
    assert.equal(saved.baseUrl, probeServer.baseUrl)
    await waitFor(() => page.isClosed(), 'saved onboarding panel to close')
    await readyDesktopWindow(app)
    assert.equal(probeServer.requests.length, requestsBeforeSave,
      'save must not repeat or require a provider probe, even while the network is unavailable')
  } catch (error) {
    await reportOnboardingFailure(app, 'optional-probe', error)
    throw error
  } finally {
    await app.close().catch(() => {})
    await probeServer.close().catch(() => {})
    await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
  }
}

await runCase('optional-probe', verifyOptionalProbeDoesNotBlockPersistence)

async function verifyEmptySetupCanBeDismissedAndStaysDismissed(action = 'skip') {
  const prefix = `opensquilla-electron-onboarding-${action}-test-`
  const initial = await launchIsolatedOnboarding(prefix)
  let app = initial.app
  let phase = 'setup-window'
  try {
    const page = await setupWindow(app)
    phase = 'initial-gateway-ready'
    const desktop = await readyDesktopWindow(app)
    phase = `dismiss-${action}`
    const initialWindows = await app.evaluate(({ BrowserWindow }) => (
      BrowserWindow.getAllWindows().map(window => ({
        title: window.webContents.getTitle(),
        url: window.webContents.getURL(),
        modal: window.isModal(),
        enabled: window.isEnabled(),
      }))
    ))
    assert.equal(initialWindows.some(window => window.modal), false,
      'first-run configuration must not disable the client behind a modal window')
    assert.equal(initialWindows.find(window => window.url.startsWith('opensquilla-app://desktop/'))?.enabled, true)
    assert.equal(await page.locator('#apiKey').inputValue(), '')
    if (action === 'close') {
      const title = await page.title()
      await app.evaluate(({ BrowserWindow }, setupTitle) => {
        const window = BrowserWindow.getAllWindows().find(candidate => (
          candidate.webContents.getTitle() === setupTitle
        ))
        if (!window) throw new Error('Onboarding window is unavailable.')
        window.close()
      }, title)
    } else {
      await page.locator('#skip').click()
    }
    await waitFor(() => page.isClosed(), 'empty configuration panel to close')
    assert.equal(desktop.isClosed(), false, 'skipping must not quit the client')
    assert.equal(await fileExists(join(initial.userDataDir, 'desktop-credential.json')), false,
      'skipping must not invent provider credentials')
    const config = parse(await readFile(join(initial.userDataDir, 'opensquilla', 'config.toml'), 'utf8'))
    assert.equal(config.llm?.provider || '', '')
    assert.equal(config.llm?.model || '', '')
    phase = 'initial-shutdown'
    await app.close()

    phase = 'relaunch'
    app = (await launchIsolatedOnboarding(prefix, initial.userDataRoot)).app
    phase = 'restarted-gateway-ready'
    await readyDesktopWindow(app)
    phase = 'no-repeated-onboarding'
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      assert.equal(await candidate.locator('#setup-form').count(), 0,
        'restarting after skip must not ask for model setup again')
    }
  } catch (error) {
    await reportOnboardingFailure(app, phase, error)
    throw error
  } finally {
    await app.close().catch(() => {})
    await rm(initial.userDataRoot, { recursive: true, force: true }).catch(() => {})
  }
}

await runCase('skip', () => verifyEmptySetupCanBeDismissedAndStaysDismissed())
await runCase('close', () => verifyEmptySetupCanBeDismissedAndStaysDismissed('close'))

async function verifySlowProbeDoesNotOwnSetupActions() {
  const { app, userDataDir, userDataRoot } = await launchIsolatedOnboarding(
    'opensquilla-electron-onboarding-slow-probe-test-',
  )
  let phase = 'setup-window'
  try {
    const page = await setupWindow(app)
    phase = 'initial-gateway-ready'
    const desktop = await readyDesktopWindow(app)
    phase = 'probe-edit-races'
    await installPendingProbeStub(app)
    await page.locator('#apiKey').fill('synthetic-original-key')

    await page.locator('#probe').click()
    await waitFor(async () => await pendingProbeCount(app) === 1, 'first pending probe')
    assert.equal(await page.locator('#finish').isDisabled(), false)
    assert.equal(await page.locator('#skip').isDisabled(), false)

    await page.locator('#apiKey').fill('synthetic-edited-key')
    assert.equal(await page.locator('#probeStatus').innerText(), 'Not tested')
    await page.locator('#probe').click()
    await waitFor(async () => await pendingProbeCount(app) === 2, 'new probe after editing')
    await settlePendingProbe(app, 1, { ok: true, latencyMs: 17 })
    await waitFor(async () => (await page.locator('#probeStatus').innerText()) === 'Verified · 17 ms',
      'current probe success')
    await settlePendingProbe(app, 0, { ok: false, failureKind: 'auth_invalid', message: 'STALE REJECTION' })
    // A round trip through the renderer drains the queued IPC completion.
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)))
    assert.equal(await page.locator('#probeStatus').innerText(), 'Verified · 17 ms',
      'a late result for the previous key must not overwrite the current result')
    assert.equal(await page.locator('#apiKey').getAttribute('aria-invalid'), null)

    phase = 'probe-pending-during-save'
    await page.locator('#probe').click()
    await waitFor(async () => await pendingProbeCount(app) === 3, 'probe pending before save')
    await installPendingSaveStub(app)
    await clickFinish(page)
    await assertSubmitPending(page, app, 1)
    await settlePendingProbe(app, 2, { ok: false, failureKind: 'auth_invalid', message: 'STALE SAVE REJECTION' })
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)))
    assert.equal(await page.locator('#apiKeyError').innerText(), '',
      'a probe completing during save must not introduce a credential error')
    assert.equal((await pendingSaveState(app)).hasPending, true,
      'the independent save must remain owned by its own completion')
    await settlePendingSave(app, { reject: true, error: 'Synthetic local write failed.' })
    await assertSubmitRestored(page,
      'Could not save locally. Please retry, or continue in the client and configure it later.',
      'synthetic-edited-key')
    assert.equal(await page.locator('#probe').isDisabled(), false,
      'a failed save must leave optional testing usable after an older probe finishes')
    assert.equal(await page.locator('#probeStatus').innerText(), 'Not tested')

    phase = 'probe-pending-during-skip'
    await page.locator('#probe').click()
    await waitFor(async () => await pendingProbeCount(app) === 4, 'probe pending before skip')
    await page.locator('#skip').click()
    await waitFor(() => page.isClosed(), 'skip to close setup without waiting for the probe')
    await settlePendingProbe(app, 3, { ok: true, latencyMs: 9000 })
    assert.equal(desktop.isClosed(), false)
    assert.equal((await desktop.evaluate(() => window.opensquillaDesktop.getGatewayConnection())).status, 'ready')
    assert.equal(await fileExists(join(userDataDir, 'desktop-credential.json')), false,
      'a late probe must not persist credentials or complete a dismissed configuration')
  } catch (error) {
    await reportOnboardingFailure(app, phase, error)
    throw error
  } finally {
    await app.close().catch(() => {})
    await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
  }
}

await runCase('slow-probe', verifySlowProbeDoesNotOwnSetupActions)

// The remaining integration scenario is the legacy top-level client workflow.
// Selected regression runs may finish here; the default still runs every case.
if (!caseSelected('provider-client')) process.exit(0)
console.log('RUN onboarding provider-client')
const successfulProbeServer = await startOnboardingProbeServer()

const { app, userDataDir, userDataRoot } = await launchIsolatedOnboarding(
  'opensquilla-electron-onboarding-test-',
)
const rendererDiagnostics = []
const observeRenderer = (candidate) => {
  candidate.on('console', (message) => rendererDiagnostics.push(`console:${message.type()}:${message.text()}`))
  candidate.on('pageerror', (error) => rendererDiagnostics.push(`pageerror:${error.message || error}`))
}
for (const candidate of app.windows()) observeRenderer(candidate)
app.on('window', observeRenderer)

try {
  const page = await setupWindow(app)
  const desktopPage = await waitFor(async () => {
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      if (
        candidate.url().startsWith('opensquilla-app://desktop/')
        && await candidate.locator('#app').count() === 1
      ) return candidate
    }
    return null
  }, 'local Desktop renderer')
  assert.equal(
    desktopPage.url(),
    'opensquilla-app://desktop/chat/new',
    'the local Desktop renderer must exist before onboarding and Gateway readiness',
  )
  assert.equal(await desktopPage.locator('#app').count(), 1)
  await readyDesktopWindow(app)
  const startingConnection = await desktopPage.evaluate(
    () => window.opensquillaDesktop?.getGatewayConnection?.(),
  )
  assert.equal(startingConnection?.status, 'ready', 'the client must start before the user completes model setup')
  assert.match(startingConnection?.wsUrl || '', /^ws:\/\/127\.0\.0\.1:\d+\/ws$/)
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message || String(error)))
  const providerScreen = page.locator('[data-screen="1"]')
  async function chooseProvider(id) {
    await page.locator('#providerSelectToggle').click()
    await page.locator(`[data-provider-option="${id}"]`).click()
  }

  await page.locator('#onboardingLocale').selectOption('zh-Hans')
  assert.deepEqual(pageErrors, [], 'onboarding should not raise page-script errors during locale rendering')
  assert.equal(await page.evaluate(() => document.documentElement.lang), 'zh-Hans')
  assert.equal(await page.title(), '设置 OpenSquilla')
  assert.equal(await page.locator('[data-screen="0"]').count(), 0, 'setup-depth selection must be removed')
  assert.equal(await page.locator('[data-screen="2"], [data-screen="3"], [data-screen="4"]').count(), 0, 'onboarding must use a single setup screen')
  assert.equal(await page.locator('[data-setup-mode], [data-model-routing-mode]').count(), 0, 'advanced setup controls must be removed')
  assert.equal(await page.locator('.rail, .progress, .step').count(), 0, 'onboarding must not render a side rail or step tracker')
  assert.equal(await page.locator('.topbar .brand').innerText(), 'OpenSquilla')
  assert.equal(await page.locator('.eyebrow, .card-badge').count(), 0, 'decorative step labels and badges must be removed')
  assert.equal(await page.locator('#providerHint').count(), 0, 'provider hint banner must be removed')
  assert.equal(await providerScreen.isVisible(), true, 'onboarding should open directly on provider setup')
  assert.equal(await page.locator('.step-switcher, [data-route-step]').count(), 0, 'onboarding should not render a numbered step switcher')
  assert.equal(await providerScreen.locator('.context-label').count(), 0)
  assert.equal(await providerScreen.locator('h2').innerText(), '模型服务配置')
  assert.equal(await providerScreen.locator('.card-head > p').innerText(), '连接模型服务，或稍后在设置中完成。')
  assert.equal(await page.locator('#apiKeyRequiredMarker').innerText(), '*')
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), true)
  assert.equal(
    await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),
    '#BA4D0F',
    'onboarding should use the in-app light-theme accent',
  )
  await page.mouse.move(0, 0)
  assert.equal(
    await page.locator('#finish').evaluate((button) => getComputedStyle(button).backgroundColor),
    'rgb(52, 58, 64)',
    'the single primary action should use the softer graphite treatment',
  )
  assert.equal(await page.locator('#finish').innerText(), '保存并进入')
  assert.equal(await page.locator('#skip').innerText(), '稍后配置')
  assert.equal(await page.locator('#cancel').count(), 0, 'configuration must not make quitting the only alternative to saving')
  assert.equal(await page.locator('.next-button, .back-button').count(), 0, 'single-page onboarding must not render next or back actions')
  assert.equal(await providerScreen.locator('.provider-feature, .provider-disclosure').count(), 0, 'provider setup should use one unified select')
  assert.equal(await providerScreen.locator('.provider-promo').count(), 0, 'the promotion should not occupy a separate row')
  assert.equal(await providerScreen.locator('.provider-promo-token').count(), 0)
  assert.equal(await providerScreen.locator('.provider-promo-copy').isVisible(), true)
  assert.equal(await providerScreen.locator('.provider-promo-copy strong').innerText(), 'TokenRhythm 限时福利')
  assert.equal(await providerScreen.locator('.provider-promo-copy span').count(), 0)
  assert.equal(
    await providerScreen.locator('#tokenrhythmRegister').getAttribute('aria-label'),
    '限时福利（在外部浏览器中打开）',
  )
  assert.equal(
    await providerScreen.locator('.provider-promo-copy strong').evaluate((copy) => getComputedStyle(copy).color),
    'rgb(186, 77, 15)',
  )
  assert.equal(await page.locator('#endpointPanel, #endpointToggle').count(), 0, 'simple onboarding should not expose endpoint controls')
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'TokenRhythm should be selected by default')
  assert.equal(await page.locator('.provider-field-head').count(), 0)
  assert.equal(await page.locator('#providerSelectLabel').innerText(), '提供商')
  assert.equal(await page.locator('#providerSelectValue').innerText(), 'TokenRhythm')
  assert.equal(
    await page.locator('#providerSelectToggle').evaluate((toggle) => getComputedStyle(toggle).backgroundColor),
    'rgb(247, 248, 247)',
    'the provider row should share the recommended-model surface',
  )
  assert.equal(
    await page.locator('#providerSelectToggle').evaluate((toggle) => getComputedStyle(toggle).borderTopWidth),
    '0px',
    'the provider row should use the same borderless treatment as the recommended-model row',
  )
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#modelEditor').isVisible(), false)
  assert.equal(await page.locator('#modelSummaryLabel').innerText(), '推荐模型')
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-flash')
  assert.deepEqual(
    await page.evaluate(() => [
      getComputedStyle(document.getElementById('providerSelectLabel')).fontSize,
      getComputedStyle(document.getElementById('providerSelectValue')).fontSize,
      getComputedStyle(document.getElementById('modelSummaryLabel')).fontSize,
      getComputedStyle(document.getElementById('modelSummaryValue')).fontSize,
    ]),
    ['11.5px', '11.5px', '11.5px', '11.5px'],
    'provider and recommended-model rows should use one consistent font size',
  )
  assert.equal(await page.locator('#modelEditToggle').innerText(), '')
  assert.equal(await page.locator('#modelEditToggle').getAttribute('aria-label'), '修改')
  assert.equal(await page.locator('#modelEditToggle svg').count(), 1)
  assert.equal(
    await page.locator('#modelEditToggle').evaluate((button) => getComputedStyle(button).color),
    'rgb(122, 129, 138)',
    'the edit icon should use a neutral gray treatment',
  )
  await page.locator('#modelEditToggle').click()
  assert.equal(await page.locator('#modelSummary').isVisible(), false)
  assert.equal(await page.locator('#modelEditor').isVisible(), true)
  assert.equal(await page.locator('label[for="model"] > .field-label-text').innerText(), '模型名称')
  assert.equal(await page.locator('#modelEditDone').innerText(), '完成')
  await page.locator('#modelEditDone').click()
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#apiKey').getAttribute('placeholder'), 'sk-...')
  assert.equal(await page.evaluate(() => typeof window.opensquillaDesktop.probeOnboarding), 'function')
  assert.equal(
    await page.locator('#verifyProvider, #providerVerifyStatus, #providerVerifyError, .provider-verify-inline').count(),
    0,
    'retired provider verification controls must remain removed',
  )
  assert.equal(await page.locator('#probe').isVisible(), true)
  assert.equal(await page.locator('#probe').innerText(), '测试连接（可选）')
  const apiKeyLabelBox = await page.locator('.api-key-label').boundingBox()
  const providerLabelBox = await page.locator('#providerSelectLabel').boundingBox()
  const promoTitleBox = await page.locator('.provider-promo-copy strong').boundingBox()
  const claimButtonBox = await page.locator('#tokenrhythmRegister').boundingBox()
  const initialApiKeyBox = await page.locator('#apiKey').boundingBox()
  assert.ok(
    apiKeyLabelBox && providerLabelBox
      && Math.abs(apiKeyLabelBox.x - providerLabelBox.x) <= 1,
    'the API-key heading should align with the inset provider label',
  )
  assert.ok(
    apiKeyLabelBox && promoTitleBox
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (promoTitleBox.y + promoTitleBox.height / 2),
      ) <= 3,
    'the limited-time promotion should share the API-key heading row',
  )
  assert.ok(
    apiKeyLabelBox && claimButtonBox
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (claimButtonBox.y + claimButtonBox.height / 2),
      ) <= 3,
    'the claim button should share the API-key heading row',
  )
  assert.ok(
    claimButtonBox && initialApiKeyBox
      && Math.abs(
        (claimButtonBox.x + claimButtonBox.width)
        - (initialApiKeyBox.x + initialApiKeyBox.width),
      ) <= 2,
    'the claim button should align to the right edge of the API-key input',
  )
  assert.equal(
    await page.locator('#providerSelectedBadges .provider-badge').count(),
    0,
    'the closed provider row should not repeat the limited-time promotion badge',
  )
  await page.locator('#providerSelectToggle').click()
  assert.equal(await page.locator('#providerSelectToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('#providerSelectPanel').isVisible(), true)
  assert.equal(await page.locator('#providerSearch, .provider-search-wrap').count(), 0, 'the provider list should open directly without a search field')
  assert.equal(
    await page.locator('[data-provider-option="tokenrhythm"]').evaluate((option) => document.activeElement === option),
    true,
  )
  assert.deepEqual(
    await page.locator('[data-provider-option="tokenrhythm"] .provider-badge').allInnerTexts(),
    ['限时免费'],
    'TokenRhythm should expose only the limited-time badge in the provider list',
  )
  assert.equal(await page.locator('[data-provider-group="recommended"] .provider-option-group-label').innerText(), '推荐')
  assert.equal(await page.locator('[data-provider-group="cloud"] .provider-option-group-label').innerText(), '云端服务')
  assert.equal(await page.locator('[data-provider-group="local"] .provider-option-group-label').innerText(), '本地服务')
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#providerSelectPanel').isVisible(), false)
  await page.locator('#finish').click()
  const apiKeyInput = page.locator('#apiKey')
  const apiKeyError = page.locator('#apiKeyError')
  assert.match(await apiKeyError.innerText(), /需要 TokenRhythm API 密钥/)
  assert.equal(await apiKeyInput.getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '', 'field validation must not use the global error region')
  const apiKeyBox = await apiKeyInput.boundingBox()
  const apiKeyErrorBox = await apiKeyError.boundingBox()
  const providerSelectBox = await page.locator('#providerSelectToggle').boundingBox()
  assert.ok(providerSelectBox && apiKeyBox && providerSelectBox.y + providerSelectBox.height <= apiKeyBox.y, 'provider selector must render above the API-key field')
  assert.ok(apiKeyBox && apiKeyErrorBox && apiKeyErrorBox.y >= apiKeyBox.y + apiKeyBox.height, 'API-key error must render below its input')
  await apiKeyInput.fill('temporary-key')
  assert.equal(await apiKeyError.innerText(), '', 'editing the API key should clear its field error')
  assert.equal(await apiKeyInput.getAttribute('aria-invalid'), null)
  await apiKeyInput.fill('')

  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm')
  assert.equal(await page.locator('#baseUrl').inputValue(), 'https://tokenrhythm.studio/v1')
  assert.equal(await page.locator('#model').inputValue(), 'deepseek-flash')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')

  const tokenRhythmCta = page.locator('#tokenrhythmRegister')
  assert.equal(await tokenRhythmCta.innerText(), '限时福利')
  assert.equal(
    await tokenRhythmCta.evaluate((link) => getComputedStyle(link, '::after').content),
    '"↗"',
    'external registration action should expose a direction cue',
  )
  assert.equal(
    await tokenRhythmCta.evaluate((link) => getComputedStyle(link).backgroundColor),
    'rgb(186, 77, 15)',
    'the registration call to action should use the canonical light-theme accent',
  )
  assert.equal(await tokenRhythmCta.evaluate((link) => getComputedStyle(link).color), 'rgb(255, 255, 255)')
  assert.equal(await tokenRhythmCta.evaluate((link) => getComputedStyle(link).borderRadius), '7px')
  assert.equal(await tokenRhythmCta.getAttribute('href'), 'https://tokenrhythm.studio/register')
  assert.equal(await tokenRhythmCta.getAttribute('target'), '_blank')
  assert.equal(await tokenRhythmCta.getAttribute('rel'), 'noopener noreferrer')
  assert.equal(await tokenRhythmCta.isVisible(), true)
  assert.equal(await page.locator('#providerMoreToggle, #providerMorePanel, #providerGrid, .provider').count(), 0)

  await page.locator('#onboardingLocale').selectOption('en')
  assert.equal(await providerScreen.locator('h2').innerText(), 'Model service setup')
  assert.equal(await page.locator('#provider').inputValue(), 'tokenrhythm', 'locale changes should preserve the selected provider')

  await chooseProvider('openrouter')
  assert.notEqual(await page.locator('#model').inputValue(), '', 'OpenRouter keeps its model preset')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#modelSummary').isVisible(), true)

  await chooseProvider('minimax_cn', 'MiniMax Mainland')
  assert.equal(await page.locator('#provider').inputValue(), 'minimax_cn')
  assert.equal(await tokenRhythmCta.isVisible(), true, 'the promotion should remain available when another provider is selected')
  assert.equal(await page.locator('#providerSelectedBadges .provider-badge').count(), 0)
  assert.equal(await page.locator('#model').inputValue(), '')
  assert.equal(await page.locator('#modelSummary').isVisible(), false)
  assert.equal(await page.locator('#modelEditor').isVisible(), true)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
  assert.equal(await page.locator('#routerMode').inputValue(), 'disabled')
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), true)

  await chooseProvider('ollama', 'Ollama')
  assert.equal(await page.locator('#provider').inputValue(), 'ollama')
  assert.equal(await page.locator('#apiKeyRequiredMarker').isVisible(), false)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'direct')
  assert.equal(await page.locator('#routerMode').inputValue(), 'disabled')
  assert.equal(await page.locator('#model').inputValue(), '')
  assert.equal(await page.locator('#modelSummary').isVisible(), false)
  assert.equal(await page.locator('#modelEditor').isVisible(), true)
  assert.equal(await page.locator('#modelRequiredMarker').isVisible(), true)
  await page.locator('#finish').click()
  assert.equal(await providerScreen.isVisible(), true, 'invalid direct-model setup must remain on the provider screen')
  assert.match(await page.locator('#modelError').innerText(), /Direct model is required/)
  assert.equal(await page.locator('#model').getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '')

  await chooseProvider('tokenrhythm', 'TokenRhythm')
  assert.equal(await tokenRhythmCta.isVisible(), true)
  assert.equal(await page.locator('#providerSelectedBadges .provider-badge').count(), 0)
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-flash')
  await page.locator('#apiKey').fill('synthetic-tokenrhythm-key')
  assert.equal(await page.locator('.inline-search-section').isVisible(), true)
  assert.equal(await page.locator('#inlineSearchHeading').innerText(), 'Choose web search')
  assert.equal(await page.locator('.inline-search-optional').innerText(), 'Optional')
  assert.equal(await page.locator('#inlineSearchToggle').getAttribute('aria-expanded'), 'false')
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), false)
  await page.locator('#inlineSearchToggle').click()
  assert.equal(await page.locator('#inlineSearchToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), true)
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"]').evaluate((choice) => getComputedStyle(choice).backgroundColor),
    'rgb(247, 248, 247)',
    'the selected default search should use the same neutral surface as the recommended model row',
  )
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"]').evaluate((choice) => getComputedStyle(choice).boxShadow),
    'none',
    'the selected default search should not add a separate accent rail',
  )
  assert.equal(
    await page.locator('[data-search-provider="duckduckgo"] .search-provider-billing').evaluate((billing) => getComputedStyle(billing).color),
    'rgb(142, 58, 10)',
    'the free status should use the canonical deep light-theme accent',
  )
  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await page.screenshot({ path: screenshotPath })
  }
  assert.equal(await page.locator('#searchHint, .note').count(), 0, 'search provider descriptions should not be repeated in a separate banner')
  assert.equal(await page.locator('[data-search-provider="duckduckgo"] .search-provider-billing').innerText(), 'Free')
  assert.equal(await page.locator('#searchPaidToggle').getAttribute('aria-expanded'), 'false')
  assert.equal(await page.locator('[data-search-provider="bocha"]').isVisible(), false)
  assert.equal(await page.locator('#searchKeyLabel').isVisible(), false)
  await page.locator('#searchPaidToggle').click()
  assert.equal(await page.locator('#searchPaidToggle').getAttribute('aria-expanded'), 'true')
  assert.equal(await page.locator('[data-search-provider="bocha"]').isVisible(), true)
  assert.equal(await page.locator('[data-search-provider="bocha"] .search-provider-billing').innerText(), 'Paid')
  await page.locator('[data-search-provider="bocha"]').click()
  assert.equal(await page.locator('[data-search-provider-option="bocha"] #searchKeyLabel').isVisible(), true)
  assert.equal(await page.locator('#searchKeyLabel .required-marker').innerText(), '*')
  assert.equal(await page.locator('#searchApiKey').getAttribute('placeholder'), 'BOCHA_SEARCH_API_KEY')
  await page.locator('#inlineSearchToggle').click()
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), false)
  await page.locator('#finish').click()
  assert.equal(await page.locator('#inlineSearchPanel').isVisible(), true, 'search validation should reopen the collapsed section')
  assert.match(await page.locator('#searchApiKeyError').innerText(), /Bocha search API key is required/)
  assert.equal(await page.locator('#searchApiKey').getAttribute('aria-invalid'), 'true')
  assert.equal(await page.locator('#error').innerText(), '')
  await page.locator('[data-search-provider="duckduckgo"]').click()
  assert.equal(await page.locator('#searchKeyLabel').isVisible(), false)
  assert.equal(await page.locator('#searchApiKeyError').innerText(), '')
  assert.equal(await page.locator('#apiKey').inputValue(), 'synthetic-tokenrhythm-key')
  await setOnboardingBaseUrl(page, successfulProbeServer.baseUrl)
  await assertUnifiedTelemetryNotice(page)
  const earlySpoolRoot = join(
    userDataDir,
    'opensquilla',
    'state',
    'telemetry',
    'desktop-early-spool',
  )
  await page.locator('#finish').click()

  const saved = await waitFor(async () => {
    const credential = JSON.parse(await readFile(join(userDataDir, 'desktop-credential.json'), 'utf8'))
    if (credential.provider !== 'tokenrhythm') return null
    const config = await readFile(join(userDataDir, 'opensquilla', 'config.toml'), 'utf8')
    return { credential, config }
  }, 'saved simple onboarding credential and config')
  const { credential, config } = saved
  const onboardingTelemetry = await waitFor(async () => {
    const records = await readOnboardingTelemetry(userDataDir)
    return records.some((record) => (
      record.event === 'onboarding_save_finished' && record.outcome === 'ok'
    )) ? records : null
  }, 'completed local onboarding timing trace')
  assertOnboardingTelemetrySchema(onboardingTelemetry, 'synthetic-tokenrhythm-key')
  assert.equal(credential.provider, 'tokenrhythm')
  assert.equal(credential.modelRoutingMode, 'squilla_router')
  assert.equal(credential.routerMode, 'recommended')
  assert.equal(credential.routerPresetBinding, 'follow_primary')
  assert.match(config, /preset_binding = "follow_primary"/)
  assert.doesNotMatch(config, /tier_profile\s*=/)
  assert.doesNotMatch(config, /reliability_diagnostics_enabled|product_analytics_enabled/)
  assert.doesNotMatch(config, /(?:reliability|product_analytics)_(?:notice_version|consented_at_utc)/)
  const consentMirror = JSON.parse(await readFile(
    join(userDataDir, 'opensquilla', 'state', 'telemetry', 'desktop-consent-mirror.json'),
    'utf8',
  ))
  assert.deepEqual(consentMirror.reliability, {
    enabled: true,
    notice_version: 'reliability-v1',
    consented_at_utc: null,
    forced_off: false,
  })
  assert.deepEqual(consentMirror.growth, {
    enabled: true,
    notice_version: 'growth-v2',
    consented_at_utc: null,
    forced_off: false,
  })
  const reliabilitySpool = await readDirectoryOrEmpty(join(earlySpoolRoot, 'reliability'))
  assert.equal(reliabilitySpool.some(isManagedTelemetrySpoolEntry), false, 'automated UI tests must not produce telemetry')
  const remainingGrowthSpool = await readDirectoryOrEmpty(join(earlySpoolRoot, 'growth'))
  assert.equal(remainingGrowthSpool.some(isManagedTelemetrySpoolEntry), false)
  assert.equal(credential.routerDefaultTier, 'c1')
  assert.equal(credential.model, 'deepseek-flash')
  assert.equal(credential.routerTiers.c0.model, 'qwen3.7-flash')
  assert.equal(credential.routerTiers.c1.model, 'deepseek-flash')
  assert.equal(credential.routerTiers.c2.model, 'deepseek-v4-pro-0813')
  assert.equal(credential.routerTiers.c3.model, 'glm-5.3')
  assert.equal(Object.hasOwn(credential.routerTiers.c0, 'supportsImage'), false)
  assert.equal(Object.hasOwn(credential.routerTiers.c1, 'supportsImage'), false)
  assert.equal(Object.hasOwn(credential.routerTiers.c2, 'supportsImage'), false)
  assert.equal(Object.hasOwn(credential.routerTiers.c3, 'supportsImage'), false)
  assert.equal(credential.routerTiers.c3.ensembleEnabled, false)
  assert.equal(credential.routerTiers.image_model.model, 'kimi-k2.6')
  assert.equal(Object.hasOwn(credential.routerTiers.image_model, 'supportsImage'), false)
  assert.match(config, /\[squilla_router\]\nenabled = true/)
  assert.match(config, /\[llm\][\s\S]*?model = "deepseek-flash"/)
  assert.match(config, /\[squilla_router\.tiers\.c0\]\nprovider = "tokenrhythm"\nmodel = "qwen3.7-flash"/)
  assert.match(config, /\[squilla_router\.tiers\.c1\]\nprovider = "tokenrhythm"\nmodel = "deepseek-flash"/)
  assert.match(config, /\[squilla_router\.tiers\.c2\]\nprovider = "tokenrhythm"\nmodel = "deepseek-v4-pro-0813"/)
  assert.match(config, /\[squilla_router\.tiers\.c3\][\s\S]*?model = "glm-5.3"[\s\S]*?ensemble_enabled = false/)
  assert.doesNotMatch(config, /thinking_level\s*=/)
  assert.doesNotMatch(config, /supports_image\s*=/)
  assert.match(config, /\[llm_ensemble\]\nenabled = false/)
  assert.equal(successfulProbeServer.requests.length, 0,
    'saving complete, untested configuration must not make a provider request')

  const readyConnection = await waitFor(async () => {
    const connection = await desktopPage.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    )
    return connection?.status === 'ready' ? connection : null
  }, 'ready Desktop Gateway descriptor')
  assert.match(readyConnection.httpUrl, /^http:\/\/127\.0\.0\.1:\d+$/)
  assert.equal(
    readyConnection.wsUrl,
    readyConnection.httpUrl.replace(/^http:/, 'ws:') + '/ws',
  )
  assert.equal(typeof readyConnection.instanceId, 'string')
  const readyRendererState = await desktopPage.evaluate(() => {
    const banner = document.getElementById('desktop-runtime-banner')
    return {
      appChildren: document.querySelector('#app')?.childElementCount ?? -1,
      bannerHidden: banner?.hidden ?? null,
      bannerState: banner?.dataset.state || '',
      bannerText: banner?.textContent || '',
      scripts: [...document.scripts].map(script => script.src || '<inline>'),
    }
  })
  assert.equal(
    readyRendererState.bannerHidden,
    true,
    `the local renderer should stay loaded and hide its runtime banner after readiness: ${JSON.stringify({ readyRendererState, rendererDiagnostics })}`,
  )

  const apiBoundary = await desktopPage.evaluate(async () => {
    const response = await fetch('/api/system/status')
    return {
      status: response.status,
      csp: response.headers.get('content-security-policy') || '',
      nosniff: response.headers.get('x-content-type-options') || '',
    }
  })
  assert.equal(apiBoundary.status, 200)
  assert.match(apiBoundary.csp, /sandbox/)
  assert.match(apiBoundary.csp, /frame-ancestors 'none'/)
  assert.equal(apiBoundary.nosniff, 'nosniff')

  await desktopPage.evaluate(() => window.location.assign('/api/system/status'))
  await delay(300)
  assert.equal(
    desktopPage.url(),
    'opensquilla-app://desktop/chat/new',
    'API responses must not replace the privileged Desktop document',
  )

  const childFrameBoundary = await desktopPage.evaluate(async () => {
    const frame = document.createElement('iframe')
    frame.src = '/api/system/status'
    document.body.appendChild(frame)
    await new Promise(resolve => setTimeout(resolve, 300))
    let location = 'inaccessible'
    try { location = frame.contentWindow?.location.href || '' } catch {}
    let bridge = 'inaccessible'
    try { bridge = typeof frame.contentWindow?.opensquillaDesktop } catch {}
    frame.remove()
    return { bridge, location }
  })
  assert.notEqual(childFrameBoundary.location, 'opensquilla-app://desktop/api/system/status')
  assert.notEqual(childFrameBoundary.bridge, 'object')

  const browserControlWindowId = await app.evaluate(async ({ BrowserWindow }, url) => {
    const window = new BrowserWindow({
      show: false,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    await window.loadURL(url)
    return window.id
  }, `${readyConnection.httpUrl}/control/`)
  const browserControlPage = await waitFor(async () => {
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      if (candidate.url().startsWith(`${readyConnection.httpUrl}/control`)) return candidate
    }
    return null
  }, 'browser Control UI window')
  await waitFor(
    async () => await browserControlPage.locator('#app > *').count() > 0,
    'browser Control UI Vue mount',
  )
  await app.evaluate(({ BrowserWindow }, id) => {
    BrowserWindow.fromId(id)?.destroy()
  }, browserControlWindowId)

  const shutdownStatus = await desktopPage.evaluate(async () => {
    const response = await fetch('/api/system/shutdown', { method: 'POST' })
    return response.status
  })
  assert.equal(shutdownStatus, 202)
  await waitFor(async () => {
    const connection = await desktopPage.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    )
    return connection?.status === 'error' ? connection : null
  }, 'Gateway stop to become a Desktop capability error')
  assert.equal(desktopPage.url(), 'opensquilla-app://desktop/chat/new')
  assert.equal(await desktopPage.locator('#app').count(), 1)
  assert.equal(await desktopPage.locator('#desktop-runtime-banner').isVisible(), true)
  assert.equal(await desktopPage.locator('#desktop-runtime-retry').isVisible(), true)

  // Exercise the real main-process save transaction after an isolated Control
  // UI edit. Its config must win over Desktop's deliberately stale credential.
  const routerConfigPath = join(userDataDir, 'opensquilla', 'config.toml')
  const routerCredentialPath = join(userDataDir, 'desktop-credential.json')
  const operatorConfig = config
    .replace('preset_binding = "follow_primary"', 'preset_binding = "custom"')
    .replace('model = "qwen3.7-flash"', 'model = "operator-custom-c0"')
    + '\n[squilla_router.budget_gate]\naction = "cap"\nlimit_usd = 2.5\n'
  await writeFile(routerConfigPath, operatorConfig)
  const operatorRouter = desktopRouterConfigTomlLines(credential, operatorConfig, 'preserve')
  const saveDesktop = payload => desktopPage.evaluate(
    payload => window.opensquillaDesktop.saveDesktopSettings(payload), payload,
  )
  const rotated = await saveDesktop({ apiKey: 'synthetic-rotated-key' })
  assert.equal(rotated.routerPresetBinding, 'follow_primary', 'key edits preserve credential metadata')
  assert.deepEqual(desktopRouterConfigTomlLines(credential, await readFile(routerConfigPath, 'utf8'), 'preserve'), operatorRouter)
  const disabled = await saveDesktop({ routerMode: 'disabled' })
  assert.equal(disabled.routerPresetBinding, 'follow_primary')
  assert.equal(disabled.routerTiers.c3.ensembleEnabled, false)
  assert.deepEqual(desktopRouterConfigTomlLines(credential, await readFile(routerConfigPath, 'utf8'), 'preserve'),
    operatorRouter.map(line => line === 'enabled = true' ? 'enabled = false' : line))
  await saveDesktop({ routerMode: 'recommended' })
  assert.deepEqual(desktopRouterConfigTomlLines(credential, await readFile(routerConfigPath, 'utf8'), 'preserve'), operatorRouter)
  const historicalCredential = JSON.parse(await readFile(routerCredentialPath, 'utf8'))
  delete historicalCredential.routerPresetBinding
  await writeFile(routerCredentialPath, JSON.stringify(historicalCredential, null, 2))
  const historicalSnapshot = await saveDesktop({ searchProvider: 'duckduckgo', routerPresetBinding: 'follow_primary' })
  assert.equal(Object.hasOwn(historicalSnapshot, 'routerPresetBinding'), false)
  assert.equal(Object.hasOwn(JSON.parse(await readFile(routerCredentialPath, 'utf8')), 'routerPresetBinding'), false)
  assert.deepEqual(desktopRouterConfigTomlLines(credential, await readFile(routerConfigPath, 'utf8'), 'preserve'), operatorRouter)
  const edited = await saveDesktop({ routerTiers: {
    ...historicalSnapshot.routerTiers,
    c1: { ...historicalSnapshot.routerTiers.c1, thinkingLevel: 'high', extra: { temperature: 0.3 } },
  }, routerPresetBinding: 'follow_primary' })
  assert.equal(edited.routerPresetBinding, 'custom')
  assert.match(await readFile(routerConfigPath, 'utf8'), /thinking_level = "high"/)
  const reset = await saveDesktop({ routerResetToRecommended: true,
    routerTiers: { c1: { provider: 'tokenrhythm', model: 'untrusted-renderer-model' } } })
  assert.equal(reset.routerPresetBinding, 'follow_primary')
  assert.equal(reset.routerTiers.c1.model, 'deepseek-flash')
  assert.match(await readFile(routerConfigPath, 'utf8'), /preset_binding = "follow_primary"/)

  // Switching a generated Desktop profile must follow config.toml ownership,
  // update its primary fallback, and retain the saved custom Ensemble plan.
  const beforeSwitch = parse(await readFile(routerConfigPath, 'utf8'))
  beforeSwitch.squilla_router.default_tier = 'c2'
  beforeSwitch.squilla_router.rollout_phase = 'observe'
  beforeSwitch.squilla_router.budget_gate = { action: 'cap', limit_usd: 2.5 }
  beforeSwitch.llm_ensemble = { enabled: false, selection_mode: 'custom_b5',
    candidates: [{ provider: 'tokenrhythm', model: 'custom/a' },
      { provider: 'openrouter', model: 'custom/b' }],
    proposer_max_retries: 3,
  }
  await writeFile(routerConfigPath, stringify(beforeSwitch))
  const switched = await saveDesktop({ provider: 'openrouter', apiKey: 'synthetic-openrouter-key' })
  const afterSwitch = parse(await readFile(routerConfigPath, 'utf8'))
  assert.equal(switched.provider, 'openrouter')
  assert.equal(switched.model, switched.routerTiers.c2.model)
  assert.equal(switched.baseUrl, 'https://openrouter.ai/api/v1')
  assert.equal(afterSwitch.llm.model, switched.model)
  assert.equal(afterSwitch.squilla_router.preset_binding, 'follow_primary')
  assert.equal(afterSwitch.squilla_router.enabled, true)
  assert.equal(afterSwitch.squilla_router.rollout_phase, 'observe')
  assert.deepEqual(afterSwitch.squilla_router.budget_gate, beforeSwitch.squilla_router.budget_gate)
  assert.ok(Object.values(afterSwitch.squilla_router.tiers).every(tier => tier.provider === 'openrouter'))
  assert.deepEqual(afterSwitch.llm_ensemble, beforeSwitch.llm_ensemble)

  afterSwitch.squilla_router.preset_binding = 'custom'
  await writeFile(routerConfigPath, stringify(afterSwitch))
  const beforeRejectedConfig = await readFile(routerConfigPath, 'utf8')
  const beforeRejectedCredential = await readFile(routerCredentialPath, 'utf8')
  await assert.rejects(saveDesktop({ provider: 'tokenrhythm', apiKey: 'synthetic-tokenrhythm-key' }),
    /Saved Router tiers use another provider/)
  assert.equal(await readFile(routerConfigPath, 'utf8'), beforeRejectedConfig)
  assert.equal(await readFile(routerCredentialPath, 'utf8'), beforeRejectedCredential)

  console.log(JSON.stringify({
    ok: true,
    steps: 1,
    provider: credential.provider,
    modelRoutingMode: credential.modelRoutingMode,
    routerMode: credential.routerMode,
    model: credential.model,
    screenshotPath: screenshotPath || null,
  }, null, 2))
} finally {
  await app.close().catch(() => {})
  await successfulProbeServer.close().catch(() => {})
  await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
}
console.log('PASS onboarding provider-client')
