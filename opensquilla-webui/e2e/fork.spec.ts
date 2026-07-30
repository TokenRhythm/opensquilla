import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const SESSION_KEY = 'agent:main:webchat:e2efork'
const EDIT_PARENT_KEY = 'agent:main:webchat:e2e-edit-parent'
const EDIT_CHILD_KEY = 'agent:main:webchat:e2e-edit-child'
const FORK_BUTTON = '[data-testid="fork-conversation"]'

type CapturedEditSend = {
  message?: string
  sessionKey?: string
  forkBeforeMessageId?: string
  [key: string]: unknown
}

function sessionFromUrl(url: string): string {
  try {
    return new URL(url).searchParams.get('session') || ''
  } catch {
    return ''
  }
}

// Seed a settled two-turn thread through the real WS pipeline: chat.history
// responses are rewritten in flight so two assistant messages render without
// a live agent run.
async function seedHistoryWithTwoTurns(page: Page) {
  await page.routeWebSocket(/\/ws$/, ws => {
    const server = ws.connectToServer()
    const historyIds = new Set<string>()
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'req' && frame.method === 'chat.history') {
          historyIds.add(String(frame.id))
        }
      } catch {}
      server.send(message)
    })
    server.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'res' && frame.id !== undefined && historyIds.has(String(frame.id))) {
          historyIds.delete(String(frame.id))
          frame.ok = true
          delete frame.error
          frame.payload = {
            messages: [
              {
                role: 'user',
                text: 'First question.',
                id: 'msg-e2e-fork-user-1',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'First answer.',
                id: 'msg-e2e-fork-ai-1',
                timestamp: Math.floor(Date.now() / 1000) - 110,
                usage: { model: 'openai/gpt-test', input_tokens: 20, output_tokens: 8, cost_usd: 0.0002 },
              },
              {
                role: 'user',
                text: 'Second question.',
                id: 'msg-e2e-fork-user-2',
                timestamp: Math.floor(Date.now() / 1000) - 60,
              },
              {
                role: 'assistant',
                text: 'Second answer.',
                id: 'msg-e2e-fork-ai-2',
                timestamp: Math.floor(Date.now() / 1000) - 50,
                usage: { model: 'openai/gpt-test', input_tokens: 30, output_tokens: 10, cost_usd: 0.0003 },
              },
            ],
            has_more: false,
          }
          ws.send(JSON.stringify(frame))
          return
        }
      } catch {}
      ws.send(message)
    })
  })
}

async function mockBranchingEditRpc(
  page: Page,
  capturedSends: CapturedEditSend[],
  historyRequests: string[],
) {
  const parentMessages = [
    {
      role: 'user',
      text: 'A marker',
      message_id: 'msg-A',
      timestamp: '2026-07-03T00:00:01.000Z',
    },
    {
      role: 'assistant',
      text: 'ack A',
      message_id: 'msg-ack-A',
      timestamp: '2026-07-03T00:00:02.000Z',
    },
    {
      role: 'user',
      text: 'B marker',
      message_id: 'msg-B',
      timestamp: '2026-07-03T00:00:03.000Z',
    },
    {
      role: 'assistant',
      text: 'ack B',
      message_id: 'msg-ack-B',
      timestamp: '2026-07-03T00:00:04.000Z',
    },
    {
      role: 'user',
      text: 'C marker must stay only on parent',
      message_id: 'msg-C',
      timestamp: '2026-07-03T00:00:05.000Z',
    },
  ]
  const childMessages = [
    parentMessages[0],
    parentMessages[1],
    {
      role: 'user',
      text: 'B edited',
      message_id: 'child-msg-B-edited',
      timestamp: '2026-07-03T00:00:06.000Z',
    },
  ]

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (method === 'chat.send') {
          capturedSends.push((frame.params || {}) as CapturedEditSend)
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: {
              sessionKey: EDIT_CHILD_KEY,
              status: 'accepted',
              task_id: 'e2e-edit-task',
            },
          }))
          return
        }
        if (method === 'chat.history') {
          const key = String(frame.params?.sessionKey || '')
          historyRequests.push(key)
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: {
              messages: key === EDIT_CHILD_KEY ? childMessages : parentMessages,
              history_scope: 'complete',
              has_more: false,
            },
          }))
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
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: payloads[method] ?? {},
        }))
      } catch (err) {
        if (!(err instanceof SyntaxError)) throw err
      }
    })
  })
}

