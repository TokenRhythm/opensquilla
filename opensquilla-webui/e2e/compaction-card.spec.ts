import { test, expect, type Page, type TestInfo } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const SESSION = 'agent:main:webchat:e2e-compaction-presentation'
const DIAGNOSTIC_ID = 'fixture-diagnostic-42'
type HistoryScenario = 'durable' | 'request_scoped' | 'automatic_recovered' | 'manual_failed' | 'terminal_failed' | 'capacity_failed'

// Only the transport is mocked; history decoding, replay and rendering use the
// production application. No provider call or user profile is involved.
async function installHistory(page: Page, scenario?: HistoryScenario, locale = 'en') {
  await page.addInitScript(value => {
    localStorage.setItem('opensquilla-locale', value)
    localStorage.setItem('opensquilla-theme', 'dark')
  }, locale)
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  for (const endpoint of ['approvals', 'system/update', 'elevated-mode']) {
    await page.route('**/api/' + endpoint, route => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ pending: [], enabled: false, mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
    }))
  }
  const terminal = scenario === 'terminal_failed' || scenario === 'capacity_failed'
  const failedAttempt = scenario === 'automatic_recovered' || terminal
  const messages = scenario && scenario !== 'manual_failed' ? [
    { id: 'user-1', role: 'user', text: locale === 'zh-Hans' ? '继续检查回滚记录。' : 'Continue the existing task.', timestamp: 1_800_000_000, turn_context: { turn_id: 'turn-1' } },
    {
      id: 'assistant-1', role: 'assistant',
      text: terminal ? '' : locale === 'zh-Hans' ? '已继续检查回滚记录，下一步核对部署结果。' : 'The task continued.',
      timestamp: 1_800_000_002,
      turn_context: {
        turn_id: 'turn-1',
        activity_markers: scenario === 'request_scoped' || failedAttempt ? [{
          kind: 'context_compaction', id: 'cmp-temporary', status: failedAttempt ? 'failed' : 'completed',
          source: 'automatic', durability: failedAttempt ? 'none' : 'request_scoped', at: 1_800_000_001_000,
        }] : [],
      },
    },
  ] : []
  const summaries = scenario === 'durable' ? [{
    id: 'summary-1', compaction_id: 'cmp-saved', compaction_index: 1,
    summary_text: 'Synthetic checkpoint body.', removed_count: 2, kept_count: 1,
    covered_through_id: 2, created_at: 1_800_000_001_000,
  }] : []
  const terminalCode = scenario === 'capacity_failed' ? 'provider_request_too_large' : 'compaction_exhausted'
  const outcomes = terminal ? [{
    task_id: 'turn-1', turn_id: 'turn-1', status: 'failed',
    outcome: { kind: 'failed', reason: terminalCode, error_class: terminalCode, failure_kind: 'unknown', error_id: DIAGNOSTIC_ID },
    terminal_message: 'PRIVATE_PROVIDER_BODY (ref: ' + DIAGNOSTIC_ID + ')',
  }] : []
  let emitCompaction: ((status: string, sequence: number) => void) | undefined
  let subscribed = false
  await page.routeWebSocket(/\/ws$/, ws => {
    emitCompaction = (status, sequence) => ws.send(JSON.stringify({
      type: 'event', event: 'session.event.compaction', payload: {
        key: SESSION, source: 'manual', compaction_id: DIAGNOSTIC_ID, status,
        sequence, stream_seq: sequence, stream_generation: 'e2e-stream-generation',
        reason: status === 'failed' ? 'summary_failed' : undefined,
      },
    }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: {
          methods: ['sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot'],
          events: ['session.event.compaction'],
        } }))
        return
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload(messages, { compaction_summaries: summaries, turn_outcomes: outcomes }),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.resolve': { session_key: SESSION, session_id: SESSION },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'models.routing.get': { mode: 'direct' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
      if (frame.method === 'sessions.messages.subscribe') subscribed = true
    })
  })
  return {
    subscribed: () => subscribed,
    startManualCompaction: () => emitCompaction!('started', 1),
    failManualCompaction: () => emitCompaction!('failed', 2),
  }
}

async function revealCompactionActivity(page: Page) {
  const activity = page.locator('.assistant-activity').filter({ has: page.getByTestId('compaction-event') })
  const disclosure = activity.locator('.assistant-activity__summary')
  await expect(disclosure).toBeVisible()
  if (await disclosure.getAttribute('aria-expanded') === 'false') await disclosure.click()
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true')
  await expect(activity.locator('.assistant-activity__body')).toHaveAttribute('aria-hidden', 'false')
  await expect(activity.getByTestId('compaction-event')).toBeVisible()
  await expect(activity.getByTestId('compaction-event')).toBeInViewport({ ratio: 1 })
}

