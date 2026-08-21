import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const screenshotPath = String(process.env.OPENSQUILLA_DESKTOP_ONBOARDING_SCREENSHOT || '').trim()
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

async function setupWindow(app) {
  return await waitFor(async () => {
    for (const page of app.windows()) {
      if (page.isClosed()) continue
      await page.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (await page.locator('#setup-form').count().catch(() => 0)) return page
    }
    return null
  }, 'desktop onboarding window')
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

function boxesOverlap(left, right) {
  return left.x < right.x + right.width
    && left.x + left.width > right.x
    && left.y < right.y + right.height
    && left.y + left.height > right.y
}

async function assertSubmitActionsDoNotOverlap(page) {
  const boxes = {
    cancel: await page.locator('#cancel').boundingBox(),
    submitStatus: await page.locator('#submitStatus').boundingBox(),
    finish: await page.locator('#finish').boundingBox(),
  }
  for (const [name, box] of Object.entries(boxes)) {
    assert.ok(box, `${name} must have a visible bounding box`)
  }
  for (const [leftName, rightName] of [
    ['cancel', 'submitStatus'],
    ['cancel', 'finish'],
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
  assert.equal(await phase.getAttribute('role'), 'status')
  assert.equal(await phase.getAttribute('aria-live'), 'polite')
  assert.equal(await phase.getAttribute('aria-atomic'), 'true')
  assert.equal(await timer.getAttribute('aria-hidden'), 'true')
  assert.equal(await page.locator('section.status').getAttribute('aria-live'), null)

  const staleStatus = {
    phaseId: 'gateway-start',
    label: 'Synthetic gateway start',
    at: new Date(Date.now() - 3_000).toISOString(),
  }
  await sendBootEvent(app, 'desktop:boot:status', staleStatus)
  const anchoredElapsed = await waitFor(async () => {
    const value = await bootElapsedSeconds(page)
    return value >= 2 ? value : null
  }, 'boot timer to include elapsed phase age')

  const activeStatus = {
    phaseId: 'gateway-health',
    label: 'Synthetic gateway health',
    at: new Date().toISOString(),
  }
  await sendBootEvent(app, 'desktop:boot:status', activeStatus)
  const resetElapsed = await waitFor(async () => {
    const value = await bootElapsedSeconds(page)
    return value < anchoredElapsed - 1 ? value : null
  }, 'boot timer to reset for a new phase identity')

  await delay(350)
  const beforeReplay = await bootElapsedSeconds(page)
  await sendBootEvent(app, 'desktop:boot:status', activeStatus)
  await delay(350)
  const afterReplay = await bootElapsedSeconds(page)
  assert.ok(
    afterReplay > beforeReplay && afterReplay > resetElapsed,
    'replaying one BootStatus identity must not reset its elapsed timer',
  )

  const repeatedPhaseWithNewTimestamp = { ...activeStatus, at: new Date().toISOString() }
  await sendBootEvent(app, 'desktop:boot:status', repeatedPhaseWithNewTimestamp)
  const repeatedPhaseReset = await waitFor(async () => {
    const value = await bootElapsedSeconds(page)
    return value < afterReplay ? value : null
  }, 'boot timer to reset for a repeated phase with a new timestamp')
  assert.ok(Number.isFinite(repeatedPhaseReset) && repeatedPhaseReset >= 0)

  const invalidTimestampLabel = 'Synthetic invalid timestamp'
  await sendBootEvent(app, 'desktop:boot:status', {
    phaseId: 'gateway-start',
    label: invalidTimestampLabel,
    at: 'not-a-date',
  })
  const invalidTimestampValue = await waitFor(async () => {
    if ((await phase.innerText()).trim() !== invalidTimestampLabel) return null
    const value = await bootElapsedSeconds(page)
    return Number.isFinite(value) && value >= 0 && value < 2 ? { value } : null
  }, 'invalid boot timestamp to clamp near zero')
  assert.ok(invalidTimestampValue.value >= 0 && invalidTimestampValue.value < 2)

  const futureTimestampLabel = 'Synthetic future timestamp'
  await sendBootEvent(app, 'desktop:boot:status', {
    phaseId: 'control',
    label: futureTimestampLabel,
    at: new Date(Date.now() + 60_000).toISOString(),
  })
  const futureTimestampValue = await waitFor(async () => {
    if ((await phase.innerText()).trim() !== futureTimestampLabel) return null
    const value = await bootElapsedSeconds(page)
    return Number.isFinite(value) && value >= 0 && value < 2 ? { value } : null
  }, 'future boot timestamp to clamp near zero')
  assert.ok(futureTimestampValue.value >= 0 && futureTimestampValue.value < 2)

  await sendBootEvent(app, 'desktop:boot:error', { message: 'Synthetic boot pause.' })
  await delay(150)
  const frozenText = await timer.innerText()
  await delay(350)
  assert.equal(await timer.innerText(), frozenText, 'boot errors must freeze the elapsed timer')

  await sendBootEvent(app, 'desktop:boot:status', {
    phaseId: 'profile',
    label: 'Synthetic retry',
    at: new Date().toISOString(),
  })
  await delay(350)
  assert.notEqual(await timer.innerText(), frozenText, 'a new retry status must resume phase timing')
}

async function launchIsolatedOnboarding(prefix, gatewayPort) {
  const userDataRoot = await mkdtemp(join(tmpdir(), prefix))
  const userDataDir = join(userDataRoot, 'chromium-user-data')
  const isolatedHome = join(userDataRoot, 'home')
  await mkdir(isolatedHome, { recursive: true })
  const app = await electron.launch({
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
      OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(gatewayPort),
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION: '',
      LANG: 'en_US.UTF-8',
      LC_ALL: 'en_US.UTF-8',
    },
  })
  return { app, userDataDir, userDataRoot }
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
  const cancel = page.locator('#cancel')
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
  assert.equal(await cancel.isDisabled(), false)
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
  expectedFinishLabel = 'Start OpenSquilla',
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
    18896,
  )
  try {
    const page = await setupWindow(app)
    await verifyBootPhaseTimer(app)
    const submitClockOrigin = Date.now()
    await page.clock.install({ time: submitClockOrigin })
    await page.clock.pauseAt(submitClockOrigin + 1_000)
    const apiKey = page.locator('#apiKey')
    await apiKey.fill('synthetic-submit-key')
    await page.locator('#onboardingLocale').selectOption('de')
    await installPendingSaveStub(app)

    await page.locator('#providerSelectToggle').click()
    assert.equal(await page.locator('#providerSelectToggle').getAttribute('aria-expanded'), 'true')
    assert.equal(await page.locator('#providerSelectPanel').isVisible(), true)
    await page.locator('#finish').click()
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
      'Die Ersteinrichtung dauert normalerweise 10–20 Sekunden. Lassen Sie dieses Fenster geöffnet.',
    )
    assert.equal(await page.locator('#submitStatus').isVisible(), true)
    assert.equal(await page.locator('#finish').isDisabled(), true)
    assert.equal((await page.locator('#finish').innerText()).trim(), 'Einrichtung wird gespeichert…')
    await assertSubmitActionsDoNotOverlap(page)
    const firstState = await pendingSaveState(app)
    assert.equal(firstState.lastPayload?.apiKey, 'synthetic-submit-key')

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
      'Synthetic onboarding save was refused.',
      'synthetic-submit-key',
      'OpenSquilla starten',
    )
    await page.clock.fastForward(8_000)
    assert.equal(
      (await page.locator('#submitStatus').innerText()).trim(),
      '',
      'a failed save must cancel stale slow-feedback timers',
    )
    await page.locator('#onboardingLocale').selectOption('en')

    await page.locator('#finish').click()
    await assertSubmitPending(page, app, 2)
    await settlePendingSave(app, {
      reject: true,
      error: 'Synthetic onboarding save rejected.',
    })
    await assertSubmitRestored(page, 'Synthetic onboarding save rejected.', 'synthetic-submit-key')

    await page.locator('#finish').click()
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
    const windows = await Promise.all(app.windows().map(async (page) => ({
      closed: page.isClosed(),
      title: await page.title().catch(() => ''),
      url: page.url(),
    })))
    const desktopLog = await readFile(join(userDataDir, 'logs', 'desktop.log'), 'utf8')
      .catch(() => '<desktop log unavailable>')
    throw new Error(
      `${error?.message || error}\nWindows: ${JSON.stringify(windows)}\nDesktop log:\n${desktopLog}`,
      { cause: error },
    )
  } finally {
    await app.close().catch(() => {})
    await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
  }
}

