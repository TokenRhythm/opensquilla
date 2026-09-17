import { test, expect } from '@playwright/test'
import {
  expectTopbarConsoleClean,
  expectTopbarGeometry,
  openTopbarSession,
  TOPBAR_GEOMETRY_VIEWPORTS,
  TOPBAR_SESSION_KEY,
} from './support/topbar-fixture'

test.afterEach(({ page }) => {
  expectTopbarConsoleClean(page)
})

const VIEWPORTS = TOPBAR_GEOMETRY_VIEWPORTS.filter(viewport => viewport.width < 960)
const WIDE_VIEWPORTS = TOPBAR_GEOMETRY_VIEWPORTS.filter(viewport => viewport.width >= 960)

test.describe('Responsive chat header actions', () => {
  test('320px prioritizes approval attention without covering session actions', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-approval`,
      locale: 'zh-Hans',
      deliverableCount: 1,
      approvalCount: 1,
      bgm: { enabled: true, playing: false },
    })

    const systemStatus = page.getByTestId('chat-system-status')
    await expect(systemStatus).toBeVisible({ timeout: 10000 })
    await expect(systemStatus).toHaveAttribute('data-layout', 'tight')
    const systemTrigger = page.getByTestId('chat-system-status-trigger')
    await expect(systemTrigger).toBeVisible()
    await expect(systemTrigger).toHaveClass(/conn-pill/)
    await systemTrigger.click()
    await expect(page.getByTestId('chat-system-approval')).toBeVisible()
    await expect(page.getByTestId('chat-system-connection')).toBeVisible()
    await expect(page.getByTestId('bgm-toggle')).toBeHidden()
    await expect(page.getByTestId('chat-session-actions-trigger')).toBeVisible()
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  for (const viewport of VIEWPORTS) {
    test(`${viewport.width}px keeps crowded global and chat controls usable`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await openTopbarSession(page, {
        sessionKey: `${TOPBAR_SESSION_KEY}-${viewport.width}`,
        locale: 'zh-Hans',
        deliverableCount: 1,
        bgm: { enabled: true, playing: false },
      })

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
      await expectTopbarGeometry(page, {
        minimumTargetSize: viewport.width <= 768 ? 44 : undefined,
      })

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
      await expect(page.getByTestId('chat-session-action-runs')).toHaveCount(0)
      await expect(page.getByTestId('chat-session-action-share')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-share')).toHaveAccessibleName('分享')
      await expect(page.getByTestId('chat-session-action-copy')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-copy'))
        .toHaveAccessibleName('复制会话 ID')
      await expectTopbarGeometry(page, {
        minimumTargetSize: viewport.width <= 768 ? 44 : undefined,
      })

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
      await openTopbarSession(page, {
        sessionKey: `${TOPBAR_SESSION_KEY}-${viewport.width}`,
        locale: 'zh-Hans',
        deliverableCount: 1,
        bgm: { enabled: true, playing: false },
      })

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
      await expect(page.getByTestId('chat-session-action-runs')).toHaveCount(0)
      await expect(page.getByTestId('chat-session-action-share')).toBeVisible()
      await expect(page.getByTestId('chat-session-action-share')).toHaveAccessibleName('分享')
      await expect(page.getByTestId('chat-session-actions-trigger')).toHaveCount(0)

      const identityGap = await page.locator('.chat-header').evaluate(element => {
        const title = element.querySelector<HTMLElement>('.chat-header__title')
        const copy = element.querySelector<HTMLElement>('.chat-header__copy')
        if (!title || !copy) return Number.POSITIVE_INFINITY
        return copy.getBoundingClientRect().left - title.getBoundingClientRect().right
      })
      expect(identityGap).toBeGreaterThanOrEqual(0)
      expect(identityGap).toBeLessThanOrEqual(8)
      await expectTopbarGeometry(page)
    })
  }
})
