import { computed, ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type {
  ProjectWorkspaceHistoryDeleteResponse,
  ProjectWorkspaceItem,
  ProjectWorkspacesResponse,
} from '@/types/rpc'

export type { ProjectWorkspaceItem } from '@/types/rpc'

const workspaces = ref<ProjectWorkspaceItem[]>([])
const isLoading = ref(false)
const error = ref<string | null>(null)
const hasLoaded = ref(false)

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
      hasLoaded.value = true
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

  async function deleteWorkspaceHistory(
    workspaceId: string,
  ): Promise<Required<ProjectWorkspaceHistoryDeleteResponse>> {
    error.value = null
    let response: ProjectWorkspaceHistoryDeleteResponse
    try {
      response = await rpc.call<ProjectWorkspaceHistoryDeleteResponse>(
        'workspaces.history.delete',
        { workspaceId },
      )
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
    // The destructive RPC is authoritative. A follow-up list refresh may fail,
    // but that cannot turn an already-completed deletion into a failed action.
    try {
      await loadWorkspaces()
    } catch {
      // loadWorkspaces records its own error for the UI/retry path.
    }
    return {
      workspaceId: response?.workspaceId || workspaceId,
      deletedTaskCount: Math.max(0, Number(response?.deletedTaskCount) || 0),
      deletedSessionKeys: Array.isArray(response?.deletedSessionKeys)
        ? response.deletedSessionKeys.filter(key => typeof key === 'string' && key.length > 0)
        : [],
    }
  }

  const byId = computed(() => new Map(workspaces.value.map(item => [item.id, item])))

  return {
    workspaces,
    byId,
    isLoading,
    error,
    hasLoaded,
    loadWorkspaces,
    openWorkspace,
    renameWorkspace: (workspaceId: string, name: string) =>
      mutate('workspaces.update', { workspaceId, name }),
    setPinned: (workspaceId: string, pinned: boolean) =>
      mutate('workspaces.pin', { workspaceId, pinned }),
    removeWorkspace: (workspaceId: string) =>
      mutate('workspaces.remove', { workspaceId }),
    deleteWorkspaceHistory,
  }
}
