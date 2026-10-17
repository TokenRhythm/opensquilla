import { describe, expect, it, vi } from 'vitest'
import { createV4WorkspaceFiles } from './workspaceFilesV4'
import { HttpTransportError } from './privateHttpTransport'
import { WorkspaceFilesChangedError, WorkspaceTextLimitError, type WorkspaceRootScope } from '@/modules/workspaceFiles'

function fixture() {
  const file = { requestedPath: 'outputs/中文 图.svg', path: 'outputs/中文 图.svg', name: '中文 图.svg',
    mime: 'image/svg+xml', size: 42, kind: 'text', workspaceBinding: 'binding-A', contentUrl: 'https://untrusted.test/leak' }
  const http = { requestJson: vi.fn().mockResolvedValue({ workspaceBinding: 'binding-A', files: [file] }),
    requestBlob: vi.fn().mockResolvedValue(new Blob(['<svg/>'], { type: 'image/svg+xml' })) }
  return { file, http, access: createV4WorkspaceFiles(http) }
}

describe('workspace files HTTP adapter', () => {
  const root = { kind: 'workspace-root' as const, name: '中文 项目', path: 'C:\\Projects\\中文 项目', workspaceBinding: 'a'.repeat(64) }
  it('opens project roots without a session and never submits a host path', async () => {
    const { access, http } = fixture()
    const signal = new AbortController().signal
    http.requestJson.mockResolvedValueOnce(root)
    const resolved = await access.root!({ projectId: 'project-a' }, signal)
    expect(http.requestJson).toHaveBeenLastCalledWith('/api/v1/workspace-files/root?projectId=project-a', {
      method: 'GET', sessionKey: undefined, signal,
    })
    http.requestJson.mockResolvedValueOnce({ available: true, host: 'gateway', apps: [{ id: 'reveal', label: 'Show in folder' }] })
    await access.rootApps!({ projectId: 'project-a' }, signal)
    expect(http.requestJson).toHaveBeenLastCalledWith('/api/v1/workspace-files/apps?projectId=project-a', {
      method: 'GET', sessionKey: undefined, signal,
    })
    http.requestJson.mockResolvedValueOnce({ ok: true, host: 'gateway', appId: 'reveal' })
    await access.openRoot!({ projectId: 'project-a' }, resolved, 'reveal', signal)
    expect(http.requestJson).toHaveBeenLastCalledWith('/api/v1/workspace-files/open-root', {
      method: 'POST', sessionKey: undefined, signal,
      json: { workspaceBinding: root.workspaceBinding, projectId: 'project-a', appId: 'reveal' },
    })
  })
  it('binds session-root opening to the session and preserves unavailable catalog reasons', async () => {
    const { access, http } = fixture()
    http.requestJson.mockResolvedValueOnce(root)
    await access.root!({ sessionKey: 'session-a' })
    expect(http.requestJson).toHaveBeenLastCalledWith('/api/v1/workspace-files/root', {
      method: 'GET', sessionKey: 'session-a', signal: undefined,
    })
    http.requestJson.mockResolvedValueOnce({ available: false, host: 'gateway', apps: [], reason: 'remote-client' })
    expect(await access.rootApps!({ sessionKey: 'session-a' })).toMatchObject({ available: false, reason: 'remote-client' })
    http.requestJson.mockResolvedValueOnce({ ok: true, host: 'gateway', appId: 'vscode' })
    await access.openRoot!({ sessionKey: 'session-a' }, root, 'vscode')
    expect(http.requestJson).toHaveBeenLastCalledWith('/api/v1/workspace-files/open-root', {
      method: 'POST', sessionKey: 'session-a', signal: undefined, json: { workspaceBinding: root.workspaceBinding, appId: 'vscode' },
    })
  })
  it.each([{}, { projectId: '' }, { projectId: '../a' }, { projectId: 'a', sessionKey: 'session-a' }, { sessionKey: '\n' }])('rejects ambiguous or invalid root scope %j', async invalid => {
    const { access, http } = fixture()
    const scope = invalid as unknown as WorkspaceRootScope
    await expect(access.root!(scope)).rejects.toThrow()
    await expect(access.rootApps!(scope)).rejects.toThrow()
    await expect(access.openRoot!(scope, root, 'reveal')).rejects.toThrow()
    expect(http.requestJson).not.toHaveBeenCalled()
  })
  it.each([{ kind: 'workspace-file' }, { workspaceBinding: '' }, { path: 'https://untrusted.test' }, { path: 'relative' }, { path: 'C:\\bad\npath' }])('rejects malformed root descriptors %j', async patch => {
    const { access, http } = fixture()
    http.requestJson.mockResolvedValueOnce({ ...root, ...patch })
    await expect(access.root!({ sessionKey: 'session-a' })).rejects.toThrow()
  })
  it('does not treat rejected or mismatched root launches as success', async () => {
    const { access, http } = fixture()
    http.requestJson.mockResolvedValueOnce({ ok: true, host: 'gateway', appId: 'default' })
    await expect(access.openRoot!({ projectId: 'a' }, root, 'reveal')).rejects.toThrow()
    http.requestJson.mockRejectedValueOnce(new HttpTransportError('http-status', 'changed', 409))
    await expect(access.openRoot!({ projectId: 'a' }, root, 'reveal')).rejects.toBeInstanceOf(WorkspaceFilesChangedError)
  })
  it.each(['line', 'page'] as const)('translates the %s preview bound without calling the workspace unavailable', async limit => {
    const { access, http, file } = fixture()
    const [resolved] = await access.resolve('session-A', [file.path])
    http.requestJson.mockRejectedValue(new HttpTransportError('http-status', 'size limit', 413, { code: 'WORKSPACE_TEXT_LIMIT', limit }))
    await expect(access.readPage!('session-A', resolved, 1, 200)).rejects.toMatchObject({ name: 'WorkspaceTextLimitError', limit })
    await expect(access.search!('session-A', resolved, 'x')).rejects.toBeInstanceOf(WorkspaceTextLimitError)
  })
  it.each([undefined, { code: 'OTHER_LIMIT', limit: 'line' }, { code: 'WORKSPACE_TEXT_LIMIT', limit: 'unknown' }])('does not guess at an unrelated 413 response %j', async payload => {
    const { access, http, file } = fixture()
    const [resolved] = await access.resolve('session-A', [file.path])
    const error = new HttpTransportError('http-status', 'size limit', 413, payload)
    http.requestJson.mockRejectedValue(error)
    await expect(access.readPage!('session-A', resolved, 1, 200)).rejects.toBe(error)
  })
  it('pins reads to resolved versions and exposes a domain conflict instead of mixed content', async () => {
    const { access, http, file } = fixture()
    http.requestJson.mockResolvedValue({ workspaceBinding: 'binding-A', files: [{ ...file, revision: 'rev-a' }] })
    const [resolved] = await access.resolve('session-A', [file.path])
    await access.read('session-A', resolved)
    expect(new URL(http.requestBlob.mock.calls[0][0], 'https://gateway.test').searchParams.get('expectedRevision')).toBe('rev-a')
    http.requestJson.mockRejectedValue(new HttpTransportError('http-status', 'changed', 409, { code: 'WORKSPACE_FILE_CHANGED' }))
    await expect(access.readPage!('session-A', resolved, 1, 200)).rejects.toBeInstanceOf(WorkspaceFilesChangedError)
    expect(new URL(http.requestJson.mock.calls[http.requestJson.mock.calls.length - 1]![0], 'https://gateway.test').searchParams.get('expectedRevision')).toBe('rev-a')
  })
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

  it('lists validated directory pages and carries the workspace binding forward', async () => {
    const { access, http } = fixture()
    http.requestJson.mockResolvedValue({
      workspaceBinding: 'binding-A', relativePath: '', nextCursor: '2', hasMore: true, truncated: false,
      entries: [{ name: 'src', path: 'src', type: 'directory', kind: 'directory', mime: '', size: null, mtimeNs: '1' }],
    })
    const page = await access.listDir!('session-A', '', '0', 1, '', new AbortController().signal)
    expect(page.entries[0]).toMatchObject({ path: 'src', type: 'directory' })
    const [endpoint, options] = http.requestJson.mock.calls[0]
    expect(endpoint).toContain('/api/v1/workspace-files/list?')
    expect(new URL(endpoint, 'https://gateway.test').searchParams.get('limit')).toBe('1')
    expect(options).toMatchObject({ method: 'GET', sessionKey: 'session-A' })
  })

  it('rejects unsafe directory entries', async () => {
    const { access, http } = fixture()
    http.requestJson.mockResolvedValue({
      workspaceBinding: 'binding-A', relativePath: '', nextCursor: null, hasMore: false, truncated: false,
      entries: [{ name: 'bad', path: '../bad', type: 'file', kind: 'text', mime: 'text/plain', size: 1, mtimeNs: '1' }],
    })
    await expect(access.listDir!('session-A')).rejects.toThrow()
  })
})
