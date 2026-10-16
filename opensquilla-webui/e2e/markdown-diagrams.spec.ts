import { expect, test, type Locator, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:markdown-diagrams'
const MERMAID = 'graph TD\n  Input[输入] --> Result[结果]'
const DOT = 'digraph Flow { Input [label="输入"]; Result [label="结果"]; Input -> Result; }'
const fence = (language: string, source: string) => `\`\`\`${language}\n${source}\n\`\`\``

// Uses the shipped App and native browser SVG/DOMPurify implementation. Every
// Gateway exchange is synthetic: this never invokes a model or real profile.
async function openFixture(page: Page, text: string, theme: 'light' | 'dark' = 'light') {
  await page.addInitScript(selectedTheme => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', selectedTheme)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async (value: string) => {
        ;(window as unknown as { diagramCopied: string }).diagramCopied = value
      },
    } })
  }, theme)
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: { mode: 'prompt', pending: [], allowPatterns: [], denyPatterns: [] } }))
  await page.route('**/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../../src/opensquilla/gateway/static/img/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as { type?: string; id?: string; method?: string }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse())
        return
      }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([{
          role: 'assistant', id: 'diagram-answer', timestamp: 1_800_000_000,
          text: `Diagram rendering fixture.\n\n${text}`,
        }]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'safe', source: 'default' },
        'sandbox.capability.status': { available: false },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method || ''] ?? {} }))
    })
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Diagram rendering fixture.' })
  await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected', { timeout: 15_000 })
  await expect(answer).toHaveCount(1)
  return answer
}

function diagramBlocks(answer: Locator, language: string) {
  return answer.locator(`pre:has(code.language-${language}), [data-diagram-language="${language}"]`)
}

async function ready(block: Locator) {
  await block.evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(block).toHaveClass(/mermaid-diagram/, { timeout: 20_000 })
  const svg = block.locator('.mermaid-svg-wrap > svg')
  await expect(svg).toBeVisible()
  expect(await svg.evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThan(0)
  return svg
}

test('recovers all visible diagrams after the bounded renderer queue fills', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 2200 })
  const answer = await openFixture(page, Array.from({ length: 14 }, (_, index) =>
    fence('mermaid', `flowchart LR\n A${index}-->B${index}`),
  ).join('\n\n'))
  const blocks = diagramBlocks(answer, 'mermaid')
  await expect(blocks).toHaveCount(14)
  // All short source blocks initially intersect; later SVGs may push the tail
  // offscreen. Reentering continues cancelled work without a false failure.
  for (let index = 0; index < 14; index++) await ready(blocks.nth(index))
  await expect(answer.locator('.mermaid-diagram')).toHaveCount(14)
  await expect(answer.locator('.mermaid-error')).toHaveCount(0)
})

test('preserves author diagram descriptions in the image accessibility tree', async ({ page }) => {
  const answer = await openFixture(page, [
    fence('mermaid', 'flowchart LR\naccTitle: Order approval flow\naccDescr: Submitted orders are reviewed before delivery\n A[Submit]-->B[Review]-->C[Deliver]'),
    fence('mermaid', 'flowchart LR\n Plain-->Diagram'),
  ].join('\n\n'))
  const blocks = diagramBlocks(answer, 'mermaid')
  const described = await ready(blocks.nth(0))
  await expect(described.locator('title')).toHaveText('Order approval flow')
  await expect(described.locator('desc')).toHaveText('Submitted orders are reviewed before delivery')
  await expect(described).toHaveAccessibleName('Order approval flow')
  await expect(described).toHaveAccessibleDescription('Submitted orders are reviewed before delivery')
  const ordinary = await ready(blocks.nth(1))
  await expect(ordinary).toHaveAttribute('aria-roledescription', /flowchart/)
})

