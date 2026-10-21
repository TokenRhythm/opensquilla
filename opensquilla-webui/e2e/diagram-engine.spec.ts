import { expect, test } from '@playwright/test'

// This suite imports the actual TypeScript engine through Vite in a native
// Chromium DOM. Production-artifact integration is covered by chat suites.
test.skip(!process.env.OPENSQUILLA_DIAGRAM_ENGINE_DEV, 'Requires the Vite development server')

test.beforeEach(async ({ page }) => {
  await page.route('**/__diagram-engine-fixture', route => route.fulfill({
    contentType: 'text/html',
    body: '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\' \'wasm-unsafe-eval\'; worker-src \'self\'; style-src \'self\' \'unsafe-inline\'; img-src \'self\' data:;"></head><body></body></html>',
  }))
  await page.goto('/__diagram-engine-fixture')
})

test('ordinary Markdown does not load either diagram engine', async ({ page }) => {
  const requests: string[] = []
  page.on('request', request => requests.push(request.url()))
  const html = await page.evaluate(async () => {
    const blocksPath = '/src/utils/markdown/mermaidRenderer.ts'
    await import(blocksPath)
    const markdownPath = '/src/composables/chat/useChatTextRendering.ts'
    const { useChatTextRendering } = await import(markdownPath)
    return useChatTextRendering().renderMarkdown('Ordinary **answer**, without a diagram.')
  })
  expect(html).toContain('<strong>answer</strong>')
  expect(requests.filter(url => /\/mermaid[./_-]|\/@viz-js|graphviz\.worker/i.test(url))).toEqual([])
})

