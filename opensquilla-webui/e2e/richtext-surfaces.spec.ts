import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

test.skip(!process.env.OPENSQUILLA_DIAGRAM_ENGINE_DEV, 'Uses source fixtures through Vite')

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async (text: string) => { window.richtextCopied = text },
    } })
  })
  await page.goto('/e2e/fixtures/richtext-preview.html')
  await page.waitForFunction(() => !!window.richtextPreview)
})

for (const surface of ['chat', 'process', 'file'] as const) {
  test(`${surface} preserves math shapes, code, numbered lists, alignment and footnotes`, async ({ page }) => {
    await page.evaluate(value => window.richtextPreview.setSurface(value), surface)
    const body = page.locator(`[data-surface="${surface}"]`)
    await expect(body.locator('.katex')).toHaveCount(3)
    await expect(body.locator('.katex math')).toHaveCount(3)
    expect(await body.locator('.katex svg path').count()).toBeGreaterThanOrEqual(3)
    await expect(body.locator('ol:not([data-markdown-footnotes])')).toHaveAttribute('start', '3')
    await expect(body.locator('input[type="checkbox"]')).toHaveCount(2)
    expect(await body.locator('input[type="checkbox"]').evaluateAll(nodes => nodes.every(node => (node as HTMLInputElement).disabled))).toBe(true)
    for (const align of ['left', 'center', 'right']) {
      expect(await body.locator(`td[align="${align}"]`).evaluateAll(nodes => nodes.every(node => getComputedStyle(node).textAlign === node.getAttribute('align')))).toBe(true)
    }
    const code = body.locator('pre').first()
    await expect(code.locator('code')).toHaveText('$x^2$ 和 \\(f(x)\\) 保持源码')
    await expect(code.locator('.katex')).toHaveCount(0)
    expect(await code.locator('.code-lang').evaluate(node => getComputedStyle(node).position)).toBe('absolute')
    await code.hover()
    await code.locator('.code-copy-btn').click()
    await expect.poll(() => page.evaluate(() => window.richtextCopied)).toBe('$x^2$ 和 \\(f(x)\\) 保持源码')
    const ref = body.locator('[data-markdown-footnote-ref="1"]').first()
    const note = body.locator('[data-markdown-footnote="1"]')
    await expect(note).toContainText('注释正文保留')
    await ref.focus()
    await ref.press('Enter')
    await expect(note).toBeFocused()
    await body.locator('[data-markdown-footnote-back="1"]').click()
    await expect(ref).toBeFocused()
    const diagramHost = body.locator('pre:has(code.language-mermaid), .mermaid-diagram')
    await diagramHost.evaluate(node => node.scrollIntoView({ block: 'center' }))
    const diagram = body.locator('.mermaid-diagram')
    await expect(diagram.locator('.mermaid-svg-wrap > svg')).toBeVisible({ timeout: 20_000 })
    await expect(diagram.getByRole('button', { name: '复制源码', exact: true })).toHaveCount(1)
    await diagram.getByRole('button', { name: '复制源码', exact: true }).click()
    await expect.poll(() => page.evaluate(() => window.richtextCopied)).toBe(await page.evaluate(() => window.richtextPreview.diagram))
    if (surface === 'file') {
      const picture = body.locator('img[alt="附件配图"]')
      await expect(picture).toBeVisible()
      await expect.poll(() => picture.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true)
      await expect(picture).toHaveAttribute('src', /^blob:/)
      expect(await page.evaluate(() => window.richtextCatalogRequests)).toEqual([{ session: 'richtext-preview', limit: 200, singlePage: true }])
    }
    await page.screenshot({ path: `output/playwright/richtext-${surface}-light.png`, fullPage: true })
  })
}

test('streaming commits rich blocks once and keeps an open tail plain until settle', async ({ page }) => {
  await page.evaluate(async () => {
    await window.richtextPreview.setContent(window.richtextPreview.sample + '\n\nOpen tail $y^2$')
    await window.richtextPreview.setStreaming(true)
  })
  const body = page.locator('[data-surface="chat"]')
  await expect(body.locator('.katex')).toHaveCount(3)
  await expect(body.locator('.streaming-tail')).toHaveText('Open tail $y^2$')
  await expect(body.locator('.streaming-tail .katex')).toHaveCount(0)
  await body.locator('pre:has(code.language-mermaid), .mermaid-diagram').evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(body.locator('.mermaid-svg-wrap > svg')).toBeVisible({ timeout: 20_000 })
  await page.evaluate(() => window.richtextPreview.setStreaming(false))
  await expect(body.locator('.katex')).toHaveCount(4)
  await expect(body.locator('pre').first().locator('code')).toHaveText('$x^2$ 和 \\(f(x)\\) 保持源码')
})

