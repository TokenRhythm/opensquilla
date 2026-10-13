// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }))
vi.mock('mermaid', () => ({ default: api }))
vi.mock('dompurify', () => ({ default: () => ({ sanitize: (source: string) => source }) }))
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
beforeEach(() => { vi.resetModules(); api.initialize.mockReset(); api.render.mockReset().mockResolvedValue({ svg }) })
afterEach(() => document.body.replaceChildren())

describe('Mermaid engine ownership', () => {
  it('does not interpret ordinary label text as image attributes or CSS requests', async () => {
    const { hasUnsupportedMermaidImageSources } = await import('./mermaidRenderer')
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A["img: ./cache.png"]')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('sequenceDiagram\n A->>B: img: ./cache.png; url(https://example.com)')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A@{img: "file:///secret", label: "Image"}')).toBe(true)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n A@{label: "Image", img: "https://example.com/a.png"}')).toBe(false)
    expect(hasUnsupportedMermaidImageSources('flowchart TD\n style A fill:url(file:///secret)')).toBe(true)
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
