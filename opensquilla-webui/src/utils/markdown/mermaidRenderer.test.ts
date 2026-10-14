// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn(), graphviz: vi.fn() }))
vi.mock('./diagrams/graphviz', () => ({ renderGraphviz: api.graphviz }))
vi.mock('mermaid', () => ({ default: api }))
vi.mock('dompurify', () => ({ default: () => ({ sanitize: (source: string) => source, addHook: vi.fn() }) }))
vi.mock('@/themes/registry', () => ({ getManifest: (id: string) => ({ capabilities: { colorScheme: id } }) }))
const labels = { zoomIn: '+', zoomOut: '-', reset: 'Reset', copyCode: 'Copy', copied: 'Copied', copyFailed: 'Failed', exportPng: 'PNG', exportFailed: 'Export failed', loadFailed: 'Load failed', tooLarge: 'Too large', blockedResource: 'Blocked resource', viewSource: 'Source', syntaxErrorAt: (line: number) => `Line ${line}` }
const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><text>Rendered</text></svg>'
function fixture(source = 'flowchart TD\n A-->B') {
  const root = document.createElement('div')
  const pre = document.createElement('pre')
  const code = document.createElement('code')
  code.className = 'language-mermaid'
  code.textContent = source
  pre.appendChild(code)
  root.appendChild(pre)
  document.body.appendChild(root)
  return root
}
beforeEach(() => { vi.resetModules(); api.initialize.mockReset(); api.render.mockReset().mockResolvedValue({ svg }); api.graphviz.mockReset().mockResolvedValue(svg) })
afterEach(() => document.body.replaceChildren())