test.describe('Conversation fork', () => {
  test('empty draft offers no fork action', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.chat-textarea')).toBeVisible()
    await expect(page.locator(FORK_BUTTON)).toHaveCount(0)
  })

  test('fork renders only on the last assistant message of the thread', async ({ page }) => {
    await seedHistoryWithTwoTurns(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.msg-ai')).toHaveCount(2, { timeout: 10000 })
    // Whole-conversation fork: one button on the tip, none on earlier turns.
    await expect(page.locator(FORK_BUTTON)).toHaveCount(1)
    await expect(page.locator('.msg-ai').last().locator(FORK_BUTTON)).toHaveCount(1)
    await expect(page.locator('.msg-ai').first().locator(FORK_BUTTON)).toHaveCount(0)
    await expect(page.locator(FORK_BUTTON)).toHaveAttribute('aria-label', 'Fork conversation')
    // The retired follow-up row stays gone.
    await expect(page.locator('.done-card')).toHaveCount(0)
  })

  test('editing a middle message forks before it without leaking later history', async ({ page }) => {
    const capturedSends: CapturedEditSend[] = []
    const historyRequests: string[] = []
    await mockBranchingEditRpc(page, capturedSends, historyRequests)

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(EDIT_PARENT_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page.locator('.msg-user')).toHaveCount(3, { timeout: 10000 })
    await expect(page.locator('.msg-user').last()).toContainText('C marker must stay only on parent')

    const middleMessage = page.locator('.msg-user').nth(1)
    await middleMessage.hover()
    await middleMessage.getByRole('button', { name: 'Edit' }).click()

    // Editing B rewinds the local transcript to the point before B. Neither
    // B's old answer nor the later C turn may be carried into the new branch.
    await expect(page.locator('.chat-textarea')).toHaveValue('B marker')
    await expect(page.locator('.msg-user')).toHaveCount(1)
    await expect(page.locator('.chat-thread')).not.toContainText('ack B')
    await expect(page.locator('.chat-thread')).not.toContainText('C marker')

    await page.locator('.chat-textarea').fill('B edited')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => capturedSends.length).toBe(1)

    const send = capturedSends[0]
    expect(send).toMatchObject({
      message: 'B edited',
      sessionKey: EDIT_PARENT_KEY,
      forkBeforeMessageId: 'msg-B',
    })
    expect(send).not.toHaveProperty('messages')
    expect(send).not.toHaveProperty('history')
    expect(JSON.stringify(send)).not.toContain('ack B')
    expect(JSON.stringify(send)).not.toContain('C marker')

    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(EDIT_CHILD_KEY)

    // A fresh load proves the URL now addresses the child transcript, whose
    // canonical history ends at the edited B rather than replaying parent C.
    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect.poll(() => historyRequests.filter(key => key === EDIT_CHILD_KEY).length).toBeGreaterThan(0)
    await expect(page.locator('.msg-user')).toHaveCount(2, { timeout: 10000 })
    await expect(page.locator('.chat-thread')).toContainText('B edited')
    await expect(page.locator('.chat-thread')).not.toContainText('C marker')
    expect(historyRequests).toContain(EDIT_PARENT_KEY)
  })

  test('live fork copies the thread into a new session with hub lineage', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(300000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // One real turn so the session exists with a transcript.
    const prompt = 'Reply with the single word: ok'
    await page.locator('.chat-textarea').fill(prompt)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect(page.locator('.msg-ai').first()).toBeVisible({ timeout: 120000 })
    await expect(page.locator('.work-card')).toHaveCount(0, { timeout: 120000 })

    const parentKey = sessionFromUrl(page.url())
    expect(parentKey).toMatch(/^agent:.+:webchat:/)

    // No done card after completion; the fork action sits in the meta cluster
    // of the tip message.
    await expect(page.locator('.done-card')).toHaveCount(0)
    const tip = page.locator('.msg-ai').last()
    await tip.hover()
    await expect(tip.locator(FORK_BUTTON)).toHaveCount(1)
    await tip.locator(FORK_BUTTON).click()

    // Navigation lands on a NEW session key.
    await page.waitForURL(url => {
      const key = sessionFromUrl(url.toString())
      return !!key && key !== parentKey
    }, { timeout: 30000 })
    const childKey = sessionFromUrl(page.url())
    expect(childKey).toMatch(/^agent:.+:webchat:/)
    expect(childKey).not.toBe(parentKey)

    // The child thread shows the copied messages.
    await expect(page.locator('.msg-user').filter({ hasText: prompt })).toBeVisible({ timeout: 30000 })
    await expect(page.locator('.msg-ai').first()).toBeVisible()

    // Hub: the fork lists under its parent with the FORK badge and indent,
    // and the parent still lists independently as a root row.
    await page.goto(CONTROL_URL + 'sessions')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await page.waitForTimeout(800)
    await expect(page.locator('.hub-ledger')).toBeVisible()

    const titleFragment = 'Reply with the single word'
    const forkRow = page.locator('.hub-row--child')
      .filter({ has: page.locator('.hub-row__fork-badge') })
      .filter({ hasText: titleFragment })
      .first()
    await expect(forkRow).toBeVisible({ timeout: 15000 })
    await expect(forkRow.locator('.hub-row__fork-badge')).toHaveText(/fork/i)
    expect((await forkRow.locator('.hub-row__title').innerText()).trim().startsWith('↳ ')).toBe(true)

    // Indented under the parent like the rest of the lineage language.
    const childPad = await forkRow.locator('.hub-row__main').evaluate(
      el => parseFloat(getComputedStyle(el as HTMLElement).paddingLeft))
    const rootPad = await page.locator('.hub-row:not(.hub-row--child) .hub-row__main').first().evaluate(
      el => parseFloat(getComputedStyle(el as HTMLElement).paddingLeft))
    expect(childPad).toBeGreaterThan(rootPad)

    // Parent row remains an independent root entry.
    const parentRow = page.locator('.hub-row:not(.hub-row--child)').filter({ hasText: titleFragment })
    await expect(parentRow.first()).toBeVisible()
  })
})
