import { describe, expect, it, vi } from 'vitest'
import { createV4WorkspaceFiles } from './workspaceFilesV4'

function fixture() {
  const file = { requestedPath: 'outputs/中文 图.svg', path: 'outputs/中文 图.svg', name: '中文 图.svg',
    mime: 'image/svg+xml', size: 42, kind: 'text', contentUrl: 'https://untrusted.test/leak' }
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

  it('never classifies SVG or HTML as an executable image preview', async () => {
    const { access, http, file } = fixture()
    http.requestJson.mockResolvedValue({ workspaceBinding: 'binding-A', files: [{ ...file, kind: 'image' }] })
    expect((await access.resolve('session-A', [file.requestedPath]))[0].kind).toBe('download')
  })
})
