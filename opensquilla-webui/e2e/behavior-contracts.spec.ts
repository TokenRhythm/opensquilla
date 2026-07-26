import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2ebehaviorcontracts'

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type EventSender = (event: string, payload: Record<string, unknown>) => void

type MockGatewayOptions = {
  abortCalls?: Array<Record<string, unknown>>
  afterSubscribe?: (sendEvent: EventSender) => void
  history?: () => Array<Record<string, unknown>>
  historyCalls?: { value: number }
  pendingApprovals?: Array<Record<string, unknown>>
  runStatus?: 'idle' | 'running' | 'approval_pending'
  sandboxEnsure?: () => Record<string, unknown>
  sandboxEnsureCalls?: { value: number }
  sandboxStatus?: () => Record<string, unknown>
  sandboxStatusCalls?: { value: number }
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

async function installMockGateway(page: Page, options: MockGatewayOptions = {}) {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      pending: options.pendingApprovals || [],
      mode: 'prompt',
      allowPatterns: [],
      denyPatterns: [],
    }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    let subscribeCallbackSent = false
    const sendEvent: EventSender = (event, payload) => {
      ws.send(JSON.stringify({ type: 'event', event, payload }))
    }

    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(message)) as RpcFrame
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')

      if (method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30000 },
          auth: {
            runModePolicy: {
              allowedRunModes: ['trusted', 'standard', 'full'],
              defaultRunMode: 'standard',
            },
          },
        }))
        return
      }

      if (method === 'chat.history') {
        if (options.historyCalls) options.historyCalls.value += 1
        ws.send(response(frame.id, {
          messages: options.history?.() || [],
          has_more: false,
          canonical_complete: true,
        }))
        return
      }

      if (method === 'sessions.messages.subscribe') {
        const runStatus = options.runStatus || 'idle'
        ws.send(response(frame.id, {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: runStatus,
          active_task: runStatus === 'idle'
            ? null
            : { task_id: 'task-e2e-running', status: runStatus },
        }))
        if (!subscribeCallbackSent && options.afterSubscribe) {
          subscribeCallbackSent = true
          setTimeout(() => options.afterSubscribe?.(sendEvent), 20)
        }
        return
      }

      if (method === 'chat.abort') {
        options.abortCalls?.push(frame.params || {})
        ws.send(response(frame.id, { aborted: true }))
        return
      }

      if (method === 'sandbox.setup.status') {
        if (options.sandboxStatusCalls) options.sandboxStatusCalls.value += 1
        ws.send(response(frame.id, options.sandboxStatus?.() || {
          state: 'ready',
          platform: 'linux',
        }))
        return
      }

      if (method === 'sandbox.setup.ensure') {
        if (options.sandboxEnsureCalls) options.sandboxEnsureCalls.value += 1
        ws.send(response(frame.id, options.sandboxEnsure?.() || {
          state: 'setting_up',
          platform: 'windows',
        }))
        return
      }

      if (method.endsWith('.approval.status')) {
        ws.send(response(frame.id, { found: true, pending: true }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.unsubscribe': { subscribed: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })
}

async function openChat(page: Page) {
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })
  await expect(page.locator('.chat-header')).toBeVisible({ timeout: 10000 })
}

const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

