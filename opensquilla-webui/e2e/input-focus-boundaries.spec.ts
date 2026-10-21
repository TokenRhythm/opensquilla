import { expect, test, type Locator, type Page } from '@playwright/test'
import { installSidebarFixture, SIDEBAR_ACTIVE_SESSION_KEY } from './support/sidebar-fixture'

// Exercise shipped App/components and CSS, with no real provider settings or
// credentials. In particular this catches CSS order changes in a built app.
async function openChat(page: Page, theme: string) {
  await page.addInitScript(value => localStorage.setItem('opensquilla-theme', value), theme)
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await installSidebarFixture(page, {
    'config.get': { llm: { provider: 'custom', model: 'focus-model',
      base_url: 'https://focus.example.invalid/v1', api_key_env: 'SYNTHETIC_FOCUS_KEY' },
    permissions: {}, skills: {} },
    'config.effective': { fields: { 'llm.provider': { value: 'custom', source: 'config' } } },
    'onboarding.catalog': { providers: [{ providerId: 'custom', label: 'Focus provider',
      runtimeSupported: true, requiresBaseUrl: true, requiresApiKey: true, acceptsApiKey: true,
      fields: [{ name: 'model', label: 'Model', required: true },
        { name: 'base_url', label: 'Base URL', required: true }] }] },
    'onboarding.status': { hasConfig: true, llmConfigured: true,
      llmCredentialStatus: { provider: 'custom', available: true, source: 'env', envKey: 'SYNTHETIC_FOCUS_KEY' } },
    'models.list': { models: [], errors: [] },
  })
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`/control/chat?session=${SIDEBAR_ACTIVE_SESSION_KEY}`)
  await expect(page.locator('#app-main .chat')).toContainText('The example tasks are ready.')
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
}

async function openFind(page: Page) {
  await page.getByTestId('chat-session-actions-trigger').click()
  await page.getByTestId('chat-session-action-find').click()
  const input = page.locator('.chat-find-bar__input')
  await expect(input).toBeFocused()
  return input
}

async function expectBareInput(input: Locator) {
  await expect(input).toHaveCSS('box-shadow', 'none')
  await expect(input).toHaveCSS('outline-style', 'none')
  await expect(input).toHaveCSS('border-top-width', '0px')
  await expect(input).toHaveCSS('border-radius', '0px')
  await expect(input).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  const fits = await input.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const parent = element.parentElement!.getBoundingClientRect()
    return rect.top >= parent.top && rect.bottom <= parent.bottom
  })
  expect(fits).toBe(true)
}

async function expectStandaloneFocus(input: Locator) {
  await input.click()
  await expect(input).toBeFocused()
  await expect(input).toHaveCSS('outline-style', 'solid')
  await expect(input).toHaveCSS('outline-width', '1px')
  await expect(input).toHaveCSS('outline-offset', '0px')
  await expect(input).toHaveCSS('box-shadow', 'none')
}

for (const theme of ['dark', 'light']) {
  test(`${theme}: real conversation and Settings inputs have one focus boundary`, async ({ page }, info) => {
    await openChat(page, theme)
    const find = await openFind(page)
    await expectBareInput(find)
    const wrapper = page.locator('.chat-find-bar')
    await expect(wrapper).toHaveCSS('border-top-width', '1px')
    const focusedBorder = await wrapper.evaluate(element => getComputedStyle(element).borderColor)
    const focusedBox = await wrapper.boundingBox()
    await page.locator('.chat-header__identity').click()
    await expect.poll(() => wrapper.evaluate(element => getComputedStyle(element).borderColor)).not.toBe(focusedBorder)
    await find.click()
    await expect(wrapper).toHaveCSS('border-color', focusedBorder)
    expect(await wrapper.boundingBox()).toEqual(focusedBox)
    await expectBareInput(find)
    await wrapper.screenshot({ path: info.outputPath('conversation-find.png') })
    await find.press('Escape')

    // The global command palette is another compound input, mounted through
    // its real keyboard entry rather than a hand-built CSS fixture.
    await page.keyboard.press('Control+k')
    const paletteInput = page.locator('.cmdp-search__input')
    await expect(paletteInput).toBeFocused()
    await expectBareInput(paletteInput)
    await paletteInput.press('Escape')

    await page.locator('.sidebar-foot button[data-icon="settings"]').click()
    const search = page.locator('.settings-search input')
    await expectStandaloneFocus(search)
    await search.screenshot({ path: info.outputPath('settings-search.png') })

    await page.locator('[data-provider-id="custom"] .setup-provider-card__select').click()
    const endpoint = page.getByTestId('setup-provider-modal-endpoint').locator('input[name="setup_provider_base_url"]')
    await expect(endpoint).toHaveValue('https://focus.example.invalid/v1')
    await expectStandaloneFocus(endpoint)
    await endpoint.screenshot({ path: info.outputPath('provider-endpoint.png') })
    // Keyboard focus is preserved, not merely hidden to improve a screenshot.
    await endpoint.press('Shift+Tab')
    await page.keyboard.press('Tab')
    await expect(endpoint).toBeFocused()
    await expect(endpoint).toHaveCSS('outline-width', '1px')
  })
}

test('forced colors preserves the system focus outline on compound and standalone fields', async ({ page }) => {
  await page.emulateMedia({ forcedColors: 'active' })
  await openChat(page, 'dark')
  const find = await openFind(page)
  await expect(find).toHaveCSS('outline-style', 'solid')
  await expect(find).toHaveCSS('outline-width', '2px')
  await expect(find).toHaveCSS('outline-offset', '2px')
  await find.press('Escape')
  await page.locator('.sidebar-foot button[data-icon="settings"]').click()
  const search = page.locator('.settings-search input')
  await search.click()
  await expect(search).toHaveCSS('outline-style', 'solid')
  await expect(search).toHaveCSS('outline-width', '2px')
  await expect(search).toHaveCSS('outline-offset', '2px')
})
