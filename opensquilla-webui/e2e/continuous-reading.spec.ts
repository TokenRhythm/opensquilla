import { expect, test, type Locator, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { validateChatHistoryResult } from '../src/contracts/generated/v4/chatHistoryValidators.mjs'
import { activitySnapshotMatchesMessage, normalizeActivitySnapshot } from '../src/utils/chat/activitySnapshot'
import type { ChatMessage } from '../src/types/chat'
import { helloOkResponse } from './support/gateway-fixture'
import { CONTINUOUS_MESSAGE, CONTINUOUS_SESSION, continuousHistory, continuousPayloads } from './support/continuous-reading-fixture'

test.beforeAll(() => {
  expect(validateChatHistoryResult(continuousHistory), JSON.stringify(validateChatHistoryResult.errors)).toBe(true)
  const history = continuousHistory as {
    messages: Array<ChatMessage & { reasoning_content?: string }>;
    turn_outcomes: Array<{ turn_id: string; task_id: string; activity_snapshot: unknown }>;
  }
  const outcome = history.turn_outcomes[0]!
  const message = history.messages[1]!
  const snapshot = normalizeActivitySnapshot(outcome.activity_snapshot, outcome.turn_id, outcome.task_id)
  expect(snapshot).toBeDefined()
  expect(activitySnapshotMatchesMessage(snapshot!, {
    ...message, reasoning: { text: message.reasoning_content || '', seconds: 4 },
  })).toBe(true)
})

async function openFixture(page: Page, theme: 'miami' | 'light' | 'dark' = 'miami', history = continuousHistory) {
  const payloads: Record<string, unknown> = { ...continuousPayloads, 'chat.history': history }
  await page.addInitScript(selectedTheme => {
    localStorage.setItem('opensquilla-locale', 'zh-Hans')
    localStorage.setItem('opensquilla-theme', selectedTheme)
    localStorage.removeItem('opensquilla.appearance.toolDetails.v1')
  }, theme)
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../../src/opensquilla/gateway/static/img/opensquilla-mark.png', import.meta.url)), contentType: 'image/png',
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as { type?: string; id?: string; method?: string; nonce?: string }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong', ...(frame.nonce === undefined ? {} : { nonce: frame.nonce }) }))
        return
      }
      if (frame.type !== 'req') return
      ws.send(frame.method === 'connect' ? helloOkResponse()
        : JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method || ''] ?? {} }))
    })
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(CONTINUOUS_SESSION)}`)
  const answer = page.locator(`[data-message-id="${CONTINUOUS_MESSAGE}"]`)
  await expect(answer).toHaveCount(1, { timeout: 15_000 })
  await expect(answer.locator('.assistant-answer')).toContainText('连续阅读验收完成')
  return answer
}

async function expandProcess(answer: Locator) {
  const summary = answer.locator('.assistant-activity__summary')
  await summary.click()
  await expect(summary).toHaveAttribute('aria-expanded', 'true')
  await expect(answer.locator('.activity-narration').first()).toBeVisible()
  await expect(answer.locator('.assistant-activity__body')).toHaveCSS('opacity', '1')
}

async function assertNoPageOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
}

async function assertActivityHeaderBoundary(answer: Locator, expanded: boolean) {
  const header = answer.locator('.assistant-activity__header')
  await expect(header).toBeVisible()
  await expect(header).toHaveText(/^用时\s+\d/)
  const geometry = await header.evaluate(node => {
    const style = getComputedStyle(node)
    const headerRect = node.getBoundingClientRect()
    const activityRect = node.closest('.assistant-activity')!.getBoundingClientRect()
    const button = node.querySelector('button')!.getBoundingClientRect()
    const inner = node.parentElement!.querySelector('.assistant-activity__body-inner')!.getBoundingClientRect()
    const answer = node.parentElement!.parentElement!.querySelector('.assistant-answer .msg-ai-text > :first-child')!.getBoundingClientRect()
    return {
      width: headerRect.width, activityWidth: activityRect.width,
      left: headerRect.left, activityLeft: activityRect.left,
      borderWidth: parseFloat(style.borderBottomWidth),
      borderStyle: style.borderBottomStyle, borderColor: style.borderBottomColor,
      buttonHeight: button.height, lineBelowLabel: headerRect.bottom - button.bottom,
      bodyGap: inner.top - headerRect.bottom,
      answerFromDivider: answer.top - headerRect.bottom,
      answerFromActivity: answer.top - activityRect.bottom,
    }
  })
  expect(geometry.width).toBeGreaterThan(0)
  expect(Math.abs(geometry.width - geometry.activityWidth)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.left - geometry.activityLeft)).toBeLessThanOrEqual(1)
  expect(geometry.borderWidth).toBe(1)
  expect(geometry.borderStyle).toBe('solid')
  expect(geometry.borderColor).not.toMatch(/^(?:transparent|rgba\(0, 0, 0, 0\))$/)
  expect(geometry.buttonHeight).toBeCloseTo(28, 1)
  expect(geometry.lineBelowLabel).toBeGreaterThan(1)
  expect(Math.abs(geometry.answerFromActivity - 18)).toBeLessThanOrEqual(1)
  if (!expanded) expect(Math.abs(geometry.answerFromDivider - 18)).toBeLessThanOrEqual(1)
  if (expanded) expect(Math.abs(geometry.bodyGap - 12)).toBeLessThanOrEqual(1)
}

test('collapsed defaults and manual disclosure preserve mixed-tool chronology', async ({ page }) => {
  const answer = await openFixture(page)
  const summary = answer.locator('.assistant-activity__summary')
  await expect(summary).toHaveAttribute('aria-expanded', 'false')
  await expect(answer.locator('.assistant-activity__body')).toHaveAttribute('aria-hidden', 'true')
  await expect(answer.locator('.tool-row-body')).toHaveCount(0)
  await assertActivityHeaderBoundary(answer, false)

  await expandProcess(answer)
  await assertActivityHeaderBoundary(answer, true)
  const calls = answer.locator('.tool-row[aria-expanded]')
  await expect(calls).toHaveCount(2)
  for (const call of await calls.all()) await expect(call).toHaveAttribute('aria-expanded', 'false')
  await expect(answer.locator('.thinking-fold')).not.toHaveAttribute('open', '')
  const orders = await answer.locator('[data-activity-entry][data-activity-order]').evaluateAll(nodes =>
    nodes.map(node => Number(node.getAttribute('data-activity-order'))))
  expect(orders).toEqual([10, 20, 30, 40, 50])
  await expect(calls.nth(0)).toContainText('demo/index.html')
  await expect(calls.nth(1)).toContainText('npm run test:layout')

  await calls.nth(1).click()
  await expect(calls.nth(1)).toHaveAttribute('aria-expanded', 'true')
  await expect(answer.locator('.tool-row-body')).toHaveCount(1)
  await calls.nth(0).click()
  await expect(answer.locator('.tool-row-body')).toHaveCount(2)
  await summary.click()
  await expect(summary).toHaveAttribute('aria-expanded', 'false')
  await summary.click()
  await expect(calls.nth(1)).toHaveAttribute('aria-expanded', 'true')
  await expect(calls.nth(0)).toHaveAttribute('aria-expanded', 'true')
  await calls.nth(0).click()
  await expect(calls.nth(0)).toHaveAttribute('aria-expanded', 'false')
  await expect(calls.nth(1)).toHaveAttribute('aria-expanded', 'true')
})

for (const width of [1440, 1280, 390]) {
  test(`reading column and composer align at ${width}px without horizontal page overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const answer = await openFixture(page)
    const text = answer.locator('.assistant-answer .msg-ai-text')
    const composer = page.locator('.chat-composer-inner')
    await expect(composer).toBeVisible()
    const textBounds = await text.boundingBox()
    const composerBounds = await composer.boundingBox()
    expect(textBounds).not.toBeNull()
    expect(composerBounds).not.toBeNull()
    expect(Math.abs(textBounds!.x - composerBounds!.x)).toBeLessThanOrEqual(2)
    expect(Math.abs(textBounds!.width - composerBounds!.width)).toBeLessThanOrEqual(2)
    expect(textBounds!.width).toBeLessThanOrEqual(800)
    if (width === 1440) expect(textBounds!.width).toBeGreaterThanOrEqual(799)

    await expandProcess(answer)
    await assertActivityHeaderBoundary(answer, true)
    await answer.locator('.tool-row[aria-expanded]').nth(1).click()
    const detail = answer.locator('.tool-row-body')
    await expect(detail).toBeVisible()
    await assertActivityHeaderBoundary(answer, true)
    const detailBounds = await detail.boundingBox()
    expect(Math.abs(detailBounds!.x - textBounds!.x)).toBeLessThanOrEqual(2)
    expect(detailBounds!.width).toBeLessThanOrEqual(textBounds!.width + 1)
    await assertNoPageOverflow(page)
  })
}

