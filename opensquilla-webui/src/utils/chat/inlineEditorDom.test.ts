// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { readInlineEditor } from './inlineEditorDom'

describe('plain-text coordinates for browser contenteditable line DOM', () => {
  it.each([
    ['first<div><br></div><div>second</div>', 'first\n\nsecond'],
    ['first<div><br></div><div><br></div><div>second</div>', 'first\n\n\nsecond'],
    ['<div><br></div><div>second</div>', '\nsecond'],
    ['first<div>second</div>', 'first\nsecond'],
    ['first<div><br></div>', 'first\n'],
    ['first<br><br>', 'first\n'],
    ['<br>', ''],
  ])('preserves blank lines without serializing an editing placeholder: %s', (html, text) => {
    const editor = document.createElement('div')
    editor.innerHTML = html
    expect(readInlineEditor(editor).text).toBe(text)
  })

  it('keeps capability and caret coordinates after an empty line', () => {
    const editor = document.createElement('div')
    editor.innerHTML = '甲<div><br></div><div><button data-skill-id="visual">Visualize</button>乙</div>'
    const snapshot = readInlineEditor(editor)
    expect(snapshot.text).toBe('甲\n\n乙')
    expect(snapshot.positions).toEqual({ visual: 3 })
    expect(snapshot.offsets.get(editor.lastChild!.lastChild!)).toEqual([3, 4])
  })
})
