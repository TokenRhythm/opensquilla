import { describe, expect, it } from 'vitest'

import {
  codePointOffsetFromUtf16,
  codePointRangeFromUtf16,
  htmlElementAtOffsets,
  htmlSourceElements,
  minimalSourcePatch,
} from './htmlSourceModel'

describe('HTML source model', () => {
  it('keeps parse5 source locations and finds the narrowest canonical element', () => {
    const source = '<main><section id="hero"><h1 class="title big">Hello</h1></section></main>'
    const elements = htmlSourceElements(source)
    expect(elements.map(element => element.label)).toEqual([
      'main',
      'section#hero',
      'h1.title.big',
    ])
    const cursor = source.indexOf('Hello') + 2
    expect(htmlElementAtOffsets(elements, cursor)?.label).toBe('h1.title.big')
    const sectionStart = source.indexOf('<section')
    const sectionEnd = source.indexOf('</section>') + '</section>'.length
    expect(htmlElementAtOffsets(elements, sectionStart, sectionEnd)?.label).toBe(
      'section#hero',
    )
  })

  it('produces one minimal offset patch and no patch for a no-op', () => {
    expect(minimalSourcePatch('hello world', 'hello brave world')).toEqual({
      startOffset: 6,
      endOffset: 6,
      replacement: 'brave ',
    })
    expect(minimalSourcePatch('same', 'same')).toBeNull()
  })

  it('uses Unicode code-point wire offsets around non-BMP characters', () => {
    const before = '<p>😀 old</p>'
    const after = '<p>😀 new</p>'
    expect(minimalSourcePatch(before, after)).toEqual({
      // Python sees the emoji as one character even though JavaScript and
      // Monaco store it as a two-unit surrogate pair.
      startOffset: 5,
      endOffset: 8,
      replacement: 'new',
    })
    expect(codePointRangeFromUtf16(before, 3, before.length)).toEqual({
      startOffset: 3,
      endOffset: 12,
    })
    expect(() => codePointOffsetFromUtf16(before, 4)).toThrow(/surrogate pair/)
  })
})
