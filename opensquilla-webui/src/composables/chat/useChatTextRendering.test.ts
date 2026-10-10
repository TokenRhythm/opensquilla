// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { marked } from 'marked'

import { useChatTextRendering } from './useChatTextRendering'

afterEach(() => vi.restoreAllMocks())

describe('useChatTextRendering failure isolation', () => {
  it('keeps the complete real 8 MiB single-line body readable when the Markdown lexer overflows', () => {
    const body = '0123456789abcdef'.repeat(512 * 1024)
    const { renderMarkdown, markdownCacheStats } = useChatTextRendering()
    const html = renderMarkdown(body)
    const host = document.createElement('div')
    host.innerHTML = html
    expect(host.textContent === body).toBe(true)
    expect(host.querySelector('.chat-markdown-plain')).not.toBeNull()
    expect(markdownCacheStats()).toEqual({ entries: 0, bytes: 0 })
    expect(renderMarkdown('**Next message**')).toContain('<strong>Next message</strong>')
  })

  it('falls back to inert original text with line breaks and Unicode, then restores normal highlighting', () => {
    const { renderMarkdown } = useChatTextRendering()
    vi.spyOn(marked, 'parse').mockImplementationOnce(() => { throw new RangeError('parser overflow') })
    const body = '<img src=x onerror="alert(1)">\n<script>alert(2)</script>\n中🙂 & **bold** $x^2$'
    const host = document.createElement('div')
    host.innerHTML = renderMarkdown(body, { highlight: false })
    expect(host.textContent).toBe(body)
    expect(host.querySelector('img,script')).toBeNull()
    expect(host.textContent).not.toContain('\uE000')
    expect(renderMarkdown('```js\nconst answer = 42\n```')).toContain('hljs-keyword')
  })
})

// URI sanitizer regressions run against the real browser DOM in
// e2e/markdown-reference-safety.spec.ts; happy-dom's Node.prototype.nodeName
// getter does not implement the browser semantics required by DOMPurify.

describe('useChatTextRendering math', () => {
  it('renders inline and display LaTeX with KaTeX', () => {
    const { renderMarkdown } = useChatTextRendering()

    const inline = renderMarkdown('Inline $x^2$ formula')
    const display = renderMarkdown('Block:\n\n$$\\frac{a}{b}$$')

    expect(inline).toContain('class="katex"')
    expect(inline).not.toContain('$x^2$')
    expect(display).toContain('class="katex-display"')
    expect(display).not.toContain('$$\\frac{a}{b}$$')
  })
})

describe('useChatTextRendering cache bounds', () => {
  it('never retains growing live prefixes', () => {
    const { renderMarkdown, markdownCacheStats } = useChatTextRendering()
    for (let index = 1; index <= 200; index += 1) {
      renderMarkdown('x'.repeat(index), {
        highlight: false,
        cache: 'none',
        math: 'defer',
      })
    }
    expect(markdownCacheStats()).toEqual({ entries: 0, bytes: 0 })
  })

  it('bounds settled entries by retained bytes and skips oversized items', () => {
    const { renderMarkdown, markdownCacheStats } = useChatTextRendering()
    for (let index = 0; index < 60; index += 1) {
      renderMarkdown(`${index}\n${'x'.repeat(40 * 1024)}`)
    }
    expect(markdownCacheStats().bytes).toBeLessThanOrEqual(8 * 1024 * 1024)
    expect(markdownCacheStats().entries).toBeGreaterThan(0)

    const before = markdownCacheStats()
    renderMarkdown('y'.repeat(300 * 1024))
    expect(markdownCacheStats()).toEqual(before)
  })
})

describe('useChatTextRendering protocol-shaped literals', () => {
  const cases = [
    {
      name: 'inline tool_calls marker',
      text: 'Document the literal `<tool_calls>` marker and keep this suffix.',
      suffix: 'marker and keep this suffix.',
    },
    {
      name: 'fenced tool protocol example',
      text: [
        'Example payload:',
        '```xml',
        '<tool_calls><invoke name="demo"><parameter name="path">x</parameter></invoke></tool_calls>',
        '```',
        'After the fenced example.',
      ].join('\n'),
      suffix: 'After the fenced example.',
    },
    {
      name: 'DSML marker in inline code',
      text: 'Keep `<｜DSML｜tool_calls><｜DSML｜invoke name="demo">` as documentation, then continue.',
      suffix: 'as documentation, then continue.',
    },
    {
      name: 'ordinary details disclosure',
      text: [
        '<details><summary>View areas around line 10</summary>',
        'Visible note.',
        '</details>',
        '',
        'After the details block.',
      ].join('\n'),
      suffix: 'After the details block.',
    },
  ]

  for (const testCase of cases) {
    it(`renders ${testCase.name} without truncating the suffix`, () => {
      const { renderMarkdown } = useChatTextRendering()
      const host = document.createElement('div')

      host.innerHTML = renderMarkdown(testCase.text)

      expect(host.textContent).toContain(testCase.suffix)
    })

    it(`copies ${testCase.name} without truncation`, () => {
      const { sanitizeCopyText } = useChatTextRendering()

      expect(sanitizeCopyText(testCase.text)).toBe(testCase.text)
    })
  }
})

describe('useChatTextRendering goal status markers', () => {
  it('renders and copies a literal goal-looking line as ordinary user-visible text', () => {
    const { renderMarkdown, sanitizeCopyText, stripGeneratedArtifactMarkers } = useChatTextRendering()
    const text = 'The work is complete.\n[goal:complete]\n'

    expect(renderMarkdown(text)).toContain('[goal:complete]')
    expect(sanitizeCopyText(text)).toBe('The work is complete.\n[goal:complete]')
    expect(stripGeneratedArtifactMarkers(text)).toBe(text)
  })

  it('does not remove a goal-looking line from the middle of a transcript', () => {
    const { stripGeneratedArtifactMarkers } = useChatTextRendering()
    const text = 'Example:\n[goal:complete]\nThen continue.'

    expect(stripGeneratedArtifactMarkers(text)).toBe(text)
  })
})

describe('useChatTextRendering silent sentinel copy projection', () => {
  it('removes only assistant boundary marker lines from copied text', () => {
    const { sanitizeCopyText } = useChatTextRendering()

    expect(sanitizeCopyText('NO_REPLY\nVisible answer.\nHEARTBEAT_OK', {
      provenance: { runKind: 'goal' },
    }))
      .toBe('Visible answer.')
    expect(sanitizeCopyText('NO_REPLY\nVisible answer.\nHEARTBEAT_OK'))
      .toBe('NO_REPLY\nVisible answer.\nHEARTBEAT_OK')
    expect(sanitizeCopyText('Before\nNO_REPLY\nAfter')).toBe('Before\nNO_REPLY\nAfter')
    expect(sanitizeCopyText(['```text', 'NO_REPLY', '```'].join('\n')))
      .toBe(['```text', 'NO_REPLY', '```'].join('\n'))
  })
})