describe('Mermaid engine ownership', () => {
  it('does not interpret ordinary label text as image attributes or CSS requests', async () => {
    const { hasUnsupportedMermaidImageSources } = await import('./mermaidRenderer')
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A["img: ./cache.png"]')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('sequenceDiagram\n A->>B: img: ./cache.png; url(https://example.com)')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A@{img: "file:///secret", label: "Image"}')).toBe(true)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A@{label: "Image", img: "https://example.com/a.png"}')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n style A fill:url(file:///secret)')).toBe(true)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A["<img src=https://example.com/a.png onerror=alert(1)>"]')).toBe(true)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A["<b>Public image</b><img src=https://example.com/a.png>"]')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A["<iframe src=https://example.com>"]')).toBe(true)
  })
  it.each([
    '<img src="file&#58;///secret">',
    '<img src="https://example.com/a.png" srcset="https://example.com/b.png 2x">',
    '<IMG SRC="https://example.com/a.png" oNeRrOr="alert(1)">',
    '<svg><image xlink:href="file&#x3a;///secret"/></svg>',
    '<svg><foreignObject><div onload="alert(1)">label</div></foreignObject></svg>',
    '<template><img src="file:///secret"></template>',
    '<b style="background: url(&quot;file:///secret&quot;)">label</b>',
    '<style>@import "https://example.com/style.css";</style>',
    '<script>fetch("https://example.com")</script>',
    '<form action="https://example.com"><input></form>',
  ])('rejects unsafe HTML before an engine can attach it: %s', async (markup) => {
    const { renderMermaidBlocks } = await import('./mermaidRenderer')
    const root = fixture(`flowchart TD\n A["${markup}"]`)
    await renderMermaidBlocks(root, labels)
    expect(root.querySelector('.mermaid-error')?.textContent).toContain('Blocked resource')
    expect(root.querySelector('.mermaid-err-src')?.textContent).toContain(markup)
    expect(api.render).not.toHaveBeenCalled()
  })
  it.each([
    '<b>Public image</b><img src="https&#58;//example.com/a.png">',
    '<svg><image xlink:href="https://example.com/a.png"/></svg>',
    '<b style="fill:url(#local)">label</b>',
    '<!-- <img src="file:///secret" onerror="alert(1)"> -->ordinary label',
    '&lt;img src="file:///secret"&gt;',
  ])('preserves safe HTML parsing and entity semantics: %s', async (markup) => {
    const { hasUnsupportedMermaidImageSources } = await import('./mermaidRenderer')
    expect(hasUnsupportedMermaidImageSources(`flowchart TD\n A["${markup}"]`)).toBe(false)
  })
  it('checks source without creating browser nodes or parsing it into a DOM', async () => {
    const { hasUnsupportedMermaidImageSources } = await import('./mermaidRenderer')
    const create = vi.spyOn(document, 'createElement')
    const parse = vi.spyOn(DOMParser.prototype, 'parseFromString')
    try {
      expect(hasUnsupportedMermaidImageSources('<img src="file:///secret">')).toBe(true)
      expect(hasUnsupportedMermaidImageSources('<b>Safe label</b>')).toBe(false)
      expect(create).not.toHaveBeenCalled()
      expect(parse).not.toHaveBeenCalled()
    } finally {
      create.mockRestore()
      parse.mockRestore()
    }
  })
  it('serializes theme initialization with the entire preceding render', async () => {
    const { renderMermaidBlocks } = await import('./mermaidRenderer')
    let finish!: (value: { svg: string }) => void
    api.render.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const first = renderMermaidBlocks(fixture(), labels, { themeId: 'light' })
    const second = renderMermaidBlocks(fixture(), labels, { themeId: 'dark' })
    await vi.waitFor(() => expect(api.render).toHaveBeenCalledTimes(1))
    expect(api.initialize).toHaveBeenCalledTimes(1)
    expect(api.initialize.mock.calls[0][0].theme).toBe('default')
    finish({ svg })
    await Promise.all([first, second])
    expect(api.initialize.mock.calls[1][0].theme).toBe('dark')
    expect(document.querySelector('[data-mermaid-host]')).toBeNull()
  })
  it('removes an aborted host immediately and never commits a late result', async () => {
    const { renderMermaidBlocks } = await import('./mermaidRenderer')
    let finish!: (value: { svg: string }) => void
    api.render.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const root = fixture()
    const controller = new AbortController()
    const pending = renderMermaidBlocks(root, labels, { signal: controller.signal })
    await vi.waitFor(() => expect(api.render).toHaveBeenCalledTimes(1))
    expect(document.querySelector('[data-mermaid-host]')).not.toBeNull()
    controller.abort()
    expect(document.querySelector('[data-mermaid-host]')).toBeNull()
    finish({ svg })
    await pending
    expect(root.querySelector('.mermaid-diagram')).toBeNull()
    expect(root.querySelector('code')?.textContent).toContain('A-->B')
    expect(root.querySelector('[data-mermaid-state]')).toBeNull()
  })
  it('skips detached nodes before submitting any engine work', async () => {
    const { renderMermaidBlocks } = await import('./mermaidRenderer')
    const root = fixture()
    root.remove()
    await renderMermaidBlocks(root, labels)
    expect(api.render).not.toHaveBeenCalled()
  })
  it('keeps oversized or private-image source readable without executing it', async () => {
    const { renderMermaidBlocks } = await import('./mermaidRenderer')
    const huge = fixture('flowchart TD\n' + 'x'.repeat(50_000))
    const local = fixture('flowchart TD\n A@{img: "file:///secret", label:"Local"}')
    await renderMermaidBlocks(huge, labels)
    await renderMermaidBlocks(local, labels)
    expect(huge.querySelector('.mermaid-error')?.textContent).toContain('Too large')
    expect(local.querySelector('.mermaid-err-src')?.textContent).toContain('file:///secret')
    expect(api.render).not.toHaveBeenCalled()
  })
})


describe('shared diagram engine integration', () => {
  it('renders DOT aliases through the existing SVG controls and cache', async () => {
    const { renderMarkdownDiagrams } = await import('./mermaidRenderer')
    for (const language of ['dot', 'graphviz']) {
      const root = fixture('digraph { a -> b }')
      root.querySelector('code')!.className = `language-${language}`
      await renderMarkdownDiagrams(root, labels, { themeId: 'light' })
      expect(root.querySelector('[data-diagram-language]')?.getAttribute('data-diagram-language')).toBe(language)
      expect(root.querySelector('.mermaid-svg-wrap svg')).not.toBeNull()
      expect(root.querySelectorAll('.mermaid-toolbar button')).toHaveLength(5)
      expect(root.querySelector('[aria-label="PNG"]')).not.toBeNull()
    }
    expect(api.graphviz).toHaveBeenCalledOnce()
    expect(api.render).not.toHaveBeenCalled()
  })

  it('restores only clone source, retaining engine and live interaction state', async () => {
    const { renderMarkdownDiagrams, restoreClonedDiagramSources } = await import('./mermaidRenderer')
    const root = fixture('digraph { a -> b }')
    root.querySelector('code')!.className = 'language-dot'
    await renderMarkdownDiagrams(root, labels)
    const live = root.querySelector('.mermaid-diagram')!
    const clone = root.cloneNode(true) as HTMLElement
    restoreClonedDiagramSources(root, clone)
    expect(root.querySelector('.mermaid-diagram')).toBe(live)
    expect(clone.querySelector('.mermaid-diagram')).toBeNull()
    expect(clone.querySelector('pre code.language-dot')?.textContent).toBe('digraph { a -> b }')
  })

  it('keeps bounded DOT source on failure and never invokes the Mermaid engine', async () => {
    const { renderMarkdownDiagrams } = await import('./mermaidRenderer')
    const source = 'digraph { { a b } -> { c d } }'
    const root = fixture(source)
    root.querySelector('code')!.className = 'language-dot'
    await renderMarkdownDiagrams(root, labels)
    expect(root.querySelector('.mermaid-err-src')?.textContent).toBe(source)
    expect(root.querySelector('[data-diagram-language="dot"]')).not.toBeNull()
    expect(api.graphviz).not.toHaveBeenCalled()
    expect(api.render).not.toHaveBeenCalled()
  })

  it('counts retried admissions once and rejects oversized surfaces before engine work', async () => {
    const { renderMarkdownDiagrams } = await import('./mermaidRenderer')
    const root = fixture()
    for (let index = 1; index < 33; index++) root.append(root.firstElementChild!.cloneNode(true))
    const budget = { sourceChars: 0, diagrams: 0 }
    await renderMarkdownDiagrams(root, labels, { budget })
    expect(root.querySelectorAll('.mermaid-error')).toHaveLength(33)
    expect(budget.diagrams).toBe(33)
    expect(api.render).not.toHaveBeenCalled()
  })
})