test.describe('Vue behavior contracts', () => {
  test('unknown routes render the 404 actions without replacing the last stable route', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('opensquilla-last-route', '/sessions'))
    await installMockGateway(page)

    await page.goto(CONTROL_URL + 'removed-legacy-screen')
    const notFound = page.locator('.not-found')
    await expect(notFound).toBeVisible()
    await expect(notFound.getByText('404', { exact: true })).toBeVisible()
    await expect(notFound.getByRole('button', { name: 'Go to Chat' })).toBeVisible()
    await expect(notFound.getByRole('button', { name: 'Sessions' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => localStorage.getItem('opensquilla-last-route')))
      .toBe('/sessions')

    await notFound.getByRole('button', { name: 'Go to Chat' }).click()
    await expect(page).toHaveURL(/\/control\/chat(?:\?|$)/)

    await page.goto(CONTROL_URL + 'still-not-a-route')
    await page.locator('.not-found').getByRole('button', { name: 'Sessions' }).click()
    await expect(page).toHaveURL(/\/control\/sessions(?:\?|$)/)
  })

  test('drawer, nested preview, and lightbox own Escape while composer Escape aborts once', async ({ page }) => {
    const abortCalls: Array<Record<string, unknown>> = []
    await page.route('**/api/v1/artifacts/**', route => route.fulfill({
      status: 200,
      contentType: 'image/png',
      body: PNG_1X1,
    }))
    await installMockGateway(page, {
      abortCalls,
      runStatus: 'running',
      history: () => [{
        role: 'assistant',
        text: 'The requested files are ready.',
        message_id: 'message-e2e-dialogs',
        timestamp: '2026-07-22T10:00:00Z',
        artifacts: [
          {
            id: 'artifact-e2e-dialog-image',
            name: 'dialog.png',
            mime: 'image/png',
            size: 68,
            download_url: '/api/v1/artifacts/artifact-e2e-dialog-image',
            thumbnail_url: '/api/v1/artifacts/artifact-e2e-dialog-image?variant=thumb',
          },
          {
            id: 'artifact-e2e-dialog-notes',
            name: 'notes.txt',
            mime: 'text/plain',
            size: 18,
            download_url: '/api/v1/artifacts/artifact-e2e-dialog-notes',
          },
        ],
      }],
    })
    await openChat(page)
    await expect(page.getByRole('button', { name: 'Stop current response' })).toBeVisible()

    const deliverables = page.locator('.chat-deliverables-btn')
    await expect(deliverables).toBeVisible()
    await deliverables.click()
    await expect(page.locator('.deliv-drawer')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.deliv-drawer')).toHaveCount(0)
    expect(abortCalls).toHaveLength(0)

    await deliverables.click()
    await page.locator('.deliv-tile', { hasText: 'notes.txt' }).click()
    await expect(page.locator('.deliv-preview')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.deliv-preview')).toHaveCount(0)
    await expect(page.locator('.deliv-drawer')).toBeVisible()
    expect(abortCalls).toHaveLength(0)
    await page.keyboard.press('Escape')
    await expect(page.locator('.deliv-drawer')).toHaveCount(0)

    const imageButton = page.locator('.msg-media-card__img')
    await expect(imageButton.locator('img')).toBeVisible({ timeout: 10000 })
    await imageButton.click()
    await expect(page.locator('.deliv-preview[role="dialog"]')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.deliv-preview[role="dialog"]')).toHaveCount(0)
    expect(abortCalls).toHaveLength(0)

    await page.locator('.chat-textarea').focus()
    await page.keyboard.press('Escape')
    await expect.poll(() => abortCalls.length).toBe(1)
    await page.waitForTimeout(100)
    expect(abortCalls).toHaveLength(1)
  })

  test('approval and clarify inputs keep Escape local; bare chat Escape aborts once', async ({ page }) => {
    const abortCalls: Array<Record<string, unknown>> = []
    await installMockGateway(page, {
      abortCalls,
      runStatus: 'running',
      pendingApprovals: [{
        id: 'approval-e2e-escape',
        namespace: 'exec',
        toolName: 'shell',
        command: 'rm -rf build/cache',
        args: { command: 'rm -rf build/cache' },
        warning: 'This command removes files.',
        sessionKey: SESSION_KEY,
        created_at: Date.now() / 1000,
      }],
      afterSubscribe: sendEvent => sendEvent('session.event.tool_result', {
        session_key: SESSION_KEY,
        task_id: 'task-e2e-running',
        stream_seq: 1,
        tool_use_id: 'tool-e2e-clarify',
        name: 'meta-step:project_clarify',
        result: 'paused: awaiting user input',
        arguments: {
          kind: 'user_input',
          paused: true,
          run_id: 'run-e2e',
          step: 'project_clarify',
          clarify_schema: {
            intro: 'One detail is required.',
            fields: [{
              name: 'topic',
              prompt: 'Project topic',
              type: 'string',
              required: true,
            }],
          },
        },
      }),
    })
    await openChat(page)

    const approvalNote = page.locator('.approval-card__note')
    await expect(approvalNote).toBeVisible({ timeout: 10000 })
    await approvalNote.focus()
    await page.keyboard.press('Escape')
    expect(abortCalls).toHaveLength(0)

    const clarifyInput = page.locator('.clarify-field__input')
    await expect(clarifyInput).toBeVisible({ timeout: 10000 })
    await clarifyInput.focus()
    await page.keyboard.press('Escape')
    expect(abortCalls).toHaveLength(0)

    await page.locator('.chat-header').click()
    await expect(clarifyInput).not.toBeFocused()
    await page.keyboard.press('Escape')
    await expect.poll(() => abortCalls.length).toBe(1)
    await page.waitForTimeout(100)
    expect(abortCalls).toHaveLength(1)
  })

  test('cron result appears live and its persisted replay does not duplicate it', async ({ page }) => {
    const historyCalls = { value: 0 }
    let cronPersisted = false
    const cronHistory = {
      role: 'assistant',
      text: 'Scheduled health check completed.',
      message_id: 'message-e2e-cron-result',
      timestamp: '2026-07-22T10:00:00Z',
      provenance_kind: 'cron',
      provenance_source_tool: 'cron.run',
    }
    await installMockGateway(page, {
      historyCalls,
      history: () => cronPersisted ? [cronHistory] : [],
      afterSubscribe: sendEvent => {
        cronPersisted = true
        const message = {
          role: 'assistant',
          text: cronHistory.text,
          timestamp: cronHistory.timestamp,
          messageId: cronHistory.message_id,
          provenanceKind: 'cron',
          provenanceSourceTool: 'cron.run',
        }
        sendEvent('session.event.cron_result', {
          session_key: SESSION_KEY,
          stream_seq: 1,
          message,
        })
        sendEvent('session.event.cron_result', {
          session_key: SESSION_KEY,
          stream_seq: 2,
          message,
        })
      },
    })
    await openChat(page)

    const cronMessage = page.locator('.msg-ai[data-message-id="message-e2e-cron-result"]')
    await expect(cronMessage).toBeVisible({ timeout: 10000 })
    await expect(cronMessage).toContainText('Scheduled health check completed.')
    await expect(cronMessage.locator('.msg-provenance-chip')).toHaveText(/Scheduled/)
    await expect(cronMessage.locator('.msg-provenance-chip')).toHaveAttribute('title', /cron\.run/)
    await expect.poll(() => historyCalls.value).toBeGreaterThanOrEqual(2)
    await page.waitForTimeout(150)
    await expect(cronMessage).toHaveCount(1)
  })

  test('Windows Standard mode offers setup and preserves the selected run mode', async ({ page }) => {
    const statusCalls = { value: 0 }
    const ensureCalls = { value: 0 }
    await page.addInitScript(() => {
      localStorage.setItem('opensquilla.chat.runMode', 'standard')
    })
    await installMockGateway(page, {
      sandboxStatusCalls: statusCalls,
      sandboxEnsureCalls: ensureCalls,
      sandboxStatus: () => ({
        state: 'not_setup',
        platform: 'windows',
        message: 'Windows Sandbox needs setup.',
      }),
      sandboxEnsure: () => ({
        state: 'setting_up',
        platform: 'windows',
        message: 'Installing Windows Sandbox.',
      }),
    })
    await openChat(page)

    const banner = page.locator('.sandbox-setup')
    await expect(banner).toBeVisible({ timeout: 10000 })
    await expect(banner).toContainText('Sandbox setup required')
    await expect.poll(() => statusCalls.value).toBeGreaterThanOrEqual(1)

    await banner.getByRole('button', { name: 'Set up' }).click()
    await expect.poll(() => ensureCalls.value).toBe(1)
    await expect(banner).toHaveClass(/sandbox-setup--setting_up/)
    await expect(banner).toContainText('Setting up Windows Sandbox')
    await expect(banner.getByRole('progressbar')).toBeVisible()
    await expect.poll(() => page.evaluate(() => localStorage.getItem('opensquilla.chat.runMode')))
      .toBe('standard')
  })
})