test('prose retains readable spacing while metadata stays secondary and output stays bounded', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const answer = await openFixture(page)
  const paragraphs = answer.locator('.assistant-answer .msg-ai-text > p')
  const bounds = await paragraphs.evaluateAll(nodes => nodes.slice(0, 2).map(node => {
    const rect = node.getBoundingClientRect()
    return { top: rect.top, bottom: rect.bottom, fontSize: parseFloat(getComputedStyle(node).fontSize) }
  }))
  expect(bounds).toHaveLength(2)
  expect(bounds[0]!.fontSize).toBe(16)
  expect(bounds[1]!.top - bounds[0]!.bottom).toBeCloseTo(12, 0)
  await expandProcess(answer)
  const sizes = await answer.evaluate(root => ({
    prose: parseFloat(getComputedStyle(root.querySelector('.activity-narration')!).fontSize),
    tool: parseFloat(getComputedStyle(root.querySelector('.tool-row__label')!).fontSize),
    metadata: parseFloat(getComputedStyle(root.querySelector('.assistant-activity__label')!).fontSize),
    reasoning: parseFloat(getComputedStyle(root.querySelector('.thinking-fold__summary')!).fontSize),
  }))
  expect(sizes.prose).toBe(16)
  expect(sizes.tool).toBe(13)
  expect(sizes.reasoning).toBe(sizes.tool)
  expect(sizes.metadata).toBeLessThan(sizes.prose)
  const rhythm = await answer.evaluate(root => {
    const narration = [...root.querySelectorAll('.activity-narration')].map(node => node.getBoundingClientRect())
    const tools = [...root.querySelectorAll('.tool-row[aria-expanded]')].map(node => node.getBoundingClientRect())
    const conclusion = root.querySelector('.assistant-answer')!.getBoundingClientRect()
    const firstParagraph = root.querySelector('.assistant-answer .msg-ai-text > p')!.getBoundingClientRect()
    const reasoning = root.querySelector('.thinking-fold__summary')!.getBoundingClientRect()
    return { narrationToWrite: tools[0]!.top - narration[0]!.bottom,
      writeToNarration: narration[1]!.top - tools[0]!.bottom,
      narrationToShell: tools[1]!.top - narration[1]!.bottom,
      reasoningToNarration: narration[0]!.top - reasoning.bottom,
      reasoningHeight: reasoning.height, toolHeights: tools.map(row => row.height),
      conclusionInset: firstParagraph.top - conclusion.top }
  })
  await test.info().attach('activity-geometry', {
    body: JSON.stringify({ sizes, rhythm }, null, 2), contentType: 'application/json',
  })
  expect(rhythm.reasoningHeight).toBeCloseTo(28, 1)
  expect(rhythm.toolHeights).toHaveLength(2)
  for (const height of rhythm.toolHeights) expect(height).toBeCloseTo(28, 1)
  expect(Math.abs(rhythm.reasoningToNarration - 12)).toBeLessThanOrEqual(1)
  expect(Math.abs(rhythm.narrationToWrite - 12)).toBeLessThanOrEqual(1)
  expect(Math.abs(rhythm.narrationToShell - 12)).toBeLessThanOrEqual(1)
  expect(Math.abs(rhythm.writeToNarration - 12)).toBeLessThanOrEqual(1)
  expect(Math.abs(rhythm.conclusionInset - 8)).toBeLessThanOrEqual(1)
  await answer.locator('.tool-row[aria-expanded]').nth(1).click()
  const outputViewport = answer.locator('.activity-code-details__viewport')
  await expect(outputViewport).toHaveCount(1)
  const outputDimensions = await outputViewport.evaluate(node => ({
    height: node.clientHeight, scrollHeight: node.scrollHeight,
    width: node.clientWidth, scrollWidth: node.scrollWidth,
    verticalOverflow: getComputedStyle(node).overflowY,
    horizontalOverflow: getComputedStyle(node).overflowX,
  }))
  expect(outputDimensions.height).toBeLessThanOrEqual(320)
  expect(outputDimensions.scrollHeight).toBeGreaterThan(outputDimensions.height)
  expect(outputDimensions.scrollWidth).toBeGreaterThan(outputDimensions.width)
  expect(outputDimensions.verticalOverflow).toMatch(/auto|scroll/)
  expect(outputDimensions.horizontalOverflow).toMatch(/auto|scroll/)
  await assertNoPageOverflow(page)
})

