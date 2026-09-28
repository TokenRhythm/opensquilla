import { describe, expect, it, vi } from 'vitest'
import { createV4WorkspaceFiles } from './workspaceFilesV4'

function fixture() {
  const file = { requestedPath: 'outputs/中文 图.svg', path: 'outputs/中文 图.svg', name: '中文 图.svg',
    mime: 'image/svg+xml', size: 42, kind: 'text', workspaceBinding: 'binding-A', contentUrl: 'https://untrusted.test/leak' }
  const http = { requestJson: vi.fn().mockResolvedValue({ workspaceBinding: 'binding-A', files: [file] }),
    requestBlob: vi.fn().mockResolvedValue(new Blob(['<svg/>'], { type: 'image/svg+xml' })) }
  return { file, http, access: createV4WorkspaceFiles(http) }
}

describe('workspace files HTTP adapter', () => {
  it('resolves and reads only through authenticated, session-bound endpoints', async () => {
    const { access, http, file } = fixture()
    const signal = new AbortController().signal
    const [resolved] = await access.resolve('session-A', [file.requestedPath], signal)
    expect(http.requestJson).toHaveBeenCalledWith('/api/v1/workspace-files/resolve', {
      method: 'POST', sessionKey: 'session-A', json: { paths: [file.requestedPath] }, signal,
    })
    expect(resolved.kind).toBe('text')
    await access.read('session-A', resolved, signal)
    const [endpoint, options] = http.requestBlob.mock.calls[0]
    expect(endpoint).toMatch(/^\/api\/v1\/workspace-files\/content\?/)
    const url = new URL(endpoint, 'https://gateway.test')
    expect(url.searchParams.get('path')).toBe(file.path)
    expect(url.searchParams.get('workspaceBinding')).toBe('binding-A')
    expect(options).toEqual({ sessionKey: 'session-A', signal })
  })

  it.each([{ requestedPath: 'other.svg' }, { path: '../other.svg' }, { path: '/private/other.svg' },
    { name: '../../other.svg' }, { size: -1 }])('rejects mismatched metadata %j', async invalid => {
    const { access, http, file } = fixture()
    http.requestJson.mockResolvedValue({ workspaceBinding: 'binding-A', files: [{ ...file, ...invalid }] })
    await expect(access.resolve('session-A', [file.requestedPath])).rejects.toThrow()
  })

  it('reads bounded source pages and validates the page envelope', async () => {
    const { access, http, file } = fixture()
    http.requestJson.mockResolvedValue({
      relativePath: file.path, content: 'line 201\n', totalLines: 201, startLine: 201, endLine: 201,
    })
    await expect(access.readPage?.('session-A', { ...file, kind: 'text' }, 201, 400))
      .resolves.toMatchObject({ relativePath: file.path, startLine: 201, endLine: 201 })
    const [endpoint, options] = http.requestJson.mock.calls[0]
    expect(endpoint).toContain('/api/v1/workspace-files/page?')
    expect(new URL(endpoint, 'https://gateway.test').searchParams.get('startLine')).toBe('201')
    expect(options).toMatchObject({ method: 'GET', sessionKey: 'session-A' })
  })

  it('only enables capabilities explicitly advertised by the current Gateway', async () => {
    const { access, http, file } = fixture()
    expect((await access.resolve('session-A', [file.path]))[0]).toMatchObject({ textPaging: false, nativeActions: false })
    http.requestJson.mockResolvedValue({ workspaceBinding: 'binding-A', files: [{ ...file, textPaging: true, nativeActions: true }] })
    expect((await access.resolve('session-A', [file.path]))[0]).toMatchObject({ textPaging: true, nativeActions: true })
  })

  it('searches through a session-bound endpoint and rejects an out-of-range result', async () => {
    const { access, http, file } = fixture()
    const resolved = { ...file, kind: 'text' as const }
    const signal = new AbortController().signal
    http.requestJson.mockResolvedValue({ relativePath: file.path, totalLines: 500, matchLine: 450 })
    await expect(access.search!('session-A', resolved, 'target', signal)).resolves.toMatchObject({ matchLine: 450 })
    const [endpoint, options] = http.requestJson.mock.calls[0]
    expect(new URL(endpoint, 'https://gateway.test').searchParams.get('query')).toBe('target')
    expect(options).toEqual({ method: 'GET', sessionKey: 'session-A', signal })
    http.requestJson.mockResolvedValue({ relativePath: file.path, totalLines: 500, matchLine: 501 })
    await expect(access.search!('session-A', resolved, 'target')).rejects.toThrow()
  })

  it('rejects oversized page ranges, mismatched paths and invalid page bounds', async () => {
    const { access, http, file } = fixture()
    const resolved = { ...file, kind: 'text' as const }
    await expect(access.readPage!('session-A', resolved, 1, 201)).rejects.toThrow()
    expect(http.requestJson).not.toHaveBeenCalled()
    for (const patch of [{ relativePath: 'other.py' }, { endLine: 300 }, { content: '\0binary' }]) {
      http.requestJson.mockResolvedValue({ relativePath: file.path, content: 'safe', totalLines: 500, startLine: 1, endLine: 200, ...patch })
      await expect(access.readPage!('session-A', resolved, 1, 200)).rejects.toThrow()
    }
  })

  it('never classifies SVG or HTML as an executable image preview', async () => {
    const { access, http, file } = fixture()
    http.requestJson.mockResolvedValue({ workspaceBinding: 'binding-A', files: [{ ...file, kind: 'image' }] })
    expect((await access.resolve('session-A', [file.requestedPath]))[0].kind).toBe('download')
  })
})