test('SVG boundary strips active content and external resources in a native DOM', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const markdownPath = '/src/composables/chat/useChatTextRendering.ts'
    await import(markdownPath)
    const path = '/src/utils/markdown/diagrams/sanitize.ts'
    const { sanitizeDiagramSvg } = await import(path)
    const svg = sanitizeDiagramSvg(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40" onload="alert(1)">
      <script>alert(1)</script><foreignObject><div>HTML</div></foreignObject>
      <image href="https://example.com/pixel" />
      <style>@import "https://example.com/style";</style>
      <style>@keyframes pulse { from {opacity:0} to {opacity:1} }.diagram-label{fill:blue;animation:pulse 1s}.label > tspan{fill:red}.bad{fill:url(https://example.com/x)}</style>
      <path style="fill:url(https://example.com/fill)" d="M0 0" />
      <a href="javascript:alert(1)"><text>safe label</text></a>
      <use href="https://example.com/svg#x" /><use href="#local" />
      <text class="diagram-label">完成</text>
    </svg>`)
    const errors: string[] = []
    for (const input of ['<svg viewBox="0 0 9000 1"/>', '<svg viewBox="0 0 5000 5000"/>', '<div>not SVG</div>']) {
      try { sanitizeDiagramSvg(input); errors.push('accepted') }
      catch (error) { errors.push((error as { code: string }).code) }
    }
    return { svg, errors }
  })
  expect(result.svg).not.toMatch(/<script|foreignObject|<image|onload|https:|javascript:|@import|<a\b/)
  expect(result.svg).toContain('safe label')
  expect(result.svg).toContain('href="#local"')
  expect(result.svg).toContain('class="diagram-label"')
  expect(result.svg).toContain('.diagram-label{fill:blue}')
  expect(result.svg).toContain('.label &gt; tspan{fill:red}')
  expect(result.svg).not.toContain('@keyframes')
  expect(result.errors).toEqual(['budget', 'budget', 'render'])
})

test('real Mermaid and Graphviz render labels in light and dark themes', async ({ page }) => {
  test.setTimeout(120_000)
  const result = await page.evaluate(async () => {
    const markdownPath = '/src/composables/chat/useChatTextRendering.ts'
    const { useChatTextRendering } = await import(markdownPath)
    const { renderMarkdown } = useChatTextRendering()
    const path = '/src/utils/markdown/mermaidRenderer.ts'
    const { renderMarkdownDiagrams } = await import(path)
    const labels = {
      zoomIn: 'Zoom in', zoomOut: 'Zoom out', reset: 'Reset', copyCode: 'Copy source',
      copied: 'Copied', copyFailed: 'Copy failed', exportPng: 'Export PNG', exportFailed: 'Export failed',
      loadFailed: 'Load failed', tooLarge: 'Too large', blockedResource: 'Blocked resource', viewSource: 'View source',
      syntaxErrorAt: (line: number, excerpt: string) => `${line}: ${excerpt}`,
    }
    const cases = [
      ['mermaid', 'flowchart LR\n A[开始] --> B[完成]'],
      ['mermaid', 'sequenceDiagram\n participant A as 客户端\n participant B as 服务端\n A->>B: 请求\n B-->>A: 返回'],
      ['dot', 'digraph { a[label="开始"]; b[label="完成"]; a -> b; }'],
      ['graphviz', 'graph { subgraph cluster_a { a[label="甲"]; b[label="乙"]; } a -- b; }'],
    ]
    const results: Array<{ language: string; theme: string; length: number; text: string; labelColors: string[]; nodeFills: string[] }> = []
    for (const theme of ['light', 'dark']) {
      for (const [language, source] of cases) {
        const probe = document.createElement('figure')
        probe.style.cssText = `display:inline-block;vertical-align:top;margin:12px;padding:12px;width:500px;--bg:${theme === 'dark' ? '#202020' : '#ffffff'};--text:${theme === 'dark' ? '#eeeeee' : '#242424'};--border:#aaaaaa;--accent:#cc5511;background:var(--bg);color:var(--text)`
        probe.innerHTML = renderMarkdown('```' + language + '\n' + source + '\n```')
        document.body.append(probe)
        await renderMarkdownDiagrams(probe, labels, { themeId: theme })
        const svg = probe.querySelector('svg')!
        const labelColors = Array.from(svg.querySelectorAll('text,tspan,foreignObject .nodeLabel, foreignObject .nodeLabel p'))
          .filter(node => node.textContent?.trim() && !node.children.length)
          .map(node => node.closest('foreignObject') ? getComputedStyle(node).color : getComputedStyle(node).fill)
        const nodeFills = Array.from(svg.querySelectorAll('.node rect')).map(node => getComputedStyle(node).fill)
        results.push({ language, theme, length: svg.outerHTML.length, text: svg.textContent || '', labelColors, nodeFills })
      }
    }
    return results
  })
  expect(result).toHaveLength(8)
  for (const item of result) {
    expect(item.length).toBeGreaterThan(500)
    expect(item.text).toMatch(/开始|客户端|甲/)
    expect(item.labelColors.length).toBeGreaterThan(0)
    for (const color of item.labelColors) {
      const rgb = color.match(/[\d.]+/g)!.slice(0, 3).map(Number)
      const brightness = (rgb[0] + rgb[1] + rgb[2]) / 3
      if (item.theme === 'dark') expect(brightness).toBeGreaterThan(140)
      else expect(brightness).toBeLessThan(140)
    }
  }
  await page.screenshot({ path: `${process.env.TEMP}/opensquilla-diagram-engine-light-dark.png`, fullPage: true })
})

test('palette resolves CSS Color 4 and composites transparent borders over the background', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/diagrams/palette.ts'
    const { diagramPalette } = await import(path)
    const root = document.createElement('div')
    root.style.cssText = '--bg:rgb(32 32 32);--text:color(srgb 0.9 0.9 0.9);--text-muted:color-mix(in srgb, white 60%, black);--border:rgb(255 255 255 / 12%);--accent:color(srgb 1 0.4 0.2)'
    document.body.append(root)
    return diagramPalette(root)
  })
  expect(result.background).toBe('#202020')
  expect(result.text).toBe('#e6e6e6')
  expect(result.muted).toBe('#999999')
  expect(result.border).toBe('#3b3b3b')
  expect(result.accent).toBe('#ff6633')
  expect(result.dark).toBe(true)
})