test('rich blocks keep the same prose boundary while streaming and after settling', async ({ page }, testInfo) => {
  const multipleRichBlocks = '## earlier-prefix\n\n~~~text\nstable code\n~~~\n\n'
    + '| boundary-prefix | value |\n|---|---|\n| one | two |\n\n'
  const prefixes = [
    ['table', '| boundary-prefix | value |\n|---|---|\n| one | two |\n\n'],
    ['heading', '## boundary-prefix\n\n'],
    ['code', '~~~text\nboundary-prefix\n~~~\n\n'],
    ['paragraph', 'boundary-prefix\n\n'],
    ['list', '- boundary-prefix\n\n'],
    ['quote', '> boundary-prefix\n\n'],
    ['multiple-rich-blocks', multipleRichBlocks],
  ] as const
  const measurements: Array<{ boundary: string; frozen: boolean; streaming: number; settled: number }> = []
  const body = page.locator('[data-surface="chat"]')
  const offsets = (prefix = 'boundary-prefix', tail = 'boundary-tail') => body.evaluate((root, tokens) => {
    const tokenTop = (token: string) => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const start = (node.textContent || '').indexOf(token)
        if (start < 0) continue
        const range = document.createRange()
        range.setStart(node, start)
        range.setEnd(node, start + token.length)
        return range.getBoundingClientRect().top
      }
      throw new Error(`Missing boundary token: ${token}`)
    }
    return tokenTop(tokens.tail) - tokenTop(tokens.prefix)
  }, { prefix, tail })
  for (const [name, prefix] of prefixes) {
    for (const frozen of [false, true]) {
      const content = prefix + 'boundary-tail ' + 'plain prose '.repeat(frozen ? 2_000 : 2)
      await page.evaluate(async value => {
        await window.richtextPreview.setContent(value)
        await window.richtextPreview.setStreaming(true)
        await document.fonts.ready
      }, content)
      await expect(body.locator('.streaming-plain-block')).toHaveCount(frozen ? 1 : 0)
      const streamingOffset = await offsets()
      if (name === 'table' && !frozen) {
        await page.screenshot({ path: testInfo.outputPath('table-prose-streaming.png') })
      }
      await page.evaluate(() => window.richtextPreview.setStreaming(false))
      await expect(body.locator('.streaming-text-part')).toHaveCount(0)
      const settledOffset = await offsets()
      measurements.push({ boundary: name, frozen, streaming: streamingOffset, settled: settledOffset })
      expect(Math.abs(settledOffset - streamingOffset), `${name}, frozen=${frozen}`).toBeLessThanOrEqual(2)
      if (name === 'table' && !frozen) {
        await page.screenshot({ path: testInfo.outputPath('table-prose-settled.png') })
      }
    }
  }
  // Adding an open tail must only change its own boundary. Earlier committed
  // rich blocks keep their existing geometry and copy controls.
  await page.evaluate(async value => {
    await window.richtextPreview.setContent(value)
    await window.richtextPreview.setStreaming(true)
  }, multipleRichBlocks)
  const richOnlyOffset = await offsets('earlier-prefix', 'boundary-prefix')
  await page.evaluate(value => window.richtextPreview.setContent(value + 'boundary-tail prose'), multipleRichBlocks)
  expect(Math.abs((await offsets('earlier-prefix', 'boundary-prefix')) - richOnlyOffset)).toBeLessThanOrEqual(2)
  await expect(body.locator('.code-copy-btn')).toHaveCount(1)
  const reportPath = testInfo.outputPath('prose-boundaries.json')
  await writeFile(reportPath, JSON.stringify(measurements, null, 2), 'utf8')
  await testInfo.attach('prose-boundaries.json', { path: reportPath, contentType: 'application/json' })
})

