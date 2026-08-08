import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_A = 'agent:main:webchat:e2e-floating-a'
const SESSION_B = 'agent:main:webchat:e2e-floating-b'

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function historyFor(sessionKey: string) {
  const label = sessionKey === SESSION_B ? 'Session B' : 'Session A'
  return Array.from({ length: 48 }, (_, index) => ({
    role: index % 2 === 0 ? 'user' : 'assistant',
    text: `${label} message ${index + 1}. ${'Synthetic conversation detail. '.repeat(8)}`,
    message_id: `${sessionKey.split(':').at(-1)}-${index + 1}`,
    timestamp: `2026-07-22T10:${String(index).padStart(2, '0')}:00Z`,
  }))
}

async function installMockGateway(page: Page) {
  await page.route('**/api/system/update', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({}),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ enabled: false }),
  }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
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
              allowedRunModes: ['safe', 'full'],
              defaultRunMode: 'full',
            },
          },
        }))
        return
      }

      if (method === 'chat.history') {
        const key = String(frame.params?.key || frame.params?.sessionKey || SESSION_A)
        ws.send(response(frame.id, {
          messages: historyFor(key),
          has_more: false,
          canonical_complete: true,
        }))
        return
      }

      if (method === 'sessions.messages.subscribe') {
        ws.send(response(frame.id, {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
          active_task: null,
        }))
        return
      }

      if (method === 'sandbox.run_mode.preference.get') {
        ws.send(response(frame.id, { runMode: 'full', source: 'config' }))
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
        'sandbox.capability.status': { available: false },
        'sessions.list': {
          sessions: [
            {
              key: SESSION_B,
              title: 'Floating Session B',
              sessionKind: 'chat',
              surface: 'webchat',
              conversationKind: 'direct',
              effectiveAgentId: 'main',
              updatedAt: 200,
              messageCount: 48,
              status: 'ok',
              runStatus: 'idle',
            },
            {
              key: SESSION_A,
              title: 'Floating Session A',
              sessionKind: 'chat',
              surface: 'webchat',
              conversationKind: 'direct',
              effectiveAgentId: 'main',
              updatedAt: 100,
              messageCount: 48,
              status: 'ok',
              runStatus: 'idle',
            },
          ],
          has_more: false,
        },
        'sessions.messages.unsubscribe': { subscribed: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })
}

async function openChat(page: Page, sessionKey = SESSION_A, enabled = true) {
  await page.addInitScript((preference: boolean) => {
    localStorage.setItem('opensquilla.composerFx', JSON.stringify({ enabled: preference }))
  }, enabled)
  await installMockGateway(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(sessionKey))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })
  await expect(page.getByText('Session A message 48.', { exact: false })).toBeVisible()
  await expect.poll(() => page.locator('.chat-thread').evaluate(el => (
    el.scrollHeight > el.clientHeight
  ))).toBe(true)
}

async function scrollGap(page: Page) {
  return page.locator('.chat-thread').evaluate(el => (
    el.scrollHeight - el.scrollTop - el.clientHeight
  ))
}

async function expectDockClearance(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const dock = document.querySelector<HTMLElement>('.chat-composer-dock')!
    const thread = document.querySelector<HTMLElement>('.chat-thread')!
    return Number.parseFloat(getComputedStyle(thread).paddingBottom)
      - dock.getBoundingClientRect().height
  })).toBeGreaterThanOrEqual(0)
}

