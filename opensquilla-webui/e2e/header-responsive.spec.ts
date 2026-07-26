import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eheaderresponsive'

const VIEWPORTS = [
  { width: 320, height: 720 },
  { width: 375, height: 812 },
  { width: 480, height: 800 },
  { width: 700, height: 900 },
  { width: 768, height: 900 },
  { width: 769, height: 900 },
] as const

const WIDE_VIEWPORTS = [
  { width: 960, height: 900 },
  { width: 1440, height: 1000 },
] as const

type GeometryProbe = {
  controls: Array<{
    label: string
    left: number
    right: number
    top: number
    bottom: number
  }>
  outsideViewport: string[]
  overlaps: string[]
  missedCenters: string[]
}

async function installCrowdedHeaderPreferences(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'zh-Hans')
    localStorage.setItem('opensquilla-bgm', JSON.stringify({
      enabled: true,
      playing: false,
      trackId: 'stream',
      volume: 0.5,
    }))
  })
}

// Seed an artifact-bearing settled turn through the real WebSocket pipeline.
// Artifact + zh-Hans + enabled BGM is the crowded combination that regressed.
async function seedArtifactHistory(page: Page) {
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
          const now = Math.floor(Date.now() / 1000)
          frame.payload = {
            messages: [
              {
                role: 'user',
                text: 'Generate a compact status report.',
                id: 'msg-header-user',
                timestamp: now - 120,
              },
              {
                role: 'assistant',
                text: 'The status report is ready.',
                id: 'msg-header-assistant',
                timestamp: now - 60,
                artifacts: [
                  {
                    id: 'artifact-header-report',
                    name: 'status-report.csv',
                    mime: 'text/csv',
                    size: 2048,
                  },
                ],
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

async function openCrowdedHeaderSession(page: Page, width: number) {
  await installCrowdedHeaderPreferences(page)
  await seedArtifactHistory(page)
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(`${SESSION_KEY}-${width}`)}`)

  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-Hans')
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 10000 })
  await expect(page.locator('.msg-ai-main').last()).toBeVisible({ timeout: 10000 })
  await expect(page.locator('.chat-header')).toBeVisible({ timeout: 10000 })
  await expect(page.getByTestId('bgm-toggle')).toBeVisible()
}

async function probeGeometry(page: Page, selector: string): Promise<GeometryProbe> {
  return page.evaluate((controlSelector) => {
    const isVisible = (element: HTMLElement) => {
      const style = getComputedStyle(element)
      const box = element.getBoundingClientRect()
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && Number(style.opacity) > 0
        && box.width > 0
        && box.height > 0
    }
    const labelFor = (element: HTMLElement, index: number) =>
      element.getAttribute('aria-label')
      || element.getAttribute('title')
      || element.textContent?.trim()
      || `${element.tagName.toLowerCase()}[${index}]`

    const elements = Array.from(
      new Set(document.querySelectorAll<HTMLElement>(controlSelector)),
    ).filter(isVisible)
    const controls = elements.map((element, index) => {
      const box = element.getBoundingClientRect()
      return {
        element,
        composite: element.closest('.bgm-menu-wrap'),
        label: labelFor(element, index),
        left: box.left,
        right: box.right,
        top: box.top,
        bottom: box.bottom,
      }
    })
    const outsideViewport = controls
      .filter(control => control.left < -0.5
        || control.right > window.innerWidth + 0.5
        || control.top < -0.5
        || control.bottom > window.innerHeight + 0.5)
      .map(control => control.label)
    const overlaps: string[] = []
    for (let leftIndex = 0; leftIndex < controls.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < controls.length; rightIndex += 1) {
        const left = controls[leftIndex]
        const right = controls[rightIndex]
        // BGM is intentionally a split button: toggle and picker share an
        // overlapping border while remaining separate hit targets.
        if (left.composite && left.composite === right.composite) continue
        const overlapWidth = Math.min(left.right, right.right) - Math.max(left.left, right.left)
        const overlapHeight = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top)
        if (overlapWidth > 0.5 && overlapHeight > 0.5) {
          overlaps.push(`${left.label} <> ${right.label}`)
        }
      }
    }
    const missedCenters = controls.flatMap(control => {
      const x = (control.left + control.right) / 2
      const y = (control.top + control.bottom) / 2
      const hit = document.elementFromPoint(x, y)
      return hit && (hit === control.element || control.element.contains(hit))
        ? []
        : [control.label]
    })

    return {
      controls: controls.map(({ element: _element, composite: _composite, ...control }) => control),
      outsideViewport,
      overlaps,
      missedCenters,
    }
  }, selector)
}

async function expectGeometryIsUsable(page: Page, selector: string) {
  const probe = await probeGeometry(page, selector)
  expect(probe.controls.length).toBeGreaterThan(0)
  expect(probe.outsideViewport, JSON.stringify(probe.controls, null, 2)).toEqual([])
  expect(probe.overlaps, JSON.stringify(probe.controls, null, 2)).toEqual([])
  expect(probe.missedCenters, JSON.stringify(probe.controls, null, 2)).toEqual([])
}

test.describe('Responsive chat header actions', () => {
  test('320px prioritizes approval attention without covering session actions', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 })
    await installCrowdedHeaderPreferences(page)
    await seedArtifactHistory(page)
    await page.route('**/api/approvals', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        pending: [{
          id: 'approval-header-responsive',
          sessionKey: `${SESSION_KEY}-approval`,
          toolName: 'shell',
          command: 'synthetic command',
        }],
      }),
    }))
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(`${SESSION_KEY}-approval`)}`)

    await expect(page.locator('.approval-inline')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.conn-pill')).toBeHidden()
    await expect(page.getByTestId('bgm-toggle')).toBeHidden()
    await expect(page.getByTestId('chat-session-actions-trigger')).toBeVisible()
    await expectGeometryIsUsable(
      page,
      '.topbar button, .topbar a, .chat-header button, .chat-header a',
    )
  })

  for (const viewport of VIEWPORTS) {
    test(`${viewport.width}px keeps crowded global and chat controls usable`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await openCrowdedHeaderSession(page, viewport.width)

      const header = page.locator('.chat-header')
      await expect(page.getByTestId('route-header-host').locator('.chat-header')).toHaveCount(1)
      const layout = await header.getAttribute('data-layout')
      expect(layout).toMatch(/^(wide|compact|tight)$/)
      if (viewport.width <= 768) expect(layout).not.toBe('wide')

      // Compact has room for a contextual primary action; tight intentionally
      // moves everything into the menu. The test follows the container's
      // published state rather than guessing it solely from viewport width.
      const primary = page.getByTestId('chat-header-primary-action')
      if (layout === 'compact') {
        await expect(primary).toBeVisible()
        await expect(primary).toHaveAccessibleName('产物（1）')
      } else if (layout === 'tight') {
        await expect(primary).toHaveCount(0)
      }

      const menuTrigger = page.getByTestId('chat-session-actions-trigger')
      await expect(menuTrigger).toBeVisible()
      await expect(menuTrigger).toHaveAccessibleName('会话操作')
      await expect(menuTrigger).toHaveAttribute('aria-haspopup', 'menu')
      await expect(menuTrigger).toHaveAttribute('aria-expanded', 'false')

      // Check every visible global/chat header control together. This catches
      // cross-owner collisions, not only overlap within either action group.
      await expectGeometryIsUsable(
        page,
        '.topbar button, .topbar a, .chat-header button, .chat-header a',
      )

      await menuTrigger.click()
      const menu = page.getByTestId('chat-session-actions-menu')
      await expect(menu).toBeVisible()
      await expect(menuTrigger).toHaveAttribute('aria-expanded', 'true')

      // Every non-primary session operation stays reachable in the localized
      // menu. In tight mode the deliverable moves there as well.
      const menuDeliverables = page.getByTestId('chat-session-action-deliverables')
      if (layout === 'tight') {
        await expect(menuDeliverables).toBeVisible()
        await expect(menuDeliverables).toHaveAccessibleName('产物（1）')
      }
      await expect(page.getByTestId('chat-session-action-runs')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-runs')).toHaveAccessibleName(/运行/)
      await expect(page.getByTestId('chat-session-action-share')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-share')).toHaveAccessibleName('分享')
      await expect(page.getByTestId('chat-session-action-copy')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-copy'))
        .toHaveAccessibleName('复制会话密钥')
      await expectGeometryIsUsable(
        page,
        '.topbar button, .topbar a, .chat-header button, .chat-header a, '
          + '[data-testid="chat-session-actions-menu"] button',
      )

      // Exercise a real menu command, not just its rendering contract.
      await page.getByTestId('chat-session-action-share').click()
      await expect(menu).toHaveCount(0)
      await expect(page.getByTestId('share-banner')).toBeVisible()

      await page.getByTestId('share-banner').getByRole('button', { name: '取消' }).click()
      await expect(page.getByTestId('share-banner')).toHaveCount(0)
      await expect(menuTrigger).toBeFocused()
    })
  }

  for (const viewport of WIDE_VIEWPORTS) {
    test(`${viewport.width}px exposes direct actions when the content pane is wide`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await openCrowdedHeaderSession(page, viewport.width)

      // Make the content pane itself wide at both viewport sizes. Layout is
      // container-driven, so viewport width alone is not the contract.
      await page.getByTestId('sidebar-toggle-expanded').click()
      await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeVisible()

      const header = page.locator('.chat-header')
      await expect(header).toHaveAttribute('data-layout', 'wide')
      await expect(page.getByTestId('route-header-host').locator('.chat-header')).toHaveCount(1)

      await expect(page.getByTestId('chat-session-action-deliverables')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-deliverables'))
        .toHaveAccessibleName('产物（1）')
      await expect(page.getByTestId('chat-session-action-runs')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-runs')).toHaveAccessibleName(/运行/)
      await expect(page.getByTestId('chat-session-action-share')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-share')).toHaveAccessibleName('分享')
      await expect(page.getByTestId('chat-session-actions-trigger')).toHaveCount(0)

      await expectGeometryIsUsable(
        page,
        '.topbar button, .topbar a, .chat-header button, .chat-header a',
      )
    })
  }
})