test('shipped chat preserves mathematical meaning, literal code and Markdown structure', async ({ page }) => {
  const answer = await openFixture(page, [
    'Numeric $2^n$ and \\(f(x)=x^2\\).',
    '$$\\sqrt{x}+\\overbrace{a+b}$$',
    '~~~latex\n$x^2$\n~~~',
    '3. Third\n4. Fourth',
    '| Left | Center | Right |\n| :--- | :---: | ---: |\n| a | b | 100 |',
    'Statement[^1].',
    '[^1]: Original **footnote body**.',
  ].join('\n\n'))
  await expect(answer.locator('.katex')).toHaveCount(3)
  expect(await answer.locator('.katex svg path').count()).toBeGreaterThanOrEqual(2)
  await expect(answer.locator('math')).toHaveCount(3)
  const code = answer.locator('pre')
  await expect(code.locator('code')).toHaveText('$x^2$')
  await code.hover()
  await code.getByRole('button', { name: 'Copy', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as unknown as { diagramCopied: string }).diagramCopied)).toBe('$x^2$')
  await expect(answer.locator('ol:not([data-markdown-footnotes])')).toHaveAttribute('start', '3')
  expect(await answer.locator('td').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).textAlign))).toEqual(['left', 'center', 'right'])
  await expect(answer.locator('[data-markdown-footnote="1"]')).toContainText('Original footnote body')
  await answer.locator('[data-markdown-footnote-ref="1"]').click()
  await expect(answer.locator('[data-markdown-footnote="1"]')).toBeFocused()
})

for (const [language, source] of [['mermaid', MERMAID], ['dot', DOT], ['graphviz', DOT]] as const) {
  test(`renders ${language} history as a decoded diagram and copies its original source`, async ({ page }) => {
    const markdown = language === 'graphviz' ? `~~~graphviz\n${source}\n~~~` : fence(language, source)
    const answer = await openFixture(page, markdown)
    const block = diagramBlocks(answer, language)
    const image = await ready(block)
    const svg = await image.evaluate(node => node.outerHTML)
    const svgPath = test.info().outputPath(`${language}-rendered.svg`)
    await writeFile(svgPath, svg)
    await test.info().attach(`${language}-rendered.svg`, { path: svgPath, contentType: 'image/svg+xml' })
    expect(svg).toContain('<svg')
    expect(svg).toContain('输入')
    expect(svg).toContain('结果')
    expect(svg).not.toMatch(/<script|\bonload\s*=/i)
    // Inline SVG uses the same live sanitizer and theme as the shared diagram
    // owner. Check node paint directly; HTML labels are intentionally supported.
    const fills = await image.locator('.node rect, polygon').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).fill))
    expect(fills.some(fill => !['rgb(0, 0, 0)', 'rgba(0, 0, 0, 0)', 'none'].includes(fill))).toBe(true)
    await block.hover()
    await block.getByRole('button', { name: 'Copy source', exact: true }).click()
    await expect.poll(() => page.evaluate(() => (window as unknown as { diagramCopied?: string }).diagramCopied))
      .toBe(source)
    await expect(block.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
    await expect(block.locator('.mermaid-svg-wrap > svg')).toHaveCount(1)
    await expect(block.locator('.code-copy-btn')).toHaveCount(0)
    if (language === 'mermaid') await page.screenshot({ path: 'output/playwright/markdown-diagram-light.png', fullPage: true })
  })
}

test('re-renders an already displayed diagram when the chat theme changes', async ({ page }) => {
  const answer = await openFixture(page, fence('mermaid', MERMAID), 'dark')
  const block = diagramBlocks(answer, 'mermaid')
  const image = await ready(block)
  const darkFill = await image.locator('.node rect').first().evaluate(node => getComputedStyle(node).fill)
  await page.screenshot({ path: 'output/playwright/markdown-diagram-dark.png', fullPage: true })
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'light'
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await ready(block)
  await expect.poll(() => image.locator('.node rect').first().evaluate(node => getComputedStyle(node).fill)).not.toBe(darkFill)
  const lightFill = await image.locator('.node rect').first().evaluate(node => getComputedStyle(node).fill)
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
  await ready(block)
  await expect.poll(() => image.locator('.node rect').first().evaluate(node => getComputedStyle(node).fill)).toBe(darkFill)
  expect(lightFill).not.toBe(darkFill)
  await expect(block.locator('.mermaid-svg-wrap')).toHaveCount(1)
})

