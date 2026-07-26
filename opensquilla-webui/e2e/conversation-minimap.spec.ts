import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-long-history-12-turns'

function longHistoryMessages() {
  const now = Math.floor(Date.now() / 1000)
  return Array.from({ length: 12 }, (_, index) => {
    const turn = index + 1
    return [
      {
        role: 'user',
        text: `Question ${turn}: verify the long conversation history navigator at this exact turn.`,
        id: `history-user-${turn}`,
        timestamp: now - (24 - index * 2) * 60,
      },
      {
        role: 'assistant',
        text: `Answer ${turn}. ${Array.from({ length: 8 }, (__, paragraph) => (
          `This is deterministic fixture paragraph ${paragraph + 1} for turn ${turn}; it makes the thread tall enough to exercise scroll and resize anchoring.`
        )).join('\n\n')}`,
        id: `history-assistant-${turn}`,
        timestamp: now - (23 - index * 2) * 60,
      },
    ]
  }).flat()
}

async function seedLongHistory(page: Page) {
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
        if (frame.method === 'connect') {
          ws.send(JSON.stringify({
            protocol: 3,
            policy: { tick_interval_ms: 30000 },
          }))
          return
        }

        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: longHistoryMessages(), has_more: false },
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
          payload: payloads[String(frame.method)] ?? {},
        }))
      } catch {}
    })
  })
}

