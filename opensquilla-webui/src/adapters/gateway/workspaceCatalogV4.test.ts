import { describe, expect, it, vi } from 'vitest'
import { createV4WorkspaceCatalog } from './workspaceCatalogV4'

const workspace = {
  id: 'project-a',
  name: 'Project A',
  path: '/repo/a',
  taskCount: 2,
  pinned: false,
  available: true,
}

function transport() {
  const request = vi.fn(async (method: string) => {
    switch (method) {
      case 'workspaces.list': return { workspaces: [workspace] }
      case 'workspaces.open': return { workspace }
      case 'workspaces.update': return { workspace: { ...workspace, name: 'Renamed' } }
      case 'workspaces.pin': return { workspace: { ...workspace, pinned: true } }
      case 'workspaces.remove': return {
        removed: true,
        workspaceId: workspace.id,
        pausedCronJobIds: [],
        pausedCronJobCount: 0,
      }
      case 'workspaces.history.delete': return {
        workspaceId: workspace.id,
        deletedTaskCount: 2,
        deletedSessionKeys: ['agent:main:webchat:project-a'],
      }
      case 'sandbox.path.list': return {
        currentPath: '/repo',
        path: '/repo',
        parentPath: '/',
        systemPickerAvailable: false,
        entries: [{ name: 'a', path: '/repo/a', kind: 'directory', selectable: true }],
      }
      case 'sandbox.path.create-directory': return { path: '/repo/new', name: 'new', kind: 'directory' }
      case 'sandbox.path.pick': return { path: '/repo/a', kind: 'workspace' }
      default: throw new Error(`unexpected method ${method}`)
    }
  })
  return { request }
}

describe('createV4WorkspaceCatalog', () => {
  it('accepts additional response fields and keeps path mutation cancellation options', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ path: '/synthetic/new', name: 'new', kind: 'directory', extra: true })
      .mockResolvedValueOnce({ path: null, kind: 'mount', extra: true })
    const catalog = createV4WorkspaceCatalog({ request })
    const signal = new AbortController().signal

    await expect(catalog.createDirectory({
      sessionKey: 'synthetic-session', parentPath: '/synthetic', name: 'new',
    }, { signal })).resolves.toEqual({ path: '/synthetic/new', name: 'new', kind: 'directory' })
    await expect(catalog.pickPath({ sessionKey: 'synthetic-session', kind: 'mount' }, { signal }))
      .resolves.toEqual({ path: null, kind: 'mount' })
    expect(request).toHaveBeenNthCalledWith(1, 'sandbox.path.create-directory', {
      sessionKey: 'synthetic-session', parentPath: '/synthetic', name: 'new',
    }, { signal, abortAction: 'reject', timeoutAction: 'reject' })
    expect(request).toHaveBeenNthCalledWith(2, 'sandbox.path.pick', {
      sessionKey: 'synthetic-session', kind: 'mount',
    }, { signal, abortAction: 'reject', timeoutAction: 'reject' })
  })

  it('maps workspace lifecycle methods to generated wire contracts', async () => {
    const source = transport()
    const catalog = createV4WorkspaceCatalog(source as never)

    await expect(catalog.list()).resolves.toEqual([workspace])
    await expect(catalog.open('/repo/a')).resolves.toEqual(workspace)
    await expect(catalog.rename('project-a', 'Renamed')).resolves.toMatchObject({ name: 'Renamed' })
    await expect(catalog.setPinned('project-a', true)).resolves.toMatchObject({ pinned: true })
    await expect(catalog.remove('project-a')).resolves.toBeUndefined()
    await expect(catalog.deleteHistory('project-a')).resolves.toMatchObject({ deletedTaskCount: 2 })

    expect(source.request).toHaveBeenCalledWith('workspaces.open', { path: '/repo/a', trusted: true }, undefined)
    expect(source.request).toHaveBeenCalledWith('workspaces.pin', { workspaceId: 'project-a', pinned: true }, undefined)
  })

  it('keeps sandbox path browsing behind the workspace domain seam', async () => {
    const source = transport()
    const catalog = createV4WorkspaceCatalog(source as never)

    await expect(catalog.listPath({ sessionKey: 'agent:main:webchat:picker' })).resolves.toMatchObject({
      currentPath: '/repo',
      entries: [{ path: '/repo/a' }],
    })
    await expect(catalog.createDirectory({
      sessionKey: 'agent:main:webchat:picker',
      parentPath: '/repo',
      name: 'new',
    })).resolves.toEqual({ path: '/repo/new', name: 'new', kind: 'directory' })
    await expect(catalog.pickPath({ sessionKey: 'agent:main:webchat:picker' })).resolves.toEqual({
      path: '/repo/a',
      kind: 'workspace',
    })
  })
})