test('retains visible source for incomplete, invalid, and excessive diagram blocks', async ({ page }) => {
  const excessive = `graph TD\n  A[${'x'.repeat(50_001)}]`
  const text = [
    fence('mermaid', 'this is not a diagram'),
    fence('dot', 'digraph { broken -> }'),
    fence('mermaid', excessive),
    '```mermaid\ngraph TD\n  Pending --> Complete',
  ].join('\n\n')
  const answer = await openFixture(page, text)
  const blocks = answer.locator('.visualize-body__text > pre, .visualize-body__text > .mermaid-error')
  await expect(blocks).toHaveCount(4)
  for (const index of [0, 1, 2]) {
    const block = blocks.nth(index)
    await block.evaluate(node => node.scrollIntoView({ block: 'center' }))
    await expect(block).toHaveClass(/mermaid-error/, { timeout: 20_000 })
    await block.locator('summary').click()
    await expect(block.locator('.mermaid-err-src')).toBeVisible()
    await expect(block.locator('img')).toHaveCount(0)
  }
  await expect(blocks.nth(2)).toContainText(/large|limit/i)
  const incomplete = blocks.nth(3)
  await incomplete.evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(incomplete.locator(':scope > code')).toBeVisible()
  await expect(incomplete).not.toHaveAttribute('data-diagram-language')
  await expect(incomplete.locator('img, .chat-diagram-note')).toHaveCount(0)
})

test('defers offscreen diagrams until their block enters the viewport', async ({ page }) => {
  const padding = Array.from({ length: 180 }, (_, index) => `Paragraph ${index}: ordinary conversation text.`).join('\n\n')
  const answer = await openFixture(page, `${padding}\n\n${fence('mermaid', MERMAID)}\n\n${padding}`)
  const block = diagramBlocks(answer, 'mermaid')
  await expect(block).toHaveCount(1)
  await expect.poll(() => block.evaluate(node => {
    const rect = node.getBoundingClientRect()
    return rect.bottom < 0 || rect.top > window.innerHeight
  })).toBe(true)
  await expect(block.locator('.mermaid-svg-wrap')).toHaveCount(0)
  await ready(block)
})

test('does not treat a shorter closing fence as a complete diagram', async ({ page }) => {
  const answer = await openFixture(page, `\`\`\`\`mermaid\n${MERMAID}\n\`\`\``)
  const block = answer.locator('pre')
  await block.evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(block).toHaveCount(1)
  await expect(block).not.toHaveAttribute('data-diagram-language')
  await expect(block.locator(':scope > code')).toBeVisible()
  await expect(block.locator(':scope > code')).toContainText(MERMAID)
  await expect(block.locator('img, .chat-diagram-note')).toHaveCount(0)
})

test('does not execute diagram author HTML, script URLs, or external resources', async ({ page }) => {
  const externalRequests: string[] = []
  page.on('request', request => {
    if (request.url().includes('diagram-attacker.invalid')) externalRequests.push(request.url())
  })
  const maliciousMermaid = 'graph TD\n  A["<img src=https://diagram-attacker.invalid/probe onerror=window.diagramExecuted=1>"]'
  const linkedDot = 'digraph { A [label="safe label", URL="javascript:window.diagramExecuted=1", target="_top"]; A -> B; }'
  const answer = await openFixture(page, [
    fence('mermaid', maliciousMermaid),
    fence('dot', linkedDot),
    fence('svg', '<svg onload="window.diagramExecuted=1"><script>window.diagramExecuted=1</script></svg>'),
  ].join('\n\n'))
  const mermaid = diagramBlocks(answer, 'mermaid')
  await mermaid.evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(mermaid).toHaveClass(/mermaid-error/, { timeout: 20_000 })
  await mermaid.locator('summary').click()
  await expect(mermaid.locator('.mermaid-err-src')).toContainText('onerror=window.diagramExecuted=1')
  const dot = diagramBlocks(answer, 'dot')
  const image = await ready(dot)
  const svg = await image.evaluate(node => node.outerHTML)
  expect(svg).not.toMatch(/javascript:|\bhref\s*=|<script|<foreignObject|\bon\w+\s*=/i)
  await expect(answer.locator('iframe, object, script, svg[onload], img[src*="diagram-attacker.invalid"]')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as { diagramExecuted?: boolean }).diagramExecuted)).toBeUndefined()
  expect(externalRequests).toEqual([])
})