test.describe('Long conversation history rail', () => {
  test('769px compact dock preserves a readable chat title without horizontal overflow', async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem('opensquilla.sidebar.width.v1'))
    await page.setViewportSize({ width: 769, height: 900 })
    await seedLongHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const main = page.locator('#app-main')
    const sidebar = page.locator('.sidebar')
    const header = page.locator('.chat-header')
    const title = page.locator('.chat-header .chat-label')

    await expect(main).toHaveClass(/main--sidebar-compact/)
    await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
    await expect.poll(async () => Math.round((await sidebar.boundingBox())?.width || 0)).toBe(260)
    await expect.poll(async () => Math.round((await title.boundingBox())?.width || 0)).toBeGreaterThanOrEqual(96)
    await expect.poll(() => header.evaluate(element => (
      element.scrollWidth <= element.clientWidth
    ))).toBe(true)
    await expect(page.locator('.chat-header .chat-share-btn__label').first()).toBeHidden()
    await expect(page.locator('.topbar .lang-menu-current')).toBeHidden()
  })

  test('reduced motion keeps a static local arrival guide without painting the whole row', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.addInitScript(() => localStorage.removeItem('opensquilla.sidebar.width.v1'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await seedLongHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const markers = page.getByTestId('conversation-minimap-marker')
    await expect(markers).toHaveCount(12, { timeout: 10000 })
    await markers.nth(0).click()

    const firstTurn = page.locator('[data-chat-turn-key="history-user-1"]')
    await expect(firstTurn).toHaveClass(/is-history-target/, { timeout: 3000 })
    const arrivalPaint = await firstTurn.evaluate((element) => {
      const stack = element.querySelector('.msg-user-stack')
      const rowStyle = getComputedStyle(element)
      const guideStyle = stack ? getComputedStyle(stack, '::after') : null
      const maxDuration = (value: string) => Math.max(
        ...value.split(',').map(part => Number.parseFloat(part) || 0),
      )
      return {
        rowBackground: rowStyle.backgroundColor,
        rowShadow: rowStyle.boxShadow,
        guideAnimation: guideStyle?.animationName || '',
        guideOpacity: guideStyle?.opacity || '',
        guideWidth: guideStyle?.width || '',
        maxAnimationDuration: maxDuration(guideStyle?.animationDuration || '0s'),
        maxTransitionDuration: maxDuration(guideStyle?.transitionDuration || '0s'),
      }
    })
    expect(arrivalPaint).toMatchObject({
      rowBackground: 'rgba(0, 0, 0, 0)',
      rowShadow: 'none',
      guideAnimation: 'none',
      guideOpacity: '0.78',
      guideWidth: '2px',
    })
    expect(arrivalPaint.maxAnimationDuration).toBeLessThanOrEqual(0.00001)
    expect(arrivalPaint.maxTransitionDuration).toBeLessThanOrEqual(0.00001)
  })

  test('keeps the maximum lens clear of the centered conversation column', async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem('opensquilla.sidebar.width.v1'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await seedLongHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const rail = page.getByTestId('conversation-minimap')
    const markers = page.getByTestId('conversation-minimap-marker')
    await expect(markers).toHaveCount(12, { timeout: 10000 })

    // The 260px sidebar plus 5px resizer leaves a 1105px chat shell here:
    // one pixel inside the rail's visible exit edge and closest to collision.
    await page.setViewportSize({ width: 1370, height: 900 })
    await expect.poll(async () => Math.round(
      (await page.locator('.chat-thread-shell').boundingBox())?.width || 0,
    )).toBe(1105)
    await expect(markers).toHaveCount(12)

    const marker = markers.nth(5)
    await marker.focus()
    await expect(marker).toBeFocused()
    await expect(rail.getByRole('tooltip')).toBeVisible()

    await expect.poll(async () => {
      const [lineBox, messageBox, composerBox, shellBox] = await Promise.all([
        marker.locator('.conversation-minimap__line').boundingBox(),
        page.locator('.msg-ai').first().boundingBox(),
        page.locator('.chat-composer').boundingBox(),
        page.locator('.chat-thread-shell').boundingBox(),
      ])
      if (!lineBox || !messageBox || !composerBox || !shellBox) return null
      const focusRingRight = lineBox.x + lineBox.width + 4
      const center = (box: { x: number; width: number }) => box.x + box.width / 2
      return {
        focusRingHasSafeGap: messageBox.x - focusRingRight >= 8,
        messageComposerCentersStayAligned:
          Math.abs(center(messageBox) - center(composerBox)) <= 5,
        messageShellCentersStayAligned:
          Math.abs(center(messageBox) - center(shellBox)) <= 5,
      }
    }).toEqual({
      // The thread's stable end-side scrollbar gutter accounts for this 5px
      // optical offset; the minimap must not add another asymmetric shift.
      focusRingHasSafeGap: true,
      messageComposerCentersStayAligned: true,
      messageShellCentersStayAligned: true,
    })
  })

  test('stays centered, clears focus when hidden, and respects sidebar hit-area spacing', async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem('opensquilla.sidebar.width.v1'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await seedLongHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const rail = page.getByTestId('conversation-minimap')
    const markers = page.getByTestId('conversation-minimap-marker')
    const resizer = page.getByTestId('sidebar-resizer')
    const shell = page.locator('.chat-thread-shell')
    await expect(markers).toHaveCount(12, { timeout: 10000 })

    await expect.poll(async () => {
      const [railBox, listBox, shellBox, handleBox] = await Promise.all([
        rail.boundingBox(),
        rail.locator('.conversation-minimap__list').boundingBox(),
        shell.boundingBox(),
        resizer.boundingBox(),
      ])
      if (!railBox || !listBox || !shellBox || !handleBox) return null
      return {
        centerDelta: Math.round(Math.abs(
          (listBox.y + listBox.height / 2) - (shellBox.y + shellBox.height / 2),
        )),
        handleGapAtLeast4: railBox.x - (handleBox.x + handleBox.width) >= 4,
      }
    }).toEqual({ centerDelta: 0, handleGapAtLeast4: true })

    await markers.nth(6).click()
    const visibleTurn = page.locator('[data-chat-turn-key="history-user-7"]')
    await expect(visibleTurn).toHaveClass(/is-history-target/, { timeout: 3000 })
    await expect(rail.getByRole('tooltip')).toHaveCount(0)
    const arrivalPaint = await visibleTurn.evaluate((element) => {
      const stack = element.querySelector('.msg-user-stack')
      const rowStyle = getComputedStyle(element)
      const guideStyle = stack ? getComputedStyle(stack, '::after') : null
      return {
        rowAnimation: rowStyle.animationName,
        rowBackground: rowStyle.backgroundColor,
        rowBackgroundImage: rowStyle.backgroundImage,
        rowShadow: rowStyle.boxShadow,
        guideAnimation: guideStyle?.animationName || '',
        guideDisplay: guideStyle?.display || '',
        guideWidth: guideStyle?.width || '',
      }
    })
    expect(arrivalPaint).toMatchObject({
      rowAnimation: 'none',
      rowBackground: 'rgba(0, 0, 0, 0)',
      rowBackgroundImage: 'none',
      rowShadow: 'none',
      guideDisplay: 'block',
      guideWidth: '2px',
    })
    expect(arrivalPaint.guideAnimation).not.toBe('none')
    const initialTurnOffset = await visibleTurn.evaluate((element) => {
      const shell = element.closest('.chat-thread-shell')
      if (!(shell instanceof HTMLElement)) return Number.NaN
      return element.getBoundingClientRect().top - shell.getBoundingClientRect().top
    })

    const anchorHandle = await resizer.boundingBox()
    await page.mouse.move(anchorHandle!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(300, 420, { steps: 8 })
    await page.mouse.up()
    await expect.poll(() => visibleTurn.evaluate((element, initialOffset) => {
      const shell = element.closest('.chat-thread-shell')
      if (!(shell instanceof HTMLElement)) return Number.POSITIVE_INFINITY
      const currentOffset = element.getBoundingClientRect().top - shell.getBoundingClientRect().top
      return Math.round(Math.abs(currentOffset - initialOffset))
    }, initialTurnOffset)).toBeLessThanOrEqual(2)

    await markers.nth(5).focus()
    await expect(markers.nth(5)).toBeFocused()
    await expect(rail.getByRole('tooltip')).toBeVisible()

    const handleBox = await resizer.boundingBox()
    expect(handleBox).not.toBeNull()
    await page.mouse.move(handleBox!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(400, 420, { steps: 12 })
    await page.mouse.up()
    await expect(rail).toHaveCount(0)
    await expect(page.getByRole('tooltip')).toHaveCount(0)

    // 1105–1119px is the hidden middle band after an exit; only crossing the
    // 1120px enter edge should remount the rail.
    const resizedHandle = await resizer.boundingBox()
    await page.mouse.move(resizedHandle!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(340, 420, { steps: 8 })
    await page.mouse.up()
    await expect(rail).toHaveCount(0)

    const middleHandle = await resizer.boundingBox()
    await page.mouse.move(middleHandle!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(300, 420, { steps: 8 })
    await page.mouse.up()
    await expect(markers).toHaveCount(12)
  })
})
