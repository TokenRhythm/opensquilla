// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { marked, Parser } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import { createMarkdownRenderBudget, renderMarkdownCore } from './renderCore'
import { renderArtifactMarkdown } from '@/utils/workbench/artifactPreview'

afterEach(() => vi.restoreAllMocks())
const fence = (source: string, language = 'js') => `\x60\x60\x60${language}\n${source}\n\x60\x60\x60`
function host(html: string): HTMLDivElement {
  const root = document.createElement('div')
  root.innerHTML = html
  return root
}
function codeTexts(html: string): string[] {
  return [...host(html).querySelectorAll('code')].map(code => code.textContent || '')
}

describe('shared Markdown work bounds', () => {
  it('bounds cumulative code characters in artifact previews and preserves all blocks', () => {
    const highlight = vi.spyOn(hljs, 'highlight').mockImplementation(code => ({ value: String(code) }) as ReturnType<typeof hljs.highlight>)
    const sources = Array.from({ length: 8 }, (_, index) => `// ${index}\n${'x'.repeat(28_000)}`)
    expect(codeTexts(renderArtifactMarkdown(sources.map(source => fence(source)).join('\n\n')))).toEqual(sources)
    expect(highlight).toHaveBeenCalledTimes(3)
  })

  it('counts failed highlight attempts and shares a budget across streaming commits', () => {
    const highlight = vi.spyOn(hljs, 'highlight').mockImplementation(() => { throw new Error('highlight failed') })
    const budget = createMarkdownRenderBudget()
    for (let index = 0; index < 40; index++) {
      const source = `const value${index} = ${index}`
      expect(codeTexts(renderMarkdownCore(fence(source), { budget }))).toEqual([source])
    }
    expect(highlight).toHaveBeenCalledTimes(32)
    renderMarkdownCore(fence('const nextMessage = 1'))
    expect(highlight).toHaveBeenCalledTimes(33)
  })

  it('bounds formulas across commits and retains source after exhaustion', () => {
    const math = vi.spyOn(katex, 'renderToString')
    const budget = createMarkdownRenderBudget()
    budget.mathExpressionsRemaining = 1
    expect(renderMarkdownCore('$x$', { budget })).toContain('class="katex"')
    const body = '$y^2$'
    expect(host(renderMarkdownCore(body, { budget })).textContent?.trim()).toBe(body)
    expect(math).toHaveBeenCalledTimes(1)
    expect(math).toHaveBeenCalledWith('x', expect.objectContaining({ trust: false, maxExpand: 1000, maxSize: 100 }))
  })

  it('leaves fenced, indented and inline code math literal', () => {
    const math = vi.spyOn(katex, 'renderToString')
    const source = ['`$inline$`', '', fence('$fenced$', 'text'), '', '~~~text', '$tilde$', '~~~', '', '    $indented$', '', '$real$'].join('\n')
    const root = host(renderMarkdownCore(source))
    expect([...root.querySelectorAll('code')].map(code => code.textContent)).toEqual(['$inline$', '$fenced$', '$tilde$', '$indented$'])
    expect(math).toHaveBeenCalledTimes(1)
    expect(math.mock.calls[0]?.[0]).toBe('real')
  })

  it('keeps complete formula source when a formula or scan exceeds its budget', () => {
    const math = vi.spyOn(katex, 'renderToString')
    const large = `$\\text{${'a_b'.repeat(6000)}}$`
    expect(host(renderMarkdownCore(large)).textContent).toBe(large)
    const budget = createMarkdownRenderBudget()
    budget.mathScansRemaining = 0
    const source = '**prefix** $\\frac{a_b}{c}$'
    expect(host(renderMarkdownCore(source, { budget })).textContent).toBe(source)
    expect(math).not.toHaveBeenCalled()
  })

  it('does not downgrade long ordinary prose because of one unmatched dollar', () => {
    const source = '# Heading\n\n$PATH ' + 'ordinary prose '.repeat(2000) + '**closing emphasis**'
    const html = renderMarkdownCore(source)
    expect(html).not.toContain('chat-markdown-plain')
    expect(html).toContain('<strong>closing emphasis</strong>')
    expect(host(html).textContent).toContain('$PATH')
  })
})

describe('Markdown state and failure isolation', () => {
  it('does not configure the global parser or purifier used by unrelated surfaces', () => {
    const beforeMarkdown = marked.parse('~approximate~', { async: false })
    const beforePurify = DOMPurify.sanitize('<span class="other-surface">content</span>')
    renderMarkdownCore('~~removed~~ $x$')
    expect(marked.parse('~approximate~', { async: false })).toBe(beforeMarkdown)
    expect(DOMPurify.sanitize('<span class="other-surface">content</span>')).toBe(beforePurify)
  })

  it('keeps complete escaped artifact source on parser failure, then recovers', () => {
    vi.spyOn(Parser, 'parse').mockImplementationOnce(() => { throw new RangeError('parser overflow') })
    const source = '# Notes\n<script>danger()</script>\n中🙂 & $x$'
    const root = host(renderArtifactMarkdown(source))
    expect(root.textContent).toBe(source)
    expect(root.querySelector('script')).toBeNull()
    expect(root.querySelector('.chat-markdown-plain')).not.toBeNull()
    expect(renderMarkdownCore('**next**')).toContain('<strong>next</strong>')
  })

  it('does not turn source-controlled private-use markers into generated math', () => {
    const source = '\uE000M0\uE001 and &#xE000;M0&#xE001; $x$'
    const root = host(renderMarkdownCore(source))
    expect(root.textContent).toContain('\uE000M0\uE001 and \uE000M0\uE001')
    expect(root.querySelectorAll('.katex')).toHaveLength(1)
  })
})
