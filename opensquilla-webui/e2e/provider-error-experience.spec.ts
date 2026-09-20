import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const sessionKey = 'agent:main:webchat:synthetic-provider-error'
const turnId = 'synthetic-provider-turn'
const errorId = 'abcdef01'
const privateMessage = 'PRIVATE_PROVIDER_BODY HTTP 403/404 upstream traceback (ref: abcdef01)'
const partialAnswer = 'Partial answer'
type Locale = 'en' | 'zh-Hans'
type Delivery = 'live' | 'task-only' | 'history' | 'timeout-first' | 'timeout-last' | 'conflict'
type Outcome = Record<string, unknown>

// Literal expectations deliberately do not import the product's dictionaries:
// these are user-facing contracts for both supported test languages.
const copy = {
  en: {
    busy: 'The model service is busy. Please try again later.',
    unknown: 'The task did not finish. Please try again later.',
    timeout: 'The task timed out before it finished.',
    noProvider: 'No model is available.',
    modelSettings: 'Open model settings',
    partial: 'Partial results were preserved.',
    working: 'Working',
    waiting: 'Waiting for model',
    retryWait: 'Rate limited · retrying in 30s',
    retrying: 'Retrying 2/3',
    fallback: 'Switching to backup model',
  },
  'zh-Hans': {
    busy: '模型服务暂时繁忙，请稍后再试',
    unknown: '任务未完成，请稍后再试',
    timeout: '任务超时，未能完成',
    noProvider: '当前没有可用模型',
    modelSettings: '打开模型设置',
    partial: '已保留部分结果',
    working: '正在工作',
    waiting: '等待模型',
    retryWait: '受到限流 · 30 秒后重试',
    retrying: '正在重试 2/3',
    fallback: '正在切换备用模型',
  },
} satisfies Record<Locale, Record<string, string>>

function rateLimitOutcome(): Outcome {
  return {
    kind: 'failed', reason: '429', error_class: '429', failure_kind: 'rate_limited',
    error_id: errorId, retryable: true,
  }
}

async function prepareGateway(page: Page, options: {
  locale: Locale
  delivery?: Delivery
  outcome?: Outcome
  partial?: boolean
}) {
  const { locale, delivery = 'live', outcome = rateLimitOutcome(), partial = true } = options
  const timeout = delivery === 'timeout-first' || delivery === 'timeout-last'
  const runtimeErrors: string[] = []
  page.on('pageerror', error => runtimeErrors.push(error.message))
  await page.addInitScript(value => localStorage.setItem('opensquilla-locale', value), locale)
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt' } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public/opensquilla-mark.png', import.meta.url)),
  }))

  let durable = delivery === 'history'
  let succeeded = false
  let accepted = false
  let sends = 0
  let connections = 0
  let streamSeq = 0
  let emit: ((event: string, payload: Outcome) => void) | undefined
  let disconnect: (() => void) | undefined
  const historyOutcome = delivery === 'conflict' ? { ...outcome, error_id: 'abcdef02' } : outcome
  await page.routeWebSocket(/\/ws$/, ws => {
    connections++
    disconnect = () => ws.close()
    const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    emit = (event, payload) => ws.send(JSON.stringify({
      type: 'event', event, payload: {
        key: sessionKey, task_id: turnId, turn_id: turnId, stream_seq: ++streamSeq,
        stream_generation: 'e2e-stream-generation', ...payload,
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
          events: ['session.event.provider_activity', 'session.event.text_delta', 'session.event.error', 'session.event.done'],
        } }))
        return
      }
      if (frame.method === 'chat.history') {
        const messages: Outcome[] = durable ? [{
          role: 'user', text: 'Synthetic request', message_id: 'synthetic-user',
          timestamp: '2026-01-01T09:00:00Z', turn_context: { turn_id: turnId },
        }] : []
        if (durable && (partial || succeeded)) messages.push({
          role: 'assistant', text: succeeded ? 'Recovered answer' : partialAnswer,
          message_id: 'synthetic-assistant', timestamp: '2026-01-01T09:00:01Z',
          turn_context: { turn_id: turnId },
        })
        respond(frame.id, chatHistoryPayload(messages, {
          turn_outcomes: durable && !succeeded ? [{
            turn_id: turnId, task_id: turnId, status: timeout ? 'timeout' : 'failed',
            outcome: historyOutcome, terminal_message: privateMessage,
          }] : [],
        }))
        return
      }
      if (frame.method === 'chat.send') {
        sends++
        respond(frame.id, {
          sessionKey, status: 'accepted', accepted: true, task_id: turnId, turn_id: turnId,
          message_id: 'synthetic-user',
        })
        accepted = true
        emit!('task.running', { status: 'running', session_key: sessionKey })
        emit!('session.event.state_change', { to_state: 'thinking' })
        return
      }
      const metadata = accepted && !durable ? {
        run_status: 'running',
        active_task: { task_id: turnId, turn_id: turnId, session_key: sessionKey, status: 'running' },
      } : {}
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(sessionKey, metadata),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(sessionKey, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(sessionKey, metadata),
        'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      respond(frame.id, payloads[String(frame.method)] ?? {})
    })
  })

  await page.goto('/control/chat?session=' + encodeURIComponent(sessionKey))
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  return {
    runtimeErrors,
    sends: () => sends,
    async start() {
      await page.locator('.chat-textarea').fill('Synthetic request')
      await page.locator('.chat-textarea').press('Enter')
      await expect.poll(() => accepted).toBe(true)
    },
    activity(phase: string, reason = 'rate_limited') {
      emit!('session.event.provider_activity', {
        schema_version: 1, activity_id: `activity-${phase}`, phase, reason,
        started_at: Date.now(), heartbeat: false, retry_attempt: 2, retry_limit: 3,
        retry_after_ms: 30_000, provider_error_body: privateMessage,
      })
    },
    fail() {
      durable = true
      if (partial) emit!('session.event.text_delta', { text: partialAnswer })
      const failure = {
        code: outcome.error_class, message: privateMessage, terminal_message: privateMessage,
        turn_outcome: outcome, ...(outcome.error_id ? { error_id: outcome.error_id } : {}),
      }
      if (delivery === 'timeout-first') {
        emit!('task.timeout', { ...failure, terminal_reason: 'timeout' })
        emit!('session.event.error', failure)
      } else {
        if (delivery !== 'task-only') emit!('session.event.error', failure)
        emit!(timeout ? 'task.timeout' : 'task.failed', {
          ...failure, terminal_reason: timeout ? 'timeout' : 'error',
        })
      }
    },
    succeed() {
      durable = true
      succeeded = true
      emit!('session.event.text_delta', { text: 'Recovered answer' })
      emit!('session.event.done', { final_text: 'Recovered answer' })
      emit!('task.succeeded', { terminal_reason: 'completed' })
    },
    async reconnect() {
      const before = connections
      disconnect!()
      await expect.poll(() => connections).toBeGreaterThan(before)
      await expect(page.locator('.conn-pill.connected')).toBeVisible()
    },
  }
}