test('legacy reasoning uses the same compact row dimensions without a structured timeline', async ({ page }) => {
  const history = structuredClone(continuousHistory)
  for (const outcome of history.turn_outcomes as Array<Record<string, unknown>>) delete outcome.activity_snapshot
  expect(validateChatHistoryResult(history), JSON.stringify(validateChatHistoryResult.errors)).toBe(true)
  const answer = await openFixture(page, 'miami', history)
  await expandProcess(answer)
  await expect(answer.locator('.reasoning-timeline')).toHaveCount(0)
  await expect(answer.locator('.assistant-unified-activity-timeline')).toHaveCount(0)
  const reasoning = answer.locator('.thinking-fold--in-activity > .thinking-fold__summary')
  await expect(reasoning).toBeVisible()
  const geometry = await answer.evaluate(root => {
    const reasoning = root.querySelector('.thinking-fold__summary')!
    const tool = root.querySelector('.tool-row[aria-expanded]')!
    return {
      reasoningSize: parseFloat(getComputedStyle(reasoning).fontSize),
      toolSize: parseFloat(getComputedStyle(tool.querySelector('.tool-row__label')!).fontSize),
      reasoningHeight: reasoning.getBoundingClientRect().height,
      toolHeight: tool.getBoundingClientRect().height,
    }
  })
  expect(geometry.reasoningSize).toBe(13)
  expect(geometry.toolSize).toBe(geometry.reasoningSize)
  expect(geometry.reasoningHeight).toBeCloseTo(28, 1)
  expect(geometry.toolHeight).toBeCloseTo(28, 1)
  await assertActivityHeaderBoundary(answer, true)
  await assertNoPageOverflow(page)
})

