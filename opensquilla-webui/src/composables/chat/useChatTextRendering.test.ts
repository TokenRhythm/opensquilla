// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest'

import { useChatTextRendering } from './useChatTextRendering'

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