await verifySubmitFeedbackAndSingleFlight()

const { app, userDataDir, userDataRoot } = await launchIsolatedOnboarding(
  'opensquilla-electron-onboarding-test-',
  18897,
)

try {
  const page = await setupWindow(app)
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
  assert.equal(await providerScreen.locator('.card-head > p').innerText(), '输入 API 密钥即可开始使用')
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
  assert.equal(await page.locator('#finish').innerText(), '启动 OpenSquilla')
  assert.equal(await page.locator('.next-button, .back-button').count(), 0, 'single-page onboarding must not render next or back actions')
  assert.equal(await providerScreen.locator('.provider-feature, .provider-disclosure').count(), 0, 'provider setup should use one unified select')
  assert.equal(await providerScreen.locator('.provider-promo').count(), 0, 'the promotion should not occupy a separate row')
  assert.equal(await providerScreen.locator('.provider-promo-token').count(), 0)
  assert.equal(await providerScreen.locator('.provider-promo-copy').isVisible(), true)
  assert.equal(await providerScreen.locator('.provider-promo-copy strong').innerText(), 'TokenRhythm 限时福利')
  assert.equal(await providerScreen.locator('.provider-promo-copy span').innerText(), '注册即领价值 68 元 Token')
  const promoTitleBox = await page.locator('.provider-promo-copy strong').boundingBox()
  const promoCopyBox = await page.locator('.provider-promo-copy span').boundingBox()
  assert.ok(
    promoTitleBox && promoCopyBox
      && Math.abs(
        (promoTitleBox.y + promoTitleBox.height / 2)
        - (promoCopyBox.y + promoCopyBox.height / 2),
    ) <= 2,
    'the limited-time promotion copy should render on one line',
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
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-v4-pro-0813')
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
    'provider verification controls should not be exposed in onboarding',
  )
  const apiKeyLabelBox = await page.locator('.api-key-label').boundingBox()
  const providerLabelBox = await page.locator('#providerSelectLabel').boundingBox()
  const claimButtonBox = await page.locator('#tokenrhythmRegister').boundingBox()
  const initialApiKeyBox = await page.locator('#apiKey').boundingBox()
  assert.ok(
    apiKeyLabelBox && providerLabelBox
      && Math.abs(apiKeyLabelBox.x - providerLabelBox.x) <= 1,
    'the API-key heading should align with the inset provider label',
  )
  assert.ok(
    apiKeyLabelBox && promoTitleBox && promoCopyBox
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (promoTitleBox.y + promoTitleBox.height / 2),
      ) <= 3
      && Math.abs(
        (apiKeyLabelBox.y + apiKeyLabelBox.height / 2)
        - (promoCopyBox.y + promoCopyBox.height / 2),
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
  assert.equal(await page.locator('#model').inputValue(), 'deepseek-v4-pro-0813')
  assert.equal(await page.locator('#modelRoutingMode').inputValue(), 'squilla_router')
  assert.equal(await page.locator('#routerMode').inputValue(), 'recommended')

  const tokenRhythmCta = page.locator('#tokenrhythmRegister')
  assert.equal(await tokenRhythmCta.innerText(), '免费领取')
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

  await chooseProvider('minimax_cn', 'MiniMax Mainland')
  assert.equal(await page.locator('#provider').inputValue(), 'minimax_cn')
  assert.equal(await tokenRhythmCta.isVisible(), true, 'the promotion should remain available when another provider is selected')
  assert.equal(await page.locator('#providerSelectedBadges .provider-badge').count(), 0)
  assert.equal(await page.locator('#model').inputValue(), 'MiniMax-M2.7')
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'MiniMax-M2.7')
  assert.equal(await page.locator('#modelSummary').isVisible(), true)
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
  assert.equal(await page.locator('#modelSummaryValue').innerText(), 'deepseek-v4-pro-0813')
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
  assert.equal(credential.routerDefaultTier, 'c1')
  assert.equal(credential.model, 'deepseek-v4-pro-0813')
  assert.equal(credential.routerTiers.c0.model, 'deepseek-v4-flash-0731')
  assert.equal(credential.routerTiers.c1.model, 'deepseek-v4-pro-0813')
  assert.equal(credential.routerTiers.c2.model, 'kimi-k2.7-code')
  assert.equal(credential.routerTiers.c3.model, 'glm-5.2')
  assert.equal(credential.routerTiers.c0.supportsImage, false)
  assert.equal(credential.routerTiers.c1.supportsImage, false)
  assert.equal(credential.routerTiers.c2.supportsImage, false)
  assert.equal(credential.routerTiers.c3.supportsImage, false)
  assert.equal(credential.routerTiers.c3.ensembleEnabled, true)
  assert.equal(credential.routerTiers.image_model.model, 'kimi-k2.6')
  assert.equal(credential.routerTiers.image_model.supportsImage, true)
  assert.match(config, /\[squilla_router\]\nenabled = true/)
  assert.match(config, /\[llm\][\s\S]*?model = "deepseek-v4-pro-0813"/)
  assert.match(config, /\[squilla_router\.tiers\.c0\]\nprovider = "tokenrhythm"\nmodel = "deepseek-v4-flash-0731"/)
  assert.match(config, /\[squilla_router\.tiers\.c1\]\nprovider = "tokenrhythm"\nmodel = "deepseek-v4-pro-0813"/)
  assert.match(config, /\[squilla_router\.tiers\.c2\]\nprovider = "tokenrhythm"\nmodel = "kimi-k2.7-code"/)
  assert.match(config, /\[squilla_router\.tiers\.c3\][\s\S]*?model = "glm-5.2"[\s\S]*?ensemble_enabled = true/)
  assert.doesNotMatch(config, /thinking_level\s*=/)
  assert.match(config, /\[llm_ensemble\]\nenabled = false/)

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
  await rm(userDataRoot, { recursive: true, force: true }).catch(() => {})
}
