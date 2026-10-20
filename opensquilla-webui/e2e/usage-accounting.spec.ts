import { expect, test, type Locator } from '@playwright/test'
import { readFile } from 'node:fs/promises'

async function expectAlignedDetails(details: Locator) {
  const columns = ['share', 'tokens', 'cost', 'source'] as const
  const geometry = await details.locator('.usage-expand__row').evaluateAll((rows, names) => rows.map(row => {
    const bounds = row.getBoundingClientRect()
    const cells = names.map(name => {
      const cell = row.querySelector<HTMLElement>(`.usage-expand__${name}`)!
      const rect = cell.getBoundingClientRect()
      return { x: rect.x, top: rect.top, bottom: rect.bottom,
        overflow: cell.scrollWidth - cell.clientWidth }
    })
    const percentage = row.querySelector('.usage-expand__share-pct')!
    const range = document.createRange()
    range.selectNodeContents(percentage)
    return { cells, top: bounds.top, bottom: bounds.bottom, percentageRight: range.getBoundingClientRect().right }
  }), columns)
  expect(geometry).toHaveLength(4)
  for (const [index, name] of columns.entries()) {
    const positions = geometry.map(row => row.cells[index].x)
    expect(Math.max(...positions) - Math.min(...positions), `${name} columns align across every model`).toBeLessThanOrEqual(1)
  }
  const percentageEdges = geometry.map(row => row.percentageRight)
  expect(Math.max(...percentageEdges) - Math.min(...percentageEdges), 'unknown dash and percentages share their right edge').toBeLessThanOrEqual(1)
  for (const row of geometry) {
    for (const cell of row.cells) {
      expect(cell.overflow, 'wrapped content stays within its column').toBeLessThanOrEqual(1)
      expect(cell.top).toBeGreaterThanOrEqual(row.top - 1)
      expect(cell.bottom).toBeLessThanOrEqual(row.bottom + 1)
    }
  }
}

