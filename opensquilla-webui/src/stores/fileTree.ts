import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  flattenLiveFileTreeModel,
  normalizeFileTreePath,
  type FileNode,
  type FileTreeRow,
} from '@/lib/fileTreeModel'
import {
  WorkspaceFilesError,
  type WorkspaceFileListing,
  type WorkspaceFiles,
} from '@/modules/workspaceFiles'

export interface FileTreeWorkspace {
  id: string
  name: string
  path: string
}

interface DirectoryState {
  expanded: boolean
  loaded: boolean
  loading: boolean
  error: string | null
  children: string[]
}

/**
 * Lazy-loading file-tree state for the workspace sidebar view.
 *
 * Ported (adapted from the SolidJS createFileTreeStore) from
 * anomalyco/opencode `packages/app/src/context/file/tree-store.ts`
 * (MIT, Copyright (c) 2025 opencode). See THIRD_PARTY_NOTICES.md.
 */
export const useFileTreeStore = defineStore('fileTree', () => {
  const workspace = ref<FileTreeWorkspace | null>(null)
  const nodes = ref<Record<string, FileNode>>({})
  const dirs = ref<Record<string, DirectoryState>>({
    '': { expanded: true, loaded: false, loading: false, error: null, children: [] },
  })
  const rootLoading = ref(false)
  const rootError = ref<string | null>(null)

  const inflight = new Map<string, Promise<void>>()
  let scopeSeq = 0
  let filesPort: WorkspaceFiles | null = null

  /**
   * Attach the WorkspaceFiles seam. The consuming component resolves it from
   * the app-level provide; the store itself stays transport-agnostic.
   */
  function attachFiles(port: WorkspaceFiles): void {
    filesPort = port
  }

  function dirState(path: string): DirectoryState | undefined {
    return dirs.value[path]
  }

  function ensureDir(path: string) {
    if (!dirs.value[path]) {
      dirs.value[path] = { expanded: false, loaded: false, loading: false, error: null, children: [] }
    }
  }

  function scopeId(): number {
    return scopeSeq
  }

  async function listDir(path: string, options?: { force?: boolean }): Promise<void> {
    if (!workspace.value) return
    const dir = normalizeFileTreePath(path)
    ensureDir(dir)

    const current = dirs.value[dir]
    if (!options?.force && current?.loaded) return

    const pending = inflight.get(dir)
    if (pending) return pending

    const wsId = workspace.value.id
    const seq = scopeId()
    current.loading = true
    current.error = null
    if (dir === '') rootLoading.value = true

    const promise = (async () => {
      if (!filesPort) {
        throw new WorkspaceFilesError('unavailable', 'WorkspaceFiles seam is not attached.')
      }
      const listing: WorkspaceFileListing = await filesPort.listDir(wsId, dir)
      if (seq !== scopeId() || workspace.value?.id !== wsId) return

      const nextChildren = listing.entries.map((entry) => entry.path)
      const nextSet = new Set(nextChildren)

      // Drop nodes that disappeared (files deleted/renamed away), including
      // any subtree under a removed directory.
      const prevChildren = dirs.value[dir]?.children ?? []
      const removedDirs = prevChildren.filter(
        (child) => !nextSet.has(child) && nodes.value[child]?.type === 'directory',
      )
      for (const key of Object.keys(nodes.value)) {
        if (removedDirs.some((removed) => key === removed || key.startsWith(`${removed}/`))) {
          delete nodes.value[key]
        }
        if (dir !== '' && key === dir) {
          /* the directory node itself is owned by its parent listing */
        }
      }
      for (const entry of listing.entries) {
        nodes.value[entry.path] = entry
      }

      dirs.value[dir] = {
        ...(dirs.value[dir] ?? { expanded: false }),
        expanded: dirs.value[dir]?.expanded ?? (dir === ''),
        loaded: true,
        loading: false,
        error: null,
        children: nextChildren,
      }
      if (dir === '') {
        rootLoading.value = false
        rootError.value = null
      }
    })().catch((error: unknown) => {
      if (seq !== scopeId() || workspace.value?.id !== wsId) return
      const message = error instanceof Error ? error.message : String(error)
      if (dirs.value[dir]) {
        dirs.value[dir].loading = false
        dirs.value[dir].error = message
      }
      if (dir === '') {
        rootLoading.value = false
        rootError.value = message
      }
      throw error
    })

    inflight.set(dir, promise)
    try {
      await promise
    } finally {
      inflight.delete(dir)
    }
  }

  function expandDir(path: string) {
    const dir = normalizeFileTreePath(path)
    ensureDir(dir)
    dirs.value[dir].expanded = true
    void listDir(dir).catch(() => {
      /* error state is recorded on the directory */
    })
  }

  function collapseDir(path: string) {
    const dir = normalizeFileTreePath(path)
    ensureDir(dir)
    dirs.value[dir].expanded = false
  }

  function toggleDir(path: string) {
    if (dirState(path)?.expanded) collapseDir(path)
    else expandDir(path)
  }

  /** Re-fetch an already-loaded directory (manual refresh button). */
  function refreshDir(path: string) {
    return listDir(normalizeFileTreePath(path), { force: true }).catch(() => {
      /* error state is recorded on the directory */
    })
  }

  /** Refresh every loaded directory (root + expanded). */
  async function refreshAll() {
    const loaded = Object.keys(dirs.value).filter(
      (path) => dirs.value[path]?.loaded || path === '',
    )
    await Promise.allSettled(loaded.map((path) => refreshDir(path)))
  }

  function openWorkspace(next: FileTreeWorkspace) {
    // A workspace switch invalidates all in-flight requests via the scope seq.
    scopeSeq += 1
    inflight.clear()
    workspace.value = next
    nodes.value = {}
    dirs.value = {
      '': { expanded: true, loaded: false, loading: false, error: null, children: [] },
    }
    rootLoading.value = false
    rootError.value = null
    void listDir('').catch(() => {
      /* error state is recorded on the root */
    })
  }

  function closeWorkspace() {
    scopeSeq += 1
    inflight.clear()
    workspace.value = null
    nodes.value = {}
    dirs.value = {
      '': { expanded: true, loaded: false, loading: false, error: null, children: [] },
    }
    rootLoading.value = false
    rootError.value = null
  }

  const children = (path: string): FileNode[] => {
    const dir = normalizeFileTreePath(path)
    const ids = dirs.value[dir]?.children
    if (!ids) return []
    const out: FileNode[] = []
    for (const id of ids) {
      const node = nodes.value[id]
      if (node) out.push(node)
    }
    return out
  }

  const rows = computed<FileTreeRow[]>(() => {
    if (!workspace.value) return []
    return flattenLiveFileTreeModel(
      (path) => children(path),
      (path) => dirState(path)?.expanded ?? false,
    )
  })

  const ready = computed(() => Boolean(workspace.value && !rootLoading.value && !rootError.value && (dirs.value['']?.loaded ?? false)))

  return {
    workspace,
    rootLoading,
    rootError,
    rows,
    ready,
    dirState,
    children,
    openWorkspace,
    closeWorkspace,
    attachFiles,
    listDir,
    expandDir,
    collapseDir,
    toggleDir,
    refreshDir,
    refreshAll,
    _testing: { scopeSeq: () => scopeSeq, inflight: () => inflight.size },
  }
})