test('all themes retain legible math and process diagram with one copy control', async ({ page }) => {
  test.setTimeout(90_000)
  await page.evaluate(() => window.richtextPreview.setSurface('process'))
  const themes = await page.getByRole('combobox', { name: '主题' }).locator('option').evaluateAll(nodes => nodes.map(node => (node as HTMLOptionElement).value))
  const body = page.locator('[data-surface="process"]')
  const block = body.locator('.mermaid-diagram')
  for (const theme of themes) {
    await page.evaluate(value => window.richtextPreview.setTheme(value), theme)
    await body.locator('pre:has(code.language-mermaid), .mermaid-diagram').evaluate(node => node.scrollIntoView({ block: 'center' }))
    await expect(block.locator('.mermaid-svg-wrap > svg')).toBeVisible({ timeout: 20_000 })
    await expect(block.getByRole('button', { name: '复制源码', exact: true })).toHaveCount(1)
    expect(await block.locator('.mermaid-svg-wrap > svg').evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThan(0)
    expect(await body.locator('.katex').first().evaluate(node => getComputedStyle(node).color)).not.toBe('rgba(0, 0, 0, 0)')
  }
  await page.evaluate(() => window.richtextPreview.setTheme('dark'))
  await page.screenshot({ path: 'output/playwright/richtext-process-dark.png', fullPage: true })
})

test('file images use bounded local resources and vetted HTTPS while active markup stays inert', async ({ page }) => {
  const external: string[] = []
  await page.route('https://untrusted.example/**', route => { external.push(route.request().url()); return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=', 'base64') }) })
  await page.evaluate(async () => {
    await window.richtextPreview.setContent([
      '![外部配图](https://untrusted.example/tracker.png)',
      '![本地路径](file:///C:/private.png)',
      '<svg onload="window.richtextInjected=true"><script>window.richtextInjected=true</script></svg>',
      '<img src="https://untrusted.example/html.png" onerror="window.richtextInjected=true">',
    ].join('\n\n'))
    await window.richtextPreview.setSurface('file')
  })
  const body = page.locator('[data-surface="file"]')
  await expect(body.locator('img[alt="外部配图"]')).toHaveAttribute('src', 'https://untrusted.example/tracker.png')
  await expect(body).toContainText('本地路径')
  await expect(body.locator('img[alt="附件配图"]')).toBeVisible()
  await expect(body.locator('script, svg[onload], img[onerror], img[src^="file:"]')).toHaveCount(0)
  expect(external).toContain('https://untrusted.example/tracker.png')
  expect(await page.evaluate(() => (window as unknown as { richtextInjected?: boolean }).richtextInjected)).toBeUndefined()
})

test('real PNG export preserves math strokes, table alignment, notes and original code', async ({ page }) => {
  test.setTimeout(90_000)
  await page.evaluate(() => window.richtextPreview.setTheme('dark'))
  await page.locator('[data-surface="chat"] pre:has(code.language-mermaid), [data-surface="chat"] .mermaid-diagram').evaluate(node => node.scrollIntoView({ block: 'center' }))
  await expect(page.locator('[data-surface="chat"] .mermaid-svg-wrap > svg')).toBeVisible({ timeout: 20_000 })
  const result = await page.evaluate(async () => {
    const shareModule = '/src/composables/chat/useChatShareExport.ts'
    const vueModule = '/node_modules/.vite/deps/vue.js'
    const { useChatShareExport } = await import(shareModule)
    const { ref } = await import(vueModule)
    const body = document.querySelector<HTMLElement>('[data-surface="chat"]')!
    body.dataset.shareMessageId = 'richtext'
    const originalDiagram = body.querySelector('.mermaid-diagram')!
    const originalSvg = originalDiagram.innerHTML
    const originalFill = getComputedStyle(originalDiagram.querySelector('.node rect')!).fill
    let snapshot: { math: number; paths: number; align: string[]; diagrams: number; controls: number; fill: string } | undefined
    const observer = new MutationObserver(() => {
      const stage = document.getElementById('opensquilla-share-export-stage')
      if (stage) snapshot = {
        math: stage.querySelectorAll('.katex').length,
        paths: stage.querySelectorAll('.katex svg path').length,
        align: Array.from(stage.querySelectorAll('th')).map(node => getComputedStyle(node).textAlign),
        diagrams: stage.querySelectorAll('.mermaid-svg-wrap > svg').length,
        controls: stage.querySelectorAll('.mermaid-toolbar, .mermaid-err-copy').length,
        fill: stage.querySelector('.mermaid-svg-wrap .node rect') ? getComputedStyle(stage.querySelector('.mermaid-svg-wrap .node rect')!).fill : '',
      }
    })
    observer.observe(document.body, { childList: true, subtree: true })
    try {
      const { buildShareImage } = useChatShareExport({ threadRef: ref(body.parentElement), title: () => '富文本验收', aiGeneratedLabel: () => '内容由 AI 生成' })
      const output = await buildShareImage(new Set(['richtext']), { theme: 'light' })
      const dataUrl = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsDataURL(output.blob) })
      return { dataUrl, width: output.width, height: output.height, snapshot, clean: !document.getElementById('opensquilla-share-export-stage'), theme: document.documentElement.dataset.theme, originalFill, originalPreserved: body.querySelector('.mermaid-diagram') === originalDiagram && originalDiagram.innerHTML === originalSvg }
    } finally { observer.disconnect() }
  })
  expect(result.snapshot?.math).toBe(3)
  expect(result.snapshot!.paths).toBeGreaterThanOrEqual(3)
  expect(result.snapshot?.align).toEqual(['left', 'center', 'right'])
  expect(result.snapshot?.diagrams).toBe(1)
  expect(result.snapshot?.controls).toBe(0)
  expect(result.snapshot?.fill).not.toBe(result.originalFill)
  expect(result.originalPreserved).toBe(true)
  expect(result.clean).toBe(true)
  expect(result.theme).toBe('dark')
  await writeFile('output/playwright/richtext-share-light-from-dark.png', Buffer.from(result.dataUrl.split(',')[1], 'base64'))
})


test('trace appends keep the existing diagram zoom and locale owner alive', async ({ page }) => {
  await page.evaluate(async () => {
    const vuePath = '/node_modules/.vite/deps/vue.js'
    const tracePath = '/src/components/run/RunTrace.vue'
    const localePath = '/src/i18n/index.ts'
    const renderPath = '/src/utils/markdown/renderCore.ts'
    const { createApp, defineComponent, h, ref, nextTick } = await import(vuePath)
    const { default: RunTrace } = await import(tracePath)
    const { default: i18n } = await import(localePath)
    const { renderMarkdownCore } = await import(renderPath)
    const items = ref([{ type: 'text', key: 'first', html: renderMarkdownCore('```mermaid\nflowchart LR\n A-->B\n```') }])
    const host = document.createElement('section')
    host.dataset.traceAcceptance = 'true'
    document.body.append(host)
    const app = createApp(defineComponent(() => () => h(RunTrace, {
      items: items.value, presentation: 'activity', isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
    }))).use(i18n)
    app.mount(host)
    ;(window as unknown as { traceAcceptance: unknown }).traceAcceptance = {
      append: async () => { items.value.push({ type: 'text', key: 'second', html: '<p>Following process message</p>' }); await nextTick() },
      english: async () => { i18n.global.locale.value = 'en'; await nextTick() },
      cleanup: () => { app.unmount(); host.remove() },
    }
  })
  const trace = page.locator('[data-trace-acceptance]')
  await trace.evaluate(node => node.scrollIntoView({ block: 'center' }))
  const svg = trace.locator('.mermaid-svg-wrap > svg')
  await expect(svg).toBeVisible({ timeout: 20_000 })
  const originalViewBox = await svg.getAttribute('viewBox')
  await trace.getByRole('button', { name: '放大', exact: true }).click()
  const zoomedViewBox = await svg.getAttribute('viewBox')
  expect(zoomedViewBox).not.toBe(originalViewBox)
  const element = await svg.elementHandle()
  await page.evaluate(async () => {
    const api = (window as unknown as { traceAcceptance: { append: () => Promise<void>; english: () => Promise<void> } }).traceAcceptance
    await api.append(); await api.english()
  })
  await expect(trace).toContainText('Following process message')
  await expect(trace.getByRole('button', { name: 'Copy source', exact: true })).toBeVisible()
  expect(await svg.evaluate((node, original) => node === original, element)).toBe(true)
  await expect(svg).toHaveAttribute('viewBox', zoomedViewBox!)
  await trace.getByRole('button', { name: 'Reset view', exact: true }).click()
  await expect(svg).toHaveAttribute('viewBox', originalViewBox!)
  await page.evaluate(() => (window as unknown as { traceAcceptance: { cleanup: () => void } }).traceAcceptance.cleanup())
})