test('existing Usage table rolls descendant calls into one parent without duplicate totals or CSV rows', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.routeWebSocket(/.*/, () => {})
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 1473, height: 1000 })
  await page.goto('/e2e/fixtures/usage-accounting.html')
  await expect(page.locator('.usage-chart')).toHaveCount(1)
  await expect(page.locator('.usage-models')).toHaveCount(1)
  await expect(page.locator('.usage-sessions')).toHaveCount(1)
  await expect(page.locator('.usage-task-group')).toHaveCount(0)
  await expect(page.getByTestId('usage-capabilities')).toHaveCount(0)
  await expect(page.getByText('请勿与父任务费用再次相加', { exact: false })).toHaveCount(0)
  const summary = page.locator('#usage-metrics')
  const costCard = summary.locator('.control-stat').nth(1)
  await expect(costCard.locator(':scope > *')).toHaveCount(2)
  await expect(costCard.locator('.control-stat__label')).toHaveText('总成本')
  await expect(costCard.locator('.control-stat__hint')).toHaveCount(0)
  await expect(summary.locator('.control-stat__value').nth(1)).toHaveText('$0.4407')
  await expect(summary.locator('.control-stat__value').nth(2)).toHaveText('2')
  await expect(summary.locator('.control-stat__value').nth(3)).toHaveText('$0.2204')
  await expect(page.locator('.usage-model-card .usage-source')).toHaveCount(0)
  await expect(page.locator('.usage-expand')).toHaveCount(0)

  const sessions = page.locator('.usage-sessions')
  const rows = sessions.locator('tbody > tr:not(.usage-expand-row)')
  await expect(rows).toHaveCount(2)
  await expect(sessions.getByRole('columnheader', { name: '来源', exact: true })).toHaveCount(0)
  await expect(rows.locator('[data-label="Source"]')).toHaveCount(0)
  await expect(rows.locator('.usage-source')).toHaveCount(0)
  await expect(sessions.locator('.usage-section-meta')).toContainText('2 个任务')
  await expect(sessions).not.toContainText('生成配图')
  await expect(sessions).not.toContainText('侧边讨论')
  await expect(sessions).not.toContainText('侧边视频子任务')
  const rootRow = rows.filter({ hasText: '调研主任务' })
  await expect(rootRow).toHaveCount(1)
  await expect(rootRow.locator('[data-label="Cost"]')).toHaveText('$0.4007')
  await expect(rows.filter({ hasText: '独立单模型任务' }).locator('.usage-model-toggle')).toHaveCount(0)
  await expect(page.locator('.usage-model-card').filter({ hasText: 'fixture/text' })
    .locator('.usage-model-card__metric').filter({ hasText: '任务' }).locator('dd')).toHaveText('2')

  await rootRow.locator('.usage-model-toggle').click()
  const details = sessions.locator('.usage-expand')
  await expect(details).toHaveCount(1)
  await expect(details.locator('.usage-expand__row')).toHaveCount(4)
  await expect(details.locator('.usage-expand__total')).toHaveText('$0.4007')
  await expect(details.locator('.usage-expand__capability')).toHaveCount(0)
  await expect(details.locator('.usage-source')).toHaveCount(3)
  await expect(details.locator('.usage-source--mixed')).toHaveText('混合')
  const image = details.locator('.usage-expand__row').filter({ hasText: 'qwen-image-2.0' })
  await expect(image).not.toContainText('图片生成')
  await expect(image).toContainText('tokenrhythm')
  await expect(image).toContainText('1 张')
  await expect(image).toContainText('$0.0287')
  await expect(image.locator('.usage-source')).toHaveText('实际')
  const search = details.locator('.usage-expand__row').filter({ hasText: 'tavily' })
  await expect(search).not.toContainText('网页搜索')
  await expect(search).toContainText('1 次')
  await expect(search.locator('.usage-expand__share-pct')).toHaveText('—')
  await expect(search.locator('.usage-expand__cost')).toContainText('无定价')
  await expect(search.getByText('无定价', { exact: true })).toHaveCount(1)
  await expect(search.locator('.usage-source')).toHaveCount(0)
  const unknownCost = search.locator('.usage-expand__cost')
  const mutedColor = await unknownCost.evaluate(element => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--text-muted)'
    element.appendChild(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })
  await expect(unknownCost).toHaveCSS('color', mutedColor)
  const video = details.locator('.usage-expand__row').filter({ hasText: 'wan3.0-video' })
  await expect(video).not.toContainText('视频生成')
  await expect(video).toContainText('2 秒')
  await expect(video).toContainText('未知')
  await expect(video).not.toContainText('0 秒')
  await expect(video).toContainText('$0.1720')
  await expect(video.locator('.usage-source')).toHaveText('实际')
  await expectAlignedDetails(details)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../.tmp/usage-browser/desktop.png', fullPage: true })

  await page.getByRole('button', { name: '¥ CNY', exact: true }).click()
  await expect(summary.locator('.control-stat__value').nth(1)).toHaveText('¥3.0740')
  await expect(summary.locator('.control-stat__value').nth(3)).toHaveText('¥1.5370')
  await expect(rootRow.locator('[data-label="Cost"]')).toHaveText('¥2.7950')
  await expect(details.locator('.usage-expand__total')).toHaveText('¥2.7950')
  await expect(image.locator('.usage-expand__cost')).toContainText('¥0.2000')
  await expect(video.locator('.usage-expand__cost')).toContainText('¥1.2000')
  await rootRow.locator('.usage-model-toggle').click()
  await expect(page.locator('.usage-expand')).toHaveCount(0)
  await expect(rows).toHaveCount(2)

  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出', exact: true }).click()
  const download = await downloadEvent
  const csv = await readFile((await download.path())!, 'utf8')
  const values = csv.split('\n').map(line => [...line.matchAll(/"((?:[^"]|"")*)"(?:,|$)/g)]
    .map(match => match[1].replace(/""/g, '"')))
  const [headers, ...csvRows] = values
  const records = csvRows.map(row => Object.fromEntries(headers.map((header, index) => [header, row[index]])))
  expect(records).toHaveLength(3) // summary + two parent tasks, never the five session projections
  const summaryRecord = records.find(row => row.row_type === 'summary')!
  expect(summaryRecord.cost_usd_exact).toBe('0.440716845878136201')
  expect(summaryRecord.cost_cny).toBe('3.074000000')
  expect(JSON.parse(summaryRecord.task_groups_json)).toHaveLength(2)
  const detailsRecords = records.filter(row => row.row_type !== 'summary')
  expect(detailsRecords.map(row => row.row_type)).toEqual(['task', 'task'])
  expect(detailsRecords.map(row => row.session_id).sort()).toEqual(['lone', 'root'])
  expect(detailsRecords.map(row => row.task_group_id).sort()).toEqual(['lone-task', 'task-group'])
  expect(detailsRecords.reduce((sum, row) => sum + Number(row.cost_usd), 0)).toBeCloseTo(0.440716845878136201, 6)
  await rootRow.locator('.usage-model-toggle').click()
  await page.setViewportSize({ width: 1074, height: 1000 })
  await expectAlignedDetails(details)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../.tmp/usage-browser/medium.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(costCard.locator(':scope > *')).toHaveCount(2)
  await expect(search.locator('.usage-expand__source')).toBeHidden()
  await page.screenshot({ path: '../.tmp/usage-browser/narrow.png', fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
})