async function capturePresentation(page: Page, testInfo: TestInfo, name: string, activity = false) {
  if (activity) await revealCompactionActivity(page)
  await expect(page.getByTestId('compaction-event')).toBeVisible()
  const path = testInfo.outputPath(name + '.png')
  await page.screenshot({ path, fullPage: true })
  await testInfo.attach('Controlled UI transport fixture: ' + name, { path, contentType: 'image/png' })
}

test.describe('Compaction transcript presentation', () => {
  test('idle chat renders no maintenance event', async ({ page }) => {
    await installHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
    await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
    await expect(page.getByTestId('compaction-event')).toHaveCount(0)
  })

  for (const durability of ['durable', 'request_scoped'] as const) {
    test(durability + ' history keeps its meaning after reload', async ({ page }, testInfo) => {
      await installHistory(page, durability)
      await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
      for (let round = 0; round < 2; round += 1) {
        if (round) await page.reload()
        await expect(page.locator('.msg-ai')).toContainText('The task continued.')
        if (durability === 'request_scoped') await revealCompactionActivity(page)
        const event = page.getByTestId('compaction-event')
        await expect(event).toHaveCount(1)
        await expect(event).toBeVisible()
        await expect(event).toHaveAttribute('data-status', 'completed')
        await expect(event).toHaveAttribute('data-durability', durability)
        await expect(event).toContainText('Context organized')
        await expect(event).not.toHaveClass(/--failed|--running/)
        await expect(event.locator('time, .assistant-activity-status__duration')).toHaveCount(0)
        if (durability === 'request_scoped') {
          const marker = event.getByRole('button', { name: 'History temporarily reduced; continuing' })
          await expect(marker).toBeVisible()
          await expect(event.locator('.assistant-activity-status__content, .chat-compaction-event__title')).toContainText('Context organized')
          await expect(event).not.toContainText('History temporarily reduced; continuing')
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await marker.click()
          await expect(page.getByRole('tooltip')).toBeVisible()
          await expect(page.getByRole('tooltip')).toBeInViewport({ ratio: 1 })
          await expect(page.locator('body > .compaction-recovery-marker__hint')).toHaveCount(1)
          await capturePresentation(page, testInfo, 'recovery-marker-hint', true)
          await marker.press('Escape')
          await expect(marker).toBeFocused()
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await marker.click()
          await expect(page.getByRole('tooltip')).toBeVisible()
          await marker.click()
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await page.locator('.chat-textarea').click()
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await marker.hover()
          await expect(page.getByRole('tooltip')).toBeVisible()
          await page.locator('.chat-textarea').hover()
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await marker.focus()
          await expect(page.getByRole('tooltip')).toBeVisible()
          await marker.press('Escape')
          await expect(page.getByRole('tooltip')).not.toBeVisible()
          await page.locator('.chat-textarea').click()
          await expect(event).toContainText('Context organized')
          await expect(event).not.toContainText('session history was not rewritten')
        }
        await expect(page.locator('.chat-textarea')).toBeEditable()
      }
      await capturePresentation(page, testInfo, durability, durability === 'request_scoped')
    })
  }

  for (const durability of ['durable', 'request_scoped'] as const) {
    test('Chinese ' + durability + ' presentation retains the same quiet completion marker', async ({ page }, testInfo) => {
      await installHistory(page, durability, 'zh-Hans')
      await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
      await expect(page.locator('.msg-ai')).toContainText('已继续检查回滚记录，下一步核对部署结果。')
      if (durability === 'request_scoped') await revealCompactionActivity(page)
      const event = page.getByTestId('compaction-event')
      await expect(event).toHaveCount(1)
      await expect(event).toContainText('上下文已整理')
      if (durability === 'request_scoped') {
        await expect(event).not.toContainText('已临时精简历史，继续处理')
        await expect(page.getByRole('tooltip')).not.toBeVisible()
        await expect(event.locator('.compaction-recovery-marker__ring')).toBeVisible()
      }
      await expect(event).not.toHaveClass(/--failed|--running/)
      await expect(event.locator('time, .assistant-activity-status__duration')).toHaveCount(0)
      await capturePresentation(page, testInfo, 'zh-' + durability, durability === 'request_scoped')
    })
  }

  test('temporary marker remains usable on a narrow viewport', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installHistory(page, 'request_scoped', 'zh-Hans')
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
    await revealCompactionActivity(page)
    const event = page.getByTestId('compaction-event')
    const marker = event.getByRole('button', { name: '已临时精简历史，继续处理' })
    await expect(marker).toBeVisible()
    await expect(event).toContainText('上下文已整理')
    await expect(page.getByRole('tooltip')).not.toBeVisible()
    await marker.click()
    await expect(page.getByRole('tooltip')).toBeVisible()
    await expect(page.getByRole('tooltip')).toBeInViewport({ ratio: 1 })
    await expect(page.locator('body > .compaction-recovery-marker__hint')).toHaveCount(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await capturePresentation(page, testInfo, 'mobile-recovery-marker', true)
  })

  test('a failed automatic attempt is informational when the task continued, including after reload', async ({ page }, testInfo) => {
    await installHistory(page, 'automatic_recovered', 'zh-Hans')
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
    for (let round = 0; round < 2; round += 1) {
      if (round) await page.reload()
      await expect(page.locator('.msg-ai')).toContainText('已继续检查回滚记录，下一步核对部署结果。')
      await revealCompactionActivity(page)
      const event = page.getByTestId('compaction-event')
      await expect(event).toHaveCount(1)
      await expect(event).toContainText('本次上下文整理未完成')
      await expect(event).not.toHaveClass(/--failed|--running/)
      await expect(event).not.toHaveAttribute('role', 'alert')
      await expect(event.locator('time, .assistant-activity-status__duration')).toHaveCount(0)
      await expect(page.locator('.msg-error')).toHaveCount(0)
      await expect(page.locator('.chat-textarea')).toBeEditable()
    }
    await capturePresentation(page, testInfo, 'automatic-attempt-recovered', true)
  })

  test('manual summary failure shows useful copy without a diagnostic ID or relative time', async ({ page }, testInfo) => {
    const gateway = await installHistory(page, 'manual_failed', 'zh-Hans')
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
    await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
    await expect.poll(gateway.subscribed).toBe(true)
    gateway.startManualCompaction()
    const event = page.getByTestId('compaction-event')
    await expect(event).toContainText('正在整理上下文')
    await expect(event).toHaveClass(/--running/)
    await expect(event.locator('time')).toHaveCount(0)
    await capturePresentation(page, testInfo, 'manual-compaction-running')
    gateway.failManualCompaction()
    await expect(event).toHaveClass(/--failed/)
    await expect(event).toHaveAttribute('role', 'alert')
    await expect(event).toContainText('模型未能生成可用的摘要')
    await expect(event).toContainText('可稍后重新整理上下文，或更换模型。')
    await expect(event).not.toContainText(DIAGNOSTIC_ID)
    await expect(event).not.toContainText('诊断')
    await expect(event.locator('time, button')).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toBeEditable()
    await capturePresentation(page, testInfo, 'manual-compaction-failed')
  })

  for (const scenario of ['terminal_failed', 'capacity_failed'] as const) {
    test(scenario + ' keeps the final error visible next to an automatic attempt after reload', async ({ page }, testInfo) => {
      await installHistory(page, scenario, 'zh-Hans')
      await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION))
      for (let round = 0; round < 2; round += 1) {
        if (round) await page.reload()
        const error = page.locator('.msg-error')
        await expect(error).toHaveCount(1)
        await expect(error).toBeVisible()
        await revealCompactionActivity(page)
        await expect(page.getByTestId('compaction-event')).toContainText('本次上下文整理未完成')
        await expect(error).toContainText(scenario === 'capacity_failed'
          ? '内容超出当前模型容量，请缩短输入或选择更大上下文的模型'
          : '上下文整理未完成，本次无法继续，请稍后重试')
        await expect(error).not.toContainText(DIAGNOSTIC_ID)
        await expect(error).not.toContainText('PRIVATE_PROVIDER_BODY')
        await expect(error).not.toContainText('诊断')
        await expect(error.locator('time')).toHaveCount(0)
        if (scenario === 'capacity_failed') {
          const action = error.getByRole('link', { name: '打开模型设置' })
          await expect(action).toBeVisible()
          await expect(action).toHaveAttribute('href', /\/settings\/modelStrategy/)
        }
        await expect(page.locator('.chat-textarea')).toBeEditable()
      }
      await capturePresentation(page, testInfo, scenario, true)
    })
  }

  test('live /compact settles a transcript event and restores the composer', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(300000)
    await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
    await page.goto(CONTROL_URL + 'chat/new')
    await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Reply with the single word: ok')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect(page.locator('.msg-ai').first()).toBeVisible({ timeout: 120000 })
    await expect(page.locator('.work-card')).toHaveCount(0, { timeout: 120000 })

    await textarea.fill('/compact')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    const event = page.getByTestId('compaction-event').last()
    await expect(event).toBeVisible({ timeout: 10000 })
    await expect(event).toHaveAttribute('data-status', /^(completed|skipped)$/, { timeout: 120000 })
    await expect(event).not.toHaveClass(/--running|--failed/)
    // A successful no-op is not a failed summary. A durable completion uses
    // the saved-context label; neither result invents a percentage gauge.
    await expect(event).toContainText(/Context organized|Context within budget|already concise|no history that can be safely organized/)
    await expect(event.locator('[role="progressbar"]')).toHaveCount(0)
    await expect(textarea).toBeEditable()
  })
})
