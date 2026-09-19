import { describe, expect, it, vi } from 'vitest'
import { normalizeWorkspaceFileReferenceV1 } from '@/types/references'
import { createV4WorkspaceReferences } from './workspaceReferencesV4'

const input = normalizeWorkspaceFileReferenceV1({
  version: 1, kind: 'workspace_file', id: 'src/a.py', label: 'src/a.py:1-2', scope: {},
  locator: { relativePath: 'src/a.py', startLine: 1, endLine: 2 },
  state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true },
})!
function response() {
  return {
    reference: { ...input, scope: { sessionKey: 'task', workspaceId: 'workspace' } },
    relativePath: 'src/a.py', revision: 'file_1234567890abcdef', content: 'one\ntwo\n',
    totalLines: 2, startLine: 1, endLine: 2,
  }
}
function adapter(result: unknown) {
  const request = vi.fn().mockResolvedValue(result)
  return { request, access: createV4WorkspaceReferences({ request }) }
}

describe('workspace reference adapter', () => {
  it('validates both production contract roles and retains the resolved workspace binding', async () => {
    const { access, request } = adapter(response())
    const result = await access.read('task', input)
    expect(result.reference.scope.workspaceId).toBe('workspace')
    expect(request).toHaveBeenCalledWith('workspaces.references.read', { sessionKey: 'task', reference: input }, expect.any(Object))
  })
  it.each([
    { relativePath: 'different.py' }, { revision: 'file_abcdef0123456789' },
    { startLine: 2 }, { content: 42 },
    { reference: { ...input, scope: { sessionKey: 'other', workspaceId: 'workspace' } } },
  ])('rejects an inconsistent response %s', async patch => {
    const { access } = adapter({ ...response(), ...patch })
    await expect(access.read('task', input)).rejects.toMatchObject({ code: 'INVALID_REFERENCE' })
  })
  it('rejects a malformed request before transport and maps a stale response', async () => {
    const { access, request } = adapter(response())
    await expect(access.read('', input)).rejects.toMatchObject({ code: 'INVALID_REFERENCE' })
    expect(request).not.toHaveBeenCalled()
    request.mockRejectedValue({ code: 'STALE_REFERENCE', message: 'changed' })
    await expect(access.read('task', input)).rejects.toMatchObject({ code: 'STALE_REFERENCE' })
  })
  it('rejects a source response from a replaced Gateway connection', async () => {
    let finish!: (value: unknown) => void
    const request = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const transport = { generation: 1, request: request as Parameters<typeof createV4WorkspaceReferences>[0]['request'] }
    const pending = createV4WorkspaceReferences(transport).read('task', input)
    transport.generation = 2
    finish(response())
    await expect(pending).rejects.toMatchObject({ code: 'UNAVAILABLE' })
  })
})