test('Mermaid math labels preserve readable content or explicitly retain source', async ({ page }) => {
  test.setTimeout(60_000)
  const result = await page.evaluate(async () => {
    const markdownPath = '/src/composables/chat/useChatTextRendering.ts'
    const { useChatTextRendering } = await import(markdownPath)
    const source = 'flowchart LR\nA["$$x^2 + y^2 = z^2$$"] --> B[Done]'
    const root = document.createElement('div')
    root.innerHTML = useChatTextRendering().renderMarkdown('```mermaid\n' + source + '\n```')
    document.body.append(root)
    const path = '/src/utils/markdown/mermaidRenderer.ts'
    const { renderMarkdownDiagrams } = await import(path)
    await renderMarkdownDiagrams(root, {
      zoomIn: 'Zoom in', zoomOut: 'Zoom out', reset: 'Reset', copyCode: 'Copy source',
      copied: 'Copied', copyFailed: 'Copy failed', exportPng: 'Export PNG', exportFailed: 'Export failed',
      loadFailed: 'Load failed', tooLarge: 'Too large', blockedResource: 'Blocked resource', viewSource: 'View source',
      syntaxErrorAt: (line: number, excerpt: string) => `${line}: ${excerpt}`,
    }, { themeId: 'light' })
    return { fallback: root.querySelector('.mermaid-err-src')?.textContent, text: root.querySelector('.mermaid-svg-wrap')?.textContent }
  })
  if (result.fallback) expect(result.fallback).toBe('flowchart LR\nA["$$x^2 + y^2 = z^2$$"] --> B[Done]')
  else { expect(result.text).toContain('x'); expect(result.text).toContain('Done'); expect(result.text).not.toContain('$$') }
})


test('reused diagram SVG keeps IDs and ARIA references local without changing color values', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const corePath = '/src/utils/markdown/renderCore.ts'
    const renderPath = '/src/utils/markdown/mermaidRenderer.ts'
    const { renderMarkdownCore } = await import(corePath)
    const { renderMarkdownDiagrams } = await import(renderPath)
    const labels = {
      zoomIn: 'Zoom in', zoomOut: 'Zoom out', reset: 'Reset', copyCode: 'Copy source',
      copied: 'Copied', copyFailed: 'Copy failed', exportPng: 'Export PNG', exportFailed: 'Export failed',
      loadFailed: 'Load failed', tooLarge: 'Too large', blockedResource: 'Blocked resource', viewSource: 'View source',
      syntaxErrorAt: (line: number, excerpt: string) => `${line}: ${excerpt}`,
    }
    const blocks: HTMLElement[] = []
    for (let index = 0; index < 2; index++) {
      const body = document.createElement('div')
      body.style.width = '500px'
      body.innerHTML = renderMarkdownCore('```mermaid\nflowchart LR\naccTitle: Shared shape\n A --> B\nstyle A fill:#fff,stroke:#333\n```')
      document.body.append(body)
      await renderMarkdownDiagrams(body, labels, { themeId: 'light' })
      blocks.push(body)
    }
    const identifiers = blocks.map(body => Array.from(body.querySelectorAll('[id]')).map(node => node.id))
    const localReferences = blocks.every(body => Array.from(body.querySelectorAll('*')).every(node => {
      const refs = [...(node.getAttribute('aria-labelledby') || '').split(/\s+/), ...(node.getAttribute('aria-describedby') || '').split(/\s+/)]
      for (const attr of Array.from(node.attributes)) for (const match of attr.value.matchAll(/url\(["']?#([^)'" ]+)/g)) refs.push(match[1])
      return refs.filter(Boolean).every(id => body.contains(document.getElementById(id)))
    }))
    return { identifiers, localReferences, fills: blocks.map(body => getComputedStyle(body.querySelector('.node rect')!).fill) }
  })
  expect(result.identifiers[0].length).toBeGreaterThan(0)
  expect(result.identifiers[0].filter(id => result.identifiers[1].includes(id))).toEqual([])
  expect(result.localReferences).toBe(true)
  expect(result.fills).toEqual(['rgb(255, 255, 255)', 'rgb(255, 255, 255)'])
})
