import { expect, test } from '@playwright/test'
import {
  expectTopbarConsoleClean,
  expectTopbarGeometry,
  openTopbarSession,
  TOPBAR_SESSION_KEY,
  type TopbarScenario,
} from './support/topbar-fixture'

test.afterEach(({ page }) => {
  expectTopbarConsoleClean(page)
})

type VisualScenario = {
  name: string
  viewport: { width: number; height: number }
  fixture: TopbarScenario
}

const VISUAL_SCENARIOS: VisualScenario[] = [
  {
    name: 'topbar-wide-connected-light',
    viewport: { width: 1440, height: 1000 },
    fixture: {
      locale: 'en',
      theme: 'light',
      deliverableCount: 1,
    },
  },
  {
    name: 'topbar-compact-approval-dark-de',
    viewport: { width: 959, height: 900 },
    fixture: {
      locale: 'de',
      theme: 'dark',
      deliverableCount: 7,
      approvalCount: 1,
    },
  },
  {
    name: 'topbar-compact-synthwave-zh',
    viewport: { width: 480, height: 800 },
    fixture: {
      locale: 'zh-Hans',
      theme: 'synthwave',
      deliverableCount: 99,
    },
  },
  {
    name: 'topbar-tight-all-pressure-ember-de',
    viewport: { width: 375, height: 812 },
    fixture: {
      locale: 'de',
      theme: 'ember',
      deliverableCount: 120,
      approvalCount: 120,
      update: {
        status: 'error',
        latestVersion: '2.0.0',
        errorCode: 'download_failed',
        error: 'synthetic update error',
      },
    },
  },
]

test.describe('Chat topbar visual regression', () => {
  for (const scenario of VISUAL_SCENARIOS) {
    test(scenario.name, async ({ page }) => {
      await page.setViewportSize(scenario.viewport)
      await openTopbarSession(page, {
        sessionKey: `${TOPBAR_SESSION_KEY}-visual-${scenario.name}`,
        ...scenario.fixture,
      })
      await expectTopbarGeometry(page, {
        minimumTargetSize: scenario.viewport.width <= 768 ? 44 : undefined,
      })

      if ((scenario.fixture.approvalCount ?? 0) > 99) {
        const badge = page.getByTestId('chat-system-status-badge')
        await expect(badge).toHaveText('99+')
        const bounds = await badge.evaluate(element => {
          const box = element.getBoundingClientRect()
          return { left: box.left, right: box.right }
        })
        expect(bounds.left).toBeGreaterThanOrEqual(0)
        expect(bounds.right).toBeLessThanOrEqual(scenario.viewport.width)
      }

      // The topbar is the regression boundary. Clipping to the component keeps
      // session-body typography and platform scrollbar rendering out of these
      // four deliberately local baselines.
      await expect(page.locator('.topbar')).toHaveScreenshot(`${scenario.name}.png`, {
        animations: 'disabled',
        caret: 'hide',
        scale: 'css',
        maxDiffPixelRatio: 0.002,
      })
    })
  }
})