test('code language and copy actions occupy the header without covering source text', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 })
  const answer = await openFixture(page)
  const code = answer.locator('.assistant-answer pre.code-block')
  await code.scrollIntoViewIfNeeded()
  await expect(code.locator('.code-lang')).toHaveText(/css/i)
  await expect(code.locator('.code-copy-btn')).toBeVisible()
  const geometry = await code.evaluate(node => {
    const label = node.querySelector('.code-lang')!.getBoundingClientRect()
    const button = node.querySelector('.code-copy-btn')!.getBoundingClientRect()
    const source = node.querySelector('code')!
    const range = document.createRange()
    range.selectNodeContents(source)
    const sourceRect = range.getBoundingClientRect()
    return { labelRight: label.right, buttonLeft: button.left,
      headerBottom: Math.max(label.bottom, button.bottom), sourceTop: sourceRect.top,
      sourceLeft: sourceRect.left, cardLeft: node.getBoundingClientRect().left }
  })
  expect(geometry.labelRight).toBeLessThanOrEqual(geometry.buttonLeft)
  expect(geometry.sourceTop).toBeGreaterThan(geometry.headerBottom)
  expect(geometry.sourceLeft - geometry.cardLeft).toBeGreaterThanOrEqual(13)
  await assertNoPageOverflow(page)
})

for (const theme of ['miami', 'light', 'dark'] as const) {
  test(`${theme} visual coverage retains the shipped palette and collapsed disclosure`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    const answer = await openFixture(page, theme)
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
    if (theme === 'miami') {
      const colors = await page.evaluate(() => {
        const style = getComputedStyle(document.documentElement)
        return Object.fromEntries(['--bg', '--bg-surface', '--bg-surface-2', '--text', '--text-muted', '--accent', '--accent-secondary']
          .map(name => [name, style.getPropertyValue(name).trim().toUpperCase()]))
      })
      expect(colors).toEqual({ '--bg': '#FFF6F2', '--bg-surface': '#FFFBF8', '--bg-surface-2': '#FEEEE6',
        '--text': '#2A1830', '--text-muted': '#6B4A5E', '--accent': '#D71776', '--accent-secondary': '#077B92' })
    }
    await expect(answer.locator('.assistant-activity__summary')).toHaveAttribute('aria-expanded', 'false')
    await assertActivityHeaderBoundary(answer, false)
    await answer.locator('.assistant-activity').screenshot({ path: test.info().outputPath(`activity-${theme}-collapsed.png`) })
    await page.screenshot({ path: test.info().outputPath(`continuous-reading-${theme}-collapsed.png`) })
    await expandProcess(answer)
    await assertActivityHeaderBoundary(answer, true)
    await answer.locator('.tool-row[aria-expanded]').first().click()
    await answer.locator('.assistant-activity').screenshot({ path: test.info().outputPath(`activity-${theme}-expanded.png`) })
    await page.screenshot({ path: test.info().outputPath(`continuous-reading-${theme}-expanded.png`) })
    await assertNoPageOverflow(page)
  })
}
