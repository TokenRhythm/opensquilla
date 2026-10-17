import { test, expect } from '@playwright/test'
import { relativeLuminance } from './utils'

const CONTROL_URL = '/control/'

async function chatBackground(page: import('@playwright/test').Page): Promise<string> {
  return page.evaluate(() => {
    const chat = document.querySelector('.chat')
    if (!chat) throw new Error('.chat surface not found')
    return getComputedStyle(chat).backgroundColor
  })
}

async function setTheme(page: import('@playwright/test').Page, theme: 'dark' | 'light') {
  await page.evaluate(value => {
    document.documentElement.setAttribute('data-theme', value)
    localStorage.setItem('opensquilla-theme', value)
  }, theme)
  await page.waitForTimeout(300)
}

test.describe('Chat Theme', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
    await page.waitForSelector('.chat', { timeout: 10000 })
  })

  test('chat surface is dark in dark theme', async ({ page }) => {
    await setTheme(page, 'dark')

    const background = await chatBackground(page)
    expect(relativeLuminance(background)).toBeLessThan(0.3)
  })

  test('chat surface stays light in light theme', async ({ page }) => {
    await setTheme(page, 'light')

    const background = await chatBackground(page)
    expect(relativeLuminance(background)).toBeGreaterThan(0.7)
  })
})
