import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFileTreeStore } from './fileTree'
import type { FileNode } from '@/lib/fileTreeModel'
import {
  WorkspaceFilesError,
  type WorkspaceFileListing,
  type WorkspaceFiles,
} from '@/modules/workspaceFiles'

const WS = { id: 'ws-1', name: 'proj', path: '/tmp/proj' }

function fileNode(name: string, path: string): FileNode {
  return { name, path, type: 'file' }
}

function dirNode(name: string, path: string): FileNode {
  return { name, path, type: 'directory' }
}

/**
 * Fake WorkspaceFiles seam. Routes listings by directory path (root = ''),
 * optionally failing for specific paths.
 */
function createFilesPort(
  responses: Record<string, FileNode[]>,
  failPaths: string[] = [],
) {
  const listDir = vi.fn(
    async (_workspaceId: string, dir: string): Promise<WorkspaceFileListing> => {
      if (failPaths.includes(dir)) {
        throw new WorkspaceFilesError('unavailable', 'boom')
      }
      return { path: dir, entries: responses[dir] ?? [] }
    },
  )
  const port: WorkspaceFiles = { listDir, readFile: vi.fn() }
  return { port, listDir }
}

const ROOT_ENTRIES: FileNode[] = [dirNode('src', 'src'), fileNode('README.md', 'README.md')]
const SRC_ENTRIES: FileNode[] = [fileNode('a.ts', 'src/a.ts')]

describe('fileTree store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists the root when a workspace opens and exposes flattened rows', async () => {
    const { port, listDir } = createFilesPort({ '': ROOT_ENTRIES, src: SRC_ENTRIES })
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.ready).toBe(true))

    expect(listDir).toHaveBeenCalledWith('ws-1', '')
    expect(store.rows.map((row) => row.node.path)).toEqual(['src', 'README.md'])
  })

  it('expanding a directory fetches its children once; re-expanding reuses the cache', async () => {
    const { port, listDir } = createFilesPort({ '': ROOT_ENTRIES, src: SRC_ENTRIES })
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.ready).toBe(true))
    const callsAfterRoot = listDir.mock.calls.length

    store.expandDir('src')
    await vi.waitFor(() => expect(store.dirState('src')?.loaded).toBe(true))
    expect(store.rows.map((row) => row.node.path)).toEqual(['src', 'src/a.ts', 'README.md'])
    const callsAfterExpand = listDir.mock.calls.length
    expect(callsAfterExpand).toBe(callsAfterRoot + 1)

    store.collapseDir('src')
    expect(store.rows.map((row) => row.node.path)).toEqual(['src', 'README.md'])
    store.expandDir('src')
    await vi.waitFor(() => expect(store.dirState('src')?.expanded).toBe(true))
    expect(listDir.mock.calls.length).toBe(callsAfterExpand)
  })

  it('concurrent expand of the same directory issues a single request', async () => {
    let release!: () => void
    const pending = new Promise<void>((resolve) => (release = resolve))
    const listDir = vi.fn(async () => {
      await pending
      return { path: '', entries: [] } satisfies WorkspaceFileListing
    })
    const port: WorkspaceFiles = { listDir, readFile: vi.fn() }
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    const first = store.listDir('')
    const second = store.listDir('')
    release()
    await Promise.all([first, second])
    expect(listDir).toHaveBeenCalledTimes(1)
  })

  it('records load errors per directory and on the root', async () => {
    const { port } = createFilesPort({}, ['', 'src'])
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.rootError).not.toBeNull())
    expect(store.ready).toBe(false)

    store.expandDir('src')
    await vi.waitFor(() => expect(store.dirState('src')?.error).not.toBeNull())
    expect(store.dirState('src')?.loading).toBe(false)
  })

  it('refreshDir force-refetches an already loaded directory', async () => {
    const { port, listDir } = createFilesPort({ '': ROOT_ENTRIES })
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.ready).toBe(true))
    const callsAfterOpen = listDir.mock.calls.length

    await store.refreshDir('')
    expect(listDir.mock.calls.length).toBe(callsAfterOpen + 1)
  })

  it('removes nodes that disappeared from a refreshed listing, with subtree cleanup', async () => {
    let call = 0
    const listDir = vi.fn(async (): Promise<WorkspaceFileListing> => {
      call += 1
      const entries =
        call === 1
          ? [dirNode('gone', 'gone'), fileNode('stay.txt', 'stay.txt')]
          : [fileNode('stay.txt', 'stay.txt')]
      return { path: '', entries }
    })
    const port: WorkspaceFiles = { listDir, readFile: vi.fn() }
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.ready).toBe(true))
    expect(store.children('').map((n) => n.name)).toEqual(['gone', 'stay.txt'])

    await store.refreshDir('')
    expect(store.children('').map((n) => n.name)).toEqual(['stay.txt'])
  })

  it('a workspace switch invalidates stale in-flight listings', async () => {
    let releaseRoot!: () => void
    const pending = new Promise<void>((resolve) => (releaseRoot = resolve))
    const listDir = vi.fn(async (
      workspaceId: string,
    ): Promise<WorkspaceFileListing> => {
      // Only ws-1's root listing is the slow/stale one; ws-2's root resolves
      // immediately with empty entries.
      if (workspaceId === 'ws-1') {
        await pending
        return { path: '', entries: [fileNode('stale.txt', 'stale.txt')] }
      }
      return { path: '', entries: [] }
    })
    const port: WorkspaceFiles = { listDir, readFile: vi.fn() }
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    const firstOpen = store.listDir('')
    // Switch before the first root listing resolves.
    store.openWorkspace({ ...WS, id: 'ws-2' })
    releaseRoot()
    await firstOpen.catch(() => {})

    await vi.waitFor(() => expect(store.ready).toBe(true))
    // The stale listing must not have landed in the new workspace's state.
    expect(store.children('')).toEqual([])
  })

  it('closeWorkspace resets state', async () => {
    const { port } = createFilesPort({ '': ROOT_ENTRIES })
    const store = useFileTreeStore()
    store.attachFiles(port)

    store.openWorkspace(WS)
    await vi.waitFor(() => expect(store.ready).toBe(true))
    store.closeWorkspace()
    expect(store.workspace).toBeNull()
    expect(store.rows).toEqual([])
    expect(store.ready).toBe(false)
  })
})