test('floating composer reacts only to user scroll and resets across sessions', async ({ page }) => {
  await openChat(page)
  const chat = page.locator('.chat')
  const thread = page.locator('.chat-thread')

  await expect(chat).toHaveClass(/chat--composer-floating/)
  await thread.evaluate(el => { el.scrollTop = el.scrollHeight })
  await expect.poll(() => scrollGap(page)).toBeLessThan(2)
  await expectDockClearance(page)

  await thread.hover({ position: { x: 120, y: 120 } })
  await page.mouse.wheel(0, -400)
  await expect(chat).toHaveClass(/chat--composer-collapsed/)

  await page.mouse.wheel(0, 40)
  await expect(chat).not.toHaveClass(/chat--composer-collapsed/)

  // A large programmatic move (history prepend/anchor/minimap shape) only
  // synchronizes the baseline and must not retract the composer.
  await thread.evaluate(el => { el.scrollTop = 0 })
  await expect(chat).not.toHaveClass(/chat--composer-collapsed/)

  const latest = page.locator('.chat-jump-latest')
  await expect(latest).toBeVisible()
  await latest.click()
  await expect(chat).not.toHaveClass(/chat--composer-collapsed/)
  await expect.poll(() => scrollGap(page)).toBeLessThan(2)

  const textarea = page.locator('.chat-textarea')
  await textarea.fill(Array.from({ length: 20 }, (_, i) => `draft line ${i + 1}`).join('\n'))
  await expectDockClearance(page)

  await thread.hover({ position: { x: 120, y: 120 } })
  await page.mouse.wheel(0, -400)
  await expect(chat).toHaveClass(/chat--composer-collapsed/)

  await page
    .locator('.sidebar-history-row[data-family="chats"]')
    .filter({ hasText: 'Floating Session B' })
    .locator('.sidebar-history-item')
    .click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expect(page.getByText('Session B message 48.', { exact: false })).toBeVisible()
  await expect(chat).not.toHaveClass(/chat--composer-collapsed/)
})

test('disabled preference restores the established docked layout', async ({ page }) => {
  await openChat(page, SESSION_A, false)
  const chat = page.locator('.chat')
  const composer = page.locator('.chat-composer')
  const dock = page.locator('.chat-composer-dock')

  await expect(chat).not.toHaveClass(/chat--composer-floating/)
  await expect(composer).toHaveClass(/chat-composer--docked/)
  await expect(composer).not.toHaveClass(/chat-composer--floating/)
  expect(await dock.evaluate(el => getComputedStyle(el).position)).toBe('relative')
})

test.describe('mobile and reduced motion', () => {
  test.use({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })

  test('keeps the mobile dock legible, bounded, and motion-reduced', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await openChat(page)
    expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches))
      .toBe(true)
    await expectDockClearance(page)

    const durations = await page.evaluate(() => [
      '.chat-composer',
      '.chat-collapse-region',
      '.chat-input-panel',
      '.chat-textarea',
    ].map(selector => getComputedStyle(document.querySelector<HTMLElement>(selector)!).transitionDuration))
    expect(durations).toEqual(['0s', '0s', '0s', '0s'])

    // Leave the live edge while keeping the composer expanded. Messages now
    // pass behind the floating dock, reproducing the layout where an
    // unbacked disclaimer used to draw directly over transcript text.
    await page.locator('.chat-thread').evaluate(el => {
      el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 320)
    })
    await expect(page.locator('.chat-jump-latest')).toBeVisible()

    const mobileGeometry = await page.evaluate(() => {
      const dock = document.querySelector<HTMLElement>('.chat-composer-dock')!
      const disclaimer = document.querySelector<HTMLElement>('.chat-ai-disclaimer')!
      const tabbar = document.querySelector<HTMLElement>('.mobile-tabbar')!
      const disclaimerStyle = getComputedStyle(disclaimer)
      return {
        dockBottom: dock.getBoundingClientRect().bottom,
        disclaimerBottom: disclaimer.getBoundingClientRect().bottom,
        tabbarTop: tabbar.getBoundingClientRect().top,
        disclaimerBackground: disclaimerStyle.backgroundColor,
      }
    })
    expect(mobileGeometry.dockBottom).toBeLessThanOrEqual(mobileGeometry.tabbarTop + 1)
    expect(mobileGeometry.disclaimerBottom).toBeLessThanOrEqual(mobileGeometry.tabbarTop + 1)
    expect(mobileGeometry.disclaimerBackground).not.toBe('rgba(0, 0, 0, 0)')
    expect(mobileGeometry.disclaimerBackground).not.toBe('transparent')
  })
})
