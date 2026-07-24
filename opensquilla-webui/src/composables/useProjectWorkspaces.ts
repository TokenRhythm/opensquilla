import { computed, ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type {
  ProjectWorkspaceItem,
  ProjectWorkspacesResponse,
} from '@/types/rpc'

export type { ProjectWorkspaceItem } from '@/types/rpc'

const workspaces = ref<ProjectWorkspaceItem[]>([])
const isLoading = ref(false)
const error = ref<string | null>(null)

function normalizeWorkspace(value: unknown): ProjectWorkspaceItem | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const id = typeof row.id === 'string' ? row.id.trim() : ''
  const name = typeof row.name === 'string' ? row.name.trim() : ''
  const path = typeof row.path === 'string' ? row.path : ''
  if (!id || !name || !path) return null
  const count = Number(row.taskCount)
  return {
    id,
    name,
    path,
    taskCount: Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0,
    pinned: row.pinned === true,
    available: row.available !== false,
  }
}

export function useProjectWorkspaces() {
  const rpc = useRpcStore()

  async function loadWorkspaces(): Promise<ProjectWorkspaceItem[]> {
    isLoading.value = true
    error.value = null
    try {
      const response = await rpc.call<ProjectWorkspacesResponse>('workspaces.list')
      workspaces.value = (response?.workspaces || [])
        .map(normalizeWorkspace)
        .filter((item): item is ProjectWorkspaceItem => item !== null)
      return workspaces.value
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      isLoading.value = false
    }
  }

  async function mutate(method: string, params: Record<string, unknown>): Promise<void> {
    error.value = null
    try {
      await rpc.call(method, params)
      await loadWorkspaces()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
  }

  async function openWorkspace(path: string): Promise<ProjectWorkspaceItem | null> {
    const response = await rpc.call<{ workspace?: unknown }>('workspaces.open', {
      path,
      trusted: true,
    })
    await loadWorkspaces()
    return normalizeWorkspace(response?.workspace)
  }

  const byId = computed(() => new Map(workspaces.value.map(item => [item.id, item])))

  return {
    workspaces,
    byId,
    isLoading,
    error,
    loadWorkspaces,
    openWorkspace,
    renameWorkspace: (workspaceId: string, name: string) =>
      mutate('workspaces.update', { workspaceId, name }),
    setPinned: (workspaceId: string, pinned: boolean) =>
      mutate('workspaces.pin', { workspaceId, pinned }),
    removeWorkspace: (workspaceId: string) =>
      mutate('workspaces.remove', { workspaceId }),
    deleteWorkspaceHistory: (workspaceId: string) =>
      mutate('workspaces.history.delete', { workspaceId }),
  }
}
