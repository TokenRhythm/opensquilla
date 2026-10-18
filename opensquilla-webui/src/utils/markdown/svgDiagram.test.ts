// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSvgDiagram, diagramExportSize, readDiagramViewBox, sanitizeDiagramSvg } from './svgDiagram'
import { isSafeMarkdownImageSource, isSafeMarkdownLinkSource } from './markdownUris'
import { copyTextWithFallback } from '@/utils/browser'

// happy-dom strips a bare SVG root in DOMPurify's native namespace path.
// Chrome e2e covers that integration; these tests isolate our resource gates
// and interaction/dimension code from the emulated purifier implementation.
vi.mock('dompurify', () => ({ default: () => ({ sanitize: (source: string) => source }) }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn() }))

const labels = { zoomIn: '+', zoomOut: '-', reset: 'Reset', copyCode: 'Copy', copyFailed: 'Failed', exportPng: 'PNG', exportFailed: 'Export failed' }
beforeEach(() => vi.mocked(copyTextWithFallback).mockReset().mockResolvedValue(undefined))
afterEach(() => { document.body.replaceChildren(); vi.clearAllTimers(); vi.useRealTimers() })

describe('generated SVG resource contract', () => {
  it('preserves HTML labels and internal references while removing active/custom resources', () => {
    const cleaned = sanitizeDiagramSvg(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <defs><marker id="arrow"><path d="M0 0L2 2"/></marker></defs>
      <path marker-end="url(#arrow)"/>
      <foreignObject><div xmlns="http://www.w3.org/1999/xhtml"><b>Readable label</b><img src="file:///secret" onerror="alert(1)"/></div></foreignObject>
      <a href="opensquilla://open"><text>Custom</text></a><a href="https://example.com"><text>Docs</text></a><script>alert(1)</script></svg>`)
    const root = document.createElement('div')
    root.innerHTML = cleaned
    expect(root.textContent).toContain('Readable label')
    expect(root.querySelector('foreignObject')).not.toBeNull()
    expect(root.querySelector('script, img, [onerror]')).toBeNull()
    expect(root.querySelector('a[href="opensquilla://open"]')).toBeNull()
    expect(root.querySelector('a[href="https://example.com"]')?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(root.querySelector('[marker-end]')?.getAttribute('marker-end')).toBe('url(#arrow)')
  })
  it('uses one explicit URL policy for public images and outbound links', () => {
    expect(isSafeMarkdownImageSource('https://example.com/a.png')).toBe(true)
    expect(isSafeMarkdownImageSource('data:image/png;base64,iVBORw0KGgo=')).toBe(true)
    for (const src of ['http://example.com/a.png', '/api/private', 'file:///a', 'data:image/svg+xml;base64,PHN2Zy8+', 'https:\n//example.com']) expect(isSafeMarkdownImageSource(src)).toBe(false)
    expect(isSafeMarkdownLinkSource('mailto:a@example.com')).toBe(true)
    expect(isSafeMarkdownLinkSource('http://example.com')).toBe(true)
    expect(isSafeMarkdownLinkSource('opensquilla://open')).toBe(false)
  })
  it('preserves Mermaid xlink navigation while keeping rejected targets inert', () => {
    const cleaned = sanitizeDiagramSvg('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 80"><a xlink:href="https://example.com/docs"><text>Docs</text></a><a xlink:href="file:///secret"><text>Local</text></a></svg>')
    const root = document.createElement('div')
    root.innerHTML = cleaned
    const safe = [...root.querySelectorAll('a')].find(anchor => anchor.getAttribute('xlink:href') === 'https://example.com/docs')
    expect(safe?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(safe?.getAttribute('target')).toBe('_blank')
    expect(safe?.getAttribute('xlink:href')).toBe('https://example.com/docs')
    expect(safe?.hasAttribute('href')).toBe(false)
    expect(root.querySelector('a[href^="file:"]')).toBeNull()
    expect([...root.querySelectorAll('a')].some(anchor => /^file:/.test(anchor.getAttribute('xlink:href') || ''))).toBe(false)
  })
})

describe('diagram dimensions and retained source viewport', () => {
  it('copies the retained source with a temporary check while keeping its action name', async () => {
    vi.useFakeTimers()
    const diagram = createSvgDiagram('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"/>', 'raw source', labels)
    document.body.append(diagram.element)
    const button = diagram.element.querySelector<HTMLButtonElement>('button[title="Copy"]')!
    button.click()
    await Promise.resolve()
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('raw source')
    expect(button.querySelector('polyline')).not.toBeNull()
    expect(button.title).toBe('Copy')
    expect(button.getAttribute('aria-label')).toBe('Copy')
    expect(button.textContent).toBe('')
    vi.advanceTimersByTime(1500)
    expect(button.querySelector('polyline')).toBeNull()
    expect(button.title).toBe('Copy')
    diagram.dispose()
  })
  it('ignores an older failure and completions for a replaced toolbar', async () => {
    const diagram = createSvgDiagram('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"/>', 'raw source', labels)
    document.body.append(diagram.element)
    const button = diagram.element.querySelector<HTMLButtonElement>('button[title="Copy"]')!
    let rejectFirst!: (error: Error) => void
    vi.mocked(copyTextWithFallback).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    button.click()
    button.click()
    await Promise.resolve()
    rejectFirst(new Error('late failure'))
    await Promise.resolve(); await Promise.resolve()
    expect(button.querySelector('polyline')).not.toBeNull()
    expect(button.getAttribute('aria-label')).toBe('Copy')
    let resolveCopy!: () => void
    vi.mocked(copyTextWithFallback).mockImplementationOnce(() => new Promise<void>(resolve => { resolveCopy = resolve }))
    button.click()
    diagram.updateLabels({ ...labels, copyCode: '复制源码' })
    resolveCopy()
    await Promise.resolve()
    const replacement = diagram.element.querySelector<HTMLButtonElement>('button[title="复制源码"]')!
    expect(replacement).not.toBe(button)
    expect(replacement.querySelector('polyline')).toBeNull()
    expect(replacement.getAttribute('aria-label')).toBe('复制源码')
    diagram.dispose()
  })
  it('bounds both axes and total output pixels for very tall diagrams', () => {
    for (const [w, h] of [[800, 200], [20, 90_000], [90_000, 20], [50_000, 50_000]]) {
      const result = diagramExportSize(w, h)
      expect(result.width).toBeGreaterThan(0)
      expect(result.height).toBeGreaterThan(0)
      expect(result.width).toBeLessThanOrEqual(4096)
      expect(result.height).toBeLessThanOrEqual(4096)
      expect(result.width * result.height).toBeLessThanOrEqual(4_000_000)
    }
    expect(() => diagramExportSize(Infinity, 10)).toThrow()
    expect(() => diagramExportSize(10, 0)).toThrow()
  })
  it('updates localized toolbar labels while preserving zoom and restores the original viewBox', () => {
    const diagram = createSvgDiagram('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><rect width="100" height="80"/></svg>', 'raw source', labels)
    document.body.appendChild(diagram.element)
    const svg = diagram.element.querySelector('svg')!
    ;(diagram.element.querySelector('button') as HTMLButtonElement).click()
    expect(svg.getAttribute('viewBox')).not.toBe('0 0 100 80')
    const zoomed = svg.getAttribute('viewBox')
    diagram.updateLabels({ ...labels, reset: 'Reset translated' })
    expect(svg.getAttribute('viewBox')).toBe(zoomed)
    ;(diagram.element.querySelector('button[title="Reset translated"]') as HTMLButtonElement).click()
    expect(readDiagramViewBox(svg)).toEqual({ x: 0, y: 0, w: 100, h: 80 })
    diagram.dispose()
  })
})


describe('repeated and cached SVG identity', () => {
  it('namespaces fragments, style selectors and accessible references per mount', () => {
    const source = '<svg xmlns="http://www.w3.org/2000/svg" id="fff" viewBox="0 0 100 80" aria-labelledby="title"><title id="title">Same title</title><defs><marker id="arrow"><path d="M0 0L2 2"/></marker></defs><path marker-end="url(#arrow)"/><a href="#title"><text>Same text</text></a></svg>'
    const first = createSvgDiagram(source, 'source', labels).element
    const second = createSvgDiagram(source, 'source', labels).element
    document.body.append(first, second)
    const ids = [...document.querySelectorAll('[id]')].map(node => node.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const root of [first, second]) {
      const titleId = root.querySelector('title')!.id
      const arrowId = root.querySelector('marker')!.id
      expect(root.querySelector('svg')?.getAttribute('aria-labelledby')).toBe(titleId)
      expect(root.querySelector('a')?.getAttribute('href')).toBe(`#${titleId}`)
      expect(root.querySelector('[marker-end]')?.getAttribute('marker-end')).toBe(`url(#${arrowId})`)
      expect(root.textContent).toContain('Same text')
    }
  })
})