async function expectSafeCard(page: Page, message: string, actionCount = 0) {
  const card = page.locator('.msg-error')
  await expect(card).toHaveCount(1)
  await expect(card.locator('.msg-error__text')).toHaveText(message)
  await expect(card.locator('button, a')).toHaveCount(actionCount)
  await expect(card).not.toContainText(/abcdef0[12]|PRIVATE_PROVIDER_BODY|traceback|HTTP 403|HTTP 404|ref:|rate_limited/)
  await expect(card.locator('svg, code, strong, h1, h2, h3')).toHaveCount(0)
  await expect(card).toHaveCSS('border-top-width', '0px')
  await expect(card).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await expect(card).toHaveCSS('box-shadow', 'none')
  await expect(card).toHaveCSS('text-align', 'center')
  await expect(card.locator('time, .msg-error__time')).toHaveCount(0)
  await expect(page.locator('.turn-outcome--failed, .turn-outcome--timeout')).toHaveCount(0)
  // On desktop the reason, optional preserved-results note and action form one
  // centered line. Narrow screens may wrap instead of clipping useful copy.
  if (page.viewportSize()!.width >= 1000) {
    await expect.poll(() => card.evaluate(element => {
      const range = document.createRange()
      range.selectNodeContents(element)
      const rects = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0)
      const bounds = element.getBoundingClientRect()
      const lineCenters = rects.map(rect => rect.y + rect.height / 2)
      const contentCenter = (Math.min(...rects.map(rect => rect.left)) + Math.max(...rects.map(rect => rect.right))) / 2
      return {
        rendered: rects.length > 0,
        singleLine: Math.max(...lineCenters) - Math.min(...lineCenters) <= 3,
        centered: Math.abs(contentCenter - (bounds.x + bounds.width / 2)) <= 2,
      }
    })).toEqual({ rendered: true, singleLine: true, centered: true })
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
}

async function capturePresentation(page: Page, name: string) {
  const path = test.info().outputPath(`${name}.png`)
  await page.screenshot({ path, fullPage: true })
  await test.info().attach(name, { path, contentType: 'image/png' })
}

