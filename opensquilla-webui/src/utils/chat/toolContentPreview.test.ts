import { describe, expect, it } from 'vitest'
import { boundedTextPreview, boundedTextPreviews, TOOL_CONTENT_PREVIEW_MAX_BYTES, TOOL_CONTENT_PREVIEW_MAX_LINE_BYTES } from './toolContentPreview'

describe('bounded tool text previews', () => {
  it('preserves an empty file, BOM and CRLF without rewriting text', () => {
    for (const content of ['', '\uFEFFhello\r\nworld\r\n', 'a\rb']) {
      expect(boundedTextPreview(content)).toEqual({ content, truncated: false })
    }
  })
  it('stops at 1000 lines instead of scanning the full body', () => {
    const body = 'a\r\n'.repeat(1000)
    expect(boundedTextPreview(body)).toEqual({ content: body, truncated: false })
    expect(boundedTextPreview(body + 'tail')).toEqual({ content: body, truncated: true })
  })
  it('caps a single huge line at 8 KiB and keeps complete Unicode points', () => {
    const body = 'a'.repeat(TOOL_CONTENT_PREVIEW_MAX_LINE_BYTES - 2) + '🙂tail'
    expect(boundedTextPreview(body)).toEqual({ content: body.slice(0, -6), truncated: true })
    expect(boundedTextPreview('中'.repeat(3000)).content).toBe('中'.repeat(2730))
  })
  it('caps total UTF-8 bytes and never splits CRLF at the boundary', () => {
    const line = 'x'.repeat(4095) + '\n'
    const exact = line.repeat(TOOL_CONTENT_PREVIEW_MAX_BYTES / 4096)
    expect(boundedTextPreview(exact)).toEqual({ content: exact, truncated: false })
    expect(boundedTextPreview(exact + 'tail')).toEqual({ content: exact, truncated: true })
    const boundary = exact.slice(0, -1) + '\r\ntail'
    expect(boundedTextPreview(boundary).content).toBe(exact.slice(0, -1))
  })
  it('shares bytes and lines between card sections without joining full input', () => {
    const first = ('x'.repeat(4095) + '\n').repeat(32)
    const result = boundedTextPreviews([first, first + 'tail', 'later'])
    expect(result).toEqual({ previews: [{ content: first, truncated: false },
      { content: first, truncated: true }, { content: '', truncated: true }], truncated: true })
    const lines = boundedTextPreviews(['line\n'.repeat(999) + 'last', 'next'])
    expect(lines.previews[1]).toEqual({ content: '', truncated: true })
  })
})
