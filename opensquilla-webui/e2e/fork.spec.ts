import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const SESSION_KEY = 'agent:main:webchat:e2efork'
const THROUGH_TURN_CHILD_KEY = 'agent:main:webchat:e2efork-through-child'
const SESSION_TITLE = 'Release planning notes'
const THROUGH_TURN_CHILD_TITLE = `${SESSION_TITLE} (2)`
const EDIT_PARENT_KEY = 'agent:main:webchat:e2e-edit-parent'
const EDIT_CHILD_KEY = 'agent:main:webchat:e2e-edit-child'
const FORK_BUTTON = '[data-testid="fork-conversation"]'

type CapturedEditSend = {
  message?: string
  sessionKey?: string
  forkBeforeMessageId?: string
  [key: string]: unknown
}

type CapturedForkRequest = {
  method: string
  params: Record<string, unknown>
}

function sessionFromUrl(url: string): string {
  try {
    return new URL(url).searchParams.get('session') || ''
  } catch {
    return ''
  }
}

// Seed a settled two-turn thread through the same deterministic WS mock used by
// the edit coverage below. The historical-fork contract must not require a live
// gateway merely to prove its method name and inclusive turn boundary.
async function seedHistoryWithTwoTurns(
  page: Page,
  capturedForks: CapturedForkRequest[] = [],
) {
  let forkCreated = false
  const parentHistory = {
    messages: [
      {
        role: 'user',
        text: 'First question.',
        id: 'msg-e2e-fork-user-1',
        timestamp: Math.floor(Date.now() / 1000) - 120,
        turn_context: { turn_id: 'turn-e2e-fork-1' },
      },
      {
        role: 'assistant',
        text: 'First answer.',
        id: 'msg-e2e-fork-ai-1',
        timestamp: Math.floor(Date.now() / 1000) - 110,
        turn_context: { turn_id: 'turn-e2e-fork-1' },
        usage: { model: 'openai/gpt-test', input_tokens: 20, output_tokens: 8, cost_usd: 0.0002 },
      },
      {
        role: 'user',
        text: 'Second question.',
        id: 'msg-e2e-fork-user-2',
        timestamp: Math.floor(Date.now() / 1000) - 60,
        turn_context: { turn_id: 'turn-e2e-fork-2' },
      },
      {
        role: 'assistant',
        text: 'Second answer.',
        id: 'msg-e2e-fork-ai-2',
        timestamp: Math.floor(Date.now() / 1000) - 50,
        turn_context: { turn_id: 'turn-e2e-fork-2' },
        usage: { model: 'openai/gpt-test', input_tokens: 30, output_tokens: 10, cost_usd: 0.0003 },
      },
    ],
    turn_outcomes: [
      {
        turn_id: 'turn-e2e-fork-1',
        task_id: 'turn-e2e-fork-1',
        status: 'succeeded',
        outcome: { kind: 'completed' },
      },
      {
        turn_id: 'turn-e2e-fork-2',
        task_id: 'turn-e2e-fork-2',
        status: 'succeeded',
        outcome: { kind: 'completed' },
      },
    ],
    has_more: false,
  }
  const childHistory = {
    messages: parentHistory.messages.slice(0, 2).map((message, index) => ({
      ...message,
      id: index === 0 ? 'child-msg-e2e-fork-user-1' : 'child-msg-e2e-fork-ai-1',
    })),
    turn_outcomes: parentHistory.turn_outcomes.slice(0, 1),
    has_more: false,
  }

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
        if (method === 'sessions.forkThroughTurn') {
          forkCreated = true
          capturedForks.push({
            method,
            params: { ...(frame.params || {}) },
          })
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: {
              key: THROUGH_TURN_CHILD_KEY,
              forkMode: 'through_turn',
              throughTurnId: frame.params?.throughTurnId,
            },
          }))
          ws.send(JSON.stringify({
            type: 'event',
            event: 'sessions.changed',
            payload: { key: THROUGH_TURN_CHILD_KEY },
          }))
          return
        }
        if (method === 'chat.history') {
          const payload = frame.params?.sessionKey === THROUGH_TURN_CHILD_KEY
            ? childHistory
            : parentHistory
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload,
          }))
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: {
              subscribed: true,
              replay_complete: true,
              current_stream_seq: 0,
              hydration_complete: true,
              run_status: 'idle',
            },
          }))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          ws.send(JSON.stringify({
            type: 'res',
            id: frame.id,
            ok: true,
            payload: {
              key: THROUGH_TURN_CHILD_KEY,
              events: [],
              current_stream_seq: 0,
            },
          }))
          return
        }
        const sessionRow = (
          key: string,
          title: string,
          updatedAt: number,
          extra: Record<string, unknown> = {},
        ) => ({
          key,
          title,
          sessionKind: 'chat',
          surface: 'webchat',
          conversationKind: 'direct',
          effectiveAgentId: 'main',
          updatedAt,
          messageCount: 2,
          status: 'ok',
          runStatus: 'idle',
          ...extra,
        })
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          },
          'sessions.list': {
            sessions: forkCreated
              ? [
                  sessionRow(
                    THROUGH_TURN_CHILD_KEY,
                    THROUGH_TURN_CHILD_TITLE,
                    200,
                    {
                      forked_from_parent: true,
                      parent: { key: SESSION_KEY, title: SESSION_TITLE },
                    },
                  ),
                  sessionRow(SESSION_KEY, SESSION_TITLE, 100),
                ]
              : [sessionRow(SESSION_KEY, SESSION_TITLE, 100)],
            has_more: false,
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

  test('fork renders on every completed assistant turn', async ({ page }) => {
    await seedHistoryWithTwoTurns(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.msg-ai')).toHaveCount(2, { timeout: 10000 })
    await expect(page.locator(FORK_BUTTON)).toHaveCount(2)
    await expect(page.locator('.msg-ai').last().locator(FORK_BUTTON)).toHaveCount(1)
    await expect(page.locator('.msg-ai').first().locator(FORK_BUTTON)).toHaveCount(1)
    await expect(page.locator(FORK_BUTTON).first()).toHaveAttribute('aria-label', 'Fork from here')
    // The retired follow-up row stays gone.
    await expect(page.locator('.done-card')).toHaveCount(0)
  })

  test('historical fork uses the dedicated through-turn RPC and exact boundary', async ({ page }) => {
    const capturedForks: CapturedForkRequest[] = []
    await seedHistoryWithTwoTurns(page, capturedForks)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page.locator('.msg-ai')).toHaveCount(2, { timeout: 10000 })

    const firstAnswer = page.locator('.msg-ai').first()
    await firstAnswer.hover()
    await firstAnswer.locator(FORK_BUTTON).click()

    await expect.poll(() => capturedForks).toEqual([{
      method: 'sessions.forkThroughTurn',
      params: {
        key: SESSION_KEY,
        throughTurnId: 'turn-e2e-fork-1',
      },
    }])
    await expect.poll(() => sessionFromUrl(page.url())).toBe(THROUGH_TURN_CHILD_KEY)
    await expect(page.locator('.chat-thread')).toContainText('First answer.')
    await expect(page.locator('.chat-thread')).not.toContainText('Second question.')

    const parentRow = page.locator(
      `.sidebar-history-row[data-session-key="${SESSION_KEY}"]`,
    )
    const childRow = page.locator(
      `.sidebar-history-row[data-session-key="${THROUGH_TURN_CHILD_KEY}"]`,
    )
    await expect(parentRow.locator('.sidebar-history-title')).toHaveText(SESSION_TITLE)
    await expect(childRow.locator('.sidebar-history-title')).toHaveText(THROUGH_TURN_CHILD_TITLE)
    await expect(parentRow).toHaveAttribute('data-depth', '0')
    await expect(childRow).toHaveAttribute('data-depth', '0')
    await expect(parentRow.locator('.sidebar-history-rail')).toHaveCount(0)
    await expect(childRow.locator('.sidebar-history-rail')).toHaveCount(0)
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
