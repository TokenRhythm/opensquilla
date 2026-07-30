import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const NAME = 'e2e-edit-telegram'

// End-to-end: the /channels workspace owns channel setup. Covers the compose
// takeover (?compose=1), the legacy settings-hash redirect, in-place edit via
// ?channel=<name>&tab=configuration&edit=1, the no-'***' invariant, the
// stored-secret keep-current save merge, and cleanup.
//
// NOTE: updated for the workspace flow by inspection; like every spec here it
// needs a live gateway (`opensquilla gateway run`) to execute.

async function openChannels(page: Page, suffix = ''): Promise<void> {
  await page.goto(`${CONTROL_URL}channels${suffix}`)
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 15000 })
}

test.describe.serial('channel workspace compose + edit', () => {
  test('compose a telegram channel from the takeover', async ({ page }) => {
    await openChannels(page, '?compose=1')
    const surface = page.locator('.chc')
    await expect(surface).toBeVisible({ timeout: 10000 })

    // Pick from the type gallery; the gallery collapses to a receipt chip
    // and the shared config editor grows beneath it in compose mode.
    await surface.locator('button[data-channel-type="telegram"]').click()
    await expect(surface.locator('.chc__chipname')).toHaveText('Telegram')
    await surface.locator('input[name="setup_channel_name"]').fill(NAME)
    await surface.locator('input[name="setup_channel_token"]').fill('0000:e2e-dummy-token')
    await surface.getByRole('button', { name: 'Save Channel' }).click()

    // Save dismisses the takeover and selects the new channel's page.
    await expect(surface).toHaveCount(0, { timeout: 15000 })
    await expect(page).toHaveURL(new RegExp(`channel=${NAME}`), { timeout: 15000 })
    await expect(page.locator('.chd h2')).toHaveText(NAME)
  })

  test('legacy settings hash redirects into the in-place editor', async ({ page }) => {
    await page.goto(`${CONTROL_URL}settings/channels#channel-${NAME}`)
    await expect(page).toHaveURL(new RegExp(`channels\\?.*channel=${NAME}`), { timeout: 15000 })
    await expect(page).toHaveURL(/tab=configuration/)
    await expect(page).toHaveURL(/edit=1/)
    await expect(page.locator('.cfge')).toBeVisible({ timeout: 15000 })
  })

  test('legacy bare and query settings paths redirect to the workspace', async ({ page }) => {
    await page.goto(`${CONTROL_URL}settings/channels`)
    await expect(page).toHaveURL(/\/channels$/, { timeout: 15000 })

    await page.goto(`${CONTROL_URL}settings/channels?compose=1&type=telegram`)
    await expect(page).toHaveURL(/\/channels\?compose=1&type=telegram$/, { timeout: 15000 })
    await expect(page.locator('.chc')).toBeVisible({ timeout: 15000 })
  })

  test('edit in place: masked secret, draft test, keep-current save', async ({ page }) => {
    await openChannels(page, `?channel=${NAME}&tab=configuration&edit=1`)
    const editor = page.locator('.cfge')
    await expect(editor).toBeVisible({ timeout: 15000 })

    // Name is locked text (not an input); the stored secret renders as a
    // masked row with Replace; no input anywhere holds the '***' sentinel.
    await expect(editor.locator('[data-field="name"] input')).toHaveCount(0)
    await expect(editor.locator('[data-field="name"] .cfge__value--locked')).toBeVisible()
    const tokenRow = editor.locator('[data-field="token"]')
    await expect(tokenRow).toContainText('Stored')
    await expect(tokenRow.getByRole('button', { name: 'Replace' })).toBeVisible()
    for (const value of await editor.locator('input').evaluateAll(
      els => els.map(el => (el as HTMLInputElement).value),
    )) {
      expect(value).not.toBe('***')
    }

    // Dirty the draft: the sticky bar names the changed field and offers
    // Test/Discard/Save. The draft probe merges blank secrets against the
    // stored entry server-side — no retyping. default_chat_id folded into the
    // Advanced disclosure, so expand it first.
    await editor.locator('.cfge__advanced > summary').click()
    await editor.locator('input[name="setup_channel_default_chat_id"]').fill('4242')
    const bar = page.locator('.ceb')
    await expect(bar).toBeVisible()
    await expect(bar).toContainText('Default chat id')
    await bar.getByRole('button', { name: 'Test connection' }).click()
    await expect(editor.locator('.cfge__transcript-row')).toBeVisible({ timeout: 20000 })

    // Save: probe → upsert, baseline reset, back to read mode; the secret
    // stays masked and the change persisted (expand Advanced to read it).
    await bar.getByRole('button', { name: 'Save changes' }).click()
    await expect(bar).toHaveCount(0, { timeout: 15000 })
    await editor.locator('.cfge__advanced > summary').click()
    await expect(editor.locator('[data-field="default_chat_id"]')).toContainText('4242', { timeout: 15000 })
    await expect(editor.locator('[data-field="token"]')).toContainText('Stored')
    await expect(page).not.toHaveURL(/edit=1/)
  })

  test('cleanup: remove the e2e channel', async ({ page }) => {
    await openChannels(page, `?channel=${NAME}`)
    const detail = page.locator('.chd')
    if (await detail.count()) {
      await detail.locator('.chd__remove').click()
      await page
        .getByRole('dialog', { name: 'Remove this channel?' })
        .getByRole('button', { name: 'Remove' })
        .click()
      await expect(page.locator('.chb-ledger').getByText(NAME)).toHaveCount(0, { timeout: 15000 })
    }
  })
})
