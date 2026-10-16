import { describe, expect, it } from 'vitest'
import { completeWorkspaceMarkdown, isWorkspaceMarkdown, WORKSPACE_MARKDOWN_PREVIEW_LIMIT } from './workspaceTextPreview'

describe('bounded whole-document Markdown preview', () => {
  it('recognizes document names and MIME, preserving code/config as source', () => {
    for (const name of ['README.md', 'REPORT.MARKDOWN', 'notes.mdown', 'notes.mkd']) {
      expect(isWorkspaceMarkdown({ name, mime: 'text/plain' })).toBe(true)
    }
    expect(isWorkspaceMarkdown({ name: 'notes', mime: 'text/markdown; charset=utf-8' })).toBe(true)
    for (const name of ['app.ts', 'settings.json', 'page.html']) expect(isWorkspaceMarkdown({ name, mime: 'text/plain' })).toBe(false)
  })
  it('keeps the entire document beyond 200 lines with Unicode line boundaries', async () => {
    const content = '# Report\r\n' + '中文\u2028'.repeat(400)
    expect(await completeWorkspaceMarkdown('report.md', new Blob([content]))).toEqual({
      relativePath: 'report.md', content, startLine: 1, endLine: 401, totalLines: 401,
    })
  })
  it('rejects oversized, binary and invalid UTF-8 payloads instead of rendering a partial/replaced document', async () => {
    await expect(completeWorkspaceMarkdown('large.md', new Blob(['x'.repeat(WORKSPACE_MARKDOWN_PREVIEW_LIMIT + 1)]))).rejects.toThrow('tooLarge')
    await expect(completeWorkspaceMarkdown('binary.md', new Blob(['a\0b']))).rejects.toThrow('unsupported')
    await expect(completeWorkspaceMarkdown('invalid.md', new Blob([new Uint8Array([0xff])]))).rejects.toThrow()
  })
})
