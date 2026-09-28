import { test, expect } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'

for (const configured of [true, false]) {
  test(`keeps readiness inside Settings with primary configured=${configured}`, async ({ page }) => {
    await installSidebarFixture(page, {
      'config.get': { llm: { provider: 'tokenrhythm', model: 'test-model' } },
      'config.effective': { fields: { 'llm.provider': { source: 'config', value: 'tokenrhythm' } } },
      'onboarding.catalog': {
        providers: [{
          providerId: 'tokenrhythm', label: 'TokenRhythm', runtimeSupported: true,
          requiresApiKey: true, fields: [{ name: 'model', label: 'Model' }],
        }],
      },
      'onboarding.status': {
        hasConfig: true, llmConfigured: configured, needsOnboarding: !configured,
        llmSource: configured ? 'explicit' : 'missing_env',
        sectionDetails: {
          llm: { status: configured ? 'ok' : 'missing', blocking: !configured, actionRequired: !configured },
          search: { status: 'missing', required: false, optional: true, blocking: false, actionRequired: true },
        },
      },
    })
    await page.goto('/control/')
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.getByRole('complementary', { name: 'Gateway setup needed' })).toHaveCount(0)
    await expect(page.locator('.sidebar-setup-banner')).toHaveCount(0)

    await page.locator('.sidebar-foot button').click()
    const provider = page.locator('#settings-rail-provider')
    await expect(provider).toHaveAccessibleName(configured ? /^Model Service: Ready/ : /^Model Service: Needs action/)
    await expect(provider.locator('.settings-rail__dot.is-danger')).toHaveCount(configured ? 0 : 1)
    await expect(provider.locator('.settings-rail__warn')).toHaveCount(0)
    await expect(page.locator('.settings-rail__warn')).toHaveCount(0)
    await expect(page.locator('#settings-rail-capabilities')).toHaveAccessibleName('Capabilities: Optional')
    await expect(page.locator('.settings-rail__dot')).toHaveCount(configured ? 1 : 2)
    await expect(page.locator('#settings-rail-gateway .settings-rail__dot')).toHaveCount(1)

    await page.getByRole('button', { name: 'Close', exact: true }).click()
    await page.locator('.sidebar-foot button').click()
    await expect(provider).toHaveAccessibleName(configured ? /^Model Service: Ready/ : /^Model Service: Needs action/)
    await expect(provider.locator('.settings-rail__dot.is-danger')).toHaveCount(configured ? 0 : 1)
  })
}
