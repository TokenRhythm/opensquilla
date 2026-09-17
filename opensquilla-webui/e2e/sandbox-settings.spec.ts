import { expect, test } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

test('Web Settings opens the Sandbox overview and file-safety details', async ({ page }) => {
  await page.goto('/control/')
  await page.waitForSelector('.conn-pill', { timeout: 10_000 })

  await page.locator('.sidebar-fn-item[data-icon="settings"]').click()
  const settings = page.getByRole('dialog', { name: 'Settings' })
  await expect(settings).toBeVisible()

  const sandboxTab = settings.getByRole('tab', { name: 'Sandbox', exact: true })
  await expect(sandboxTab).toBeVisible()
  await sandboxTab.click()
  await expect(page).toHaveURL(/\/settings\/sandbox$/)

  await expect(page.getByTestId('sandbox-overview')).toBeVisible()
  await expect(page.getByTestId('sandbox-safe-mode')).toBeVisible()
  await expect(page.getByTestId('sandbox-full-mode')).toBeVisible()
  await expect(page.locator('.sandbox-settings__status')).toBeVisible()

  await page.getByTestId('sandbox-open-files').click()
  await expect(page.getByTestId('sandbox-detail')).toBeVisible()
  await expect(page.getByTestId('builtin-file-rules')).toBeVisible()
  await expect(page.getByTestId('sandbox-backup-quota')).toBeVisible()

  await page.getByTestId('sandbox-detail-back').click()
  await expect(page.getByTestId('sandbox-overview')).toBeVisible()
})

test('Sandbox settings stay within the panel at medium window widths', async ({ page }) => {
  await page.setViewportSize({ width: 1078, height: 880 })
  await page.goto('/control/settings/sandbox')
  await page.waitForSelector('.conn-pill', { timeout: 10_000 })

  const settings = page.getByRole('dialog', { name: 'Settings' })
  await expect(settings).toBeVisible()
  await expect(page.getByTestId('sandbox-overview')).toBeVisible()

  for (const width of [1078, 858, 769, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 880 })
    const layout = await page.locator('.settings-panel').evaluate((panel) => {
      const modePicker = panel.querySelector<HTMLElement>('.sandbox-mode-picker')
      const segmented = panel.querySelector<HTMLElement>('.sandbox-segmented')
      const panelRect = panel.getBoundingClientRect()
      const modeRect = modePicker?.getBoundingClientRect()
      const segmentedRect = segmented?.getBoundingClientRect()
      const overflowing = Array.from(panel.querySelectorAll<HTMLElement>('*'))
        .filter((element) => element.getBoundingClientRect().right > panelRect.right + 1)
        .slice(0, 8)
        .map((element) => ({
          className: element.className,
          right: element.getBoundingClientRect().right,
          scrollWidth: element.scrollWidth,
          clientWidth: element.clientWidth,
        }))
      return {
        panelClientWidth: panel.clientWidth,
        panelScrollWidth: panel.scrollWidth,
        modeLeft: modeRect?.left ?? 0,
        modeRight: modeRect?.right ?? 0,
        segmentedLeft: segmentedRect?.left ?? 0,
        segmentedRight: segmentedRect?.right ?? 0,
        panelLeft: panelRect.left,
        panelRight: panelRect.right,
        overflowing,
      }
    })
    expect(
      layout.panelScrollWidth,
      `panel overflow at ${width}px: ${JSON.stringify(layout.overflowing)}`,
    )
      .toBeLessThanOrEqual(layout.panelClientWidth + 1)
    expect(layout.modeLeft, `mode left edge at ${width}px`)
      .toBeGreaterThanOrEqual(layout.panelLeft - 1)
    expect(layout.modeRight, `mode right edge at ${width}px`)
      .toBeLessThanOrEqual(layout.panelRight + 1)
    expect(layout.segmentedLeft, `segmented left edge at ${width}px`)
      .toBeGreaterThanOrEqual(layout.modeLeft - 1)
    expect(layout.segmentedRight, `segmented right edge at ${width}px`)
      .toBeLessThanOrEqual(layout.modeRight + 1)
    await expect(page.getByTestId('sandbox-safe-mode')).toBeVisible()
    await expect(page.getByTestId('sandbox-full-mode')).toBeVisible()
  }
})