for (const locale of ['en', 'zh-Hans'] as const) {
  for (const { delivery, width } of [
    { delivery: 'live', width: 1280 }, { delivery: 'task-only', width: 1280 },
    { delivery: 'history', width: 1280 }, { delivery: 'history', width: 390 },
    { delivery: 'timeout-first', width: 1280 }, { delivery: 'timeout-last', width: 1280 },
    { delivery: 'conflict', width: 1280 },
  ] as const) {
    test(`${locale} terminal error stays concise through ${delivery}, reconnect and reload at ${width}px`, async ({ page }) => {
      const timeout = delivery === 'timeout-first' || delivery === 'timeout-last'
      const outcome = timeout ? {
        ...rateLimitOutcome(), kind: 'interrupted', reason: 'timeout', error_class: 'llm_timeout',
        failure_kind: 'transport_transient',
      } : rateLimitOutcome()
      await page.setViewportSize({ width, height: 900 })
      const gateway = await prepareGateway(page, { locale, delivery, outcome })
      if (delivery !== 'history') { await gateway.start(); gateway.fail() }
      const message = timeout ? copy[locale].timeout : copy[locale].busy
      await expectSafeCard(page, message)
      await expect(page.locator('.msg-ai')).toContainText(partialAnswer)
      if (delivery === 'history' && width === 390) await capturePresentation(page, `${locale}-partial-mobile`)
      if (delivery !== 'history') {
        await gateway.reconnect()
        await expectSafeCard(page, message)
      }
      await page.reload()
      await expectSafeCard(page, message)
      await expect(page.locator('.msg-error__note')).toHaveText(copy[locale].partial)
      await expect(page.locator('.msg-ai')).toContainText(partialAnswer)
      expect(gateway.sends()).toBe(delivery === 'history' ? 0 : 1)
      expect(gateway.runtimeErrors).toEqual([])
    })
  }

  for (const status of ['403', '404']) {
    test(`${locale} bare ${status} stays unknown without an action or raw response`, async ({ page }) => {
      const gateway = await prepareGateway(page, {
        locale, partial: false,
        outcome: { kind: 'failed', reason: status, error_class: status, failure_kind: 'unknown', error_id: errorId, retryable: true },
      })
      await gateway.start()
      gateway.fail()
      await expectSafeCard(page, copy[locale].unknown)
      await expect(page.locator('.msg-error__note')).toHaveCount(0)
      if (status === '403') await capturePresentation(page, `${locale}-unknown-desktop`)
      await page.reload()
      await expectSafeCard(page, copy[locale].unknown)
      expect(gateway.sends()).toBe(1)
      expect(gateway.runtimeErrors).toEqual([])
    })
  }

  for (const { hasDiagnostic, width } of [
    { hasDiagnostic: true, width: 1280 }, { hasDiagnostic: false, width: 1280 },
    { hasDiagnostic: false, width: 390 },
  ]) {
    test(`${locale} no_provider has the same safe settings action with diagnostic=${hasDiagnostic} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      const gateway = await prepareGateway(page, {
        locale, partial: false,
        outcome: { kind: 'failed', reason: 'no_provider', error_class: 'no_provider', ...(hasDiagnostic ? { error_id: errorId } : {}) },
      })
      await gateway.start()
      gateway.fail()
      await expectSafeCard(page, copy[locale].noProvider, 1)
      await expect(page.locator('.msg-error').getByRole('link', { name: copy[locale].modelSettings })).toHaveAttribute('href', '/control/settings/modelStrategy')
      if (!hasDiagnostic) await capturePresentation(page, `${locale}-settings-${width}px`)
      await page.reload()
      await expectSafeCard(page, copy[locale].noProvider, 1)
      expect(gateway.sends()).toBe(1)
      expect(gateway.runtimeErrors).toEqual([])
    })
  }

  for (const recovered of [true, false]) {
    test(`${locale} automatic recovery shows activity and ends with ${recovered ? 'success without failure' : 'one terminal failure'}`, async ({ page }) => {
      const gateway = await prepareGateway(page, { locale, partial: false })
      await gateway.start()
      const label = page.locator('.assistant-activity--live .assistant-activity__live-label')
      gateway.activity('requesting', 'initial')
      // The disclosure header intentionally stays at the current lifecycle;
      // recovery details belong to its expandable, localized activity rows.
      await expect(label).toHaveText(copy[locale].working)
      await expect(page.locator('.msg-error')).toHaveCount(0)
      const disclosure = page.locator('.assistant-activity--live .assistant-activity__live-head')
      if (await disclosure.getAttribute('aria-expanded') !== 'true') await disclosure.click()
      await expect(page.locator('.assistant-activity--live .assistant-activity-status__row').filter({ hasText: copy[locale].waiting })).toBeVisible()
      for (const [phase, expected] of [
        ['retry_wait', copy[locale].retryWait],
        ['retrying', copy[locale].retrying], ['fallback', copy[locale].fallback],
      ]) {
        gateway.activity(phase!)
        await expect(page.locator('.assistant-activity--live .assistant-activity-status__row').filter({ hasText: expected! })).toBeVisible()
        await expect(page.locator('.msg-error')).toHaveCount(0)
        await expect(page.locator('body')).not.toContainText('PRIVATE_PROVIDER_BODY')
      }
      if (recovered) {
        gateway.succeed()
        await expect(page.locator('.msg-ai')).toContainText('Recovered answer')
        await expect(page.locator('.msg-error')).toHaveCount(0)
      } else {
        gateway.fail()
        await expectSafeCard(page, copy[locale].busy)
      }
      await page.reload()
      if (recovered) {
        await expect(page.locator('.msg-ai')).toContainText('Recovered answer')
        await expect(page.locator('.msg-error')).toHaveCount(0)
      } else {
        await expectSafeCard(page, copy[locale].busy)
      }
      expect(gateway.sends()).toBe(1)
      expect(gateway.runtimeErrors).toEqual([])
    })
  }
}
