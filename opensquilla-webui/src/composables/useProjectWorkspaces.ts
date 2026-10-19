import { computed, hasInjectionContext, inject, ref, watch } from 'vue'
import { WORKSPACE_CATALOG_KEY, type WorkspaceCatalog, type WorkspaceCreationDefaults, type WorkspaceHistoryDeletion, type WorkspaceItem } from '@/modules/workspaceCatalog'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'

type ProjectWorkspaceItem = WorkspaceItem
interface WorkspaceCatalogReadOptions {
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
}
export type { WorkspaceItem as ProjectWorkspaceItem } from '@/modules/workspaceCatalog'

const workspaces = ref<ProjectWorkspaceItem[]>([])
const isLoading = ref(false)
const error = ref<string | null>(null)
const hasLoaded = ref(false)
const creationDefaults = ref<WorkspaceCreationDefaults | null>(null)
let loadSequence = 0

export function useProjectWorkspaces(
  catalogOverride?: WorkspaceCatalog,
  accessOverride?: GatewayAccess,
) {
  const catalog = catalogOverride
    ?? (hasInjectionContext() ? inject(WORKSPACE_CATALOG_KEY, null) : null)
  const access = accessOverride
    ?? (hasInjectionContext() ? inject(GATEWAY_ACCESS_KEY, null) : null)

  if (!catalog) {
    throw new Error('Workspace catalog is unavailable.')
  }
  if (!access) {
    throw new Error('Gateway access is unavailable.')
  }
  const gatewayAccess = access
  const workspaceCatalog: WorkspaceCatalog = catalog

  function catalogScope() {
    return JSON.stringify([
      gatewayAccess.deliveryIdentity, gatewayAccess.loadConnectionEndpoint(),
      gatewayAccess.subscriptionEpoch,
    ])
  }

  function resetWorkspaces() {
    loadSequence += 1
    workspaces.value = []
    isLoading.value = false
    error.value = null
    hasLoaded.value = false
    creationDefaults.value = null
  }

  function requireOwner() {
    if (!gatewayAccess.isLocalOwner) {
      throw new Error('Project workspaces require a local owner.')
    }
  }

  async function loadWorkspaces(
    requestOptions?: WorkspaceCatalogReadOptions,
  ): Promise<ProjectWorkspaceItem[]> {
    if (!gatewayAccess.canManageProjectWorkspaces) {
      resetWorkspaces()
      return []
    }
    const requestSequence = ++loadSequence
    const scope = catalogScope()
    isLoading.value = true
    error.value = null
    try {
      const loadedWorkspaces = [...await workspaceCatalog.list(requestOptions)]
      if (requestSequence === loadSequence && scope === catalogScope()) {
        workspaces.value = loadedWorkspaces
        creationDefaults.value = workspaceCatalog.creationDefaults ?? null
        hasLoaded.value = true
      }
      return loadedWorkspaces
    } catch (cause) {
      if (requestSequence === loadSequence) {
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
      throw cause
    } finally {
      if (requestSequence === loadSequence) isLoading.value = false
    }
  }

  async function refreshAfterMutation(): Promise<void> {
    error.value = null
    try {
      await loadWorkspaces()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
  }

  async function openWorkspace(path: string): Promise<ProjectWorkspaceItem | null> {
    requireOwner()
    const workspace = await workspaceCatalog.open(path)
    await loadWorkspaces()
    return workspace
  }

  async function createWorkspace(parentPath: string, name: string): Promise<ProjectWorkspaceItem | null> {
    requireOwner()
    if (!gatewayAccess.canCreateProjectWorkspace || !workspaceCatalog.create) {
      throw new Error('This Gateway does not support creating project directories.')
    }
    const scope = catalogScope()
    try {
      const workspace = await workspaceCatalog.create(parentPath, name)
      if (scope === catalogScope()) {
        // A returned registration is authoritative even if refreshing fails.
        if (workspace) workspaces.value = [
          ...workspaces.value.filter(item => item.id !== workspace.id), workspace,
        ]
        try { await loadWorkspaces() } catch { /* The known creation result remains valid. */ }
      }
      return workspace
    } catch (cause) {
      if (scope === catalogScope()) {
        // A lost reply may follow registration. Refresh for review, but never
        // adopt a directory or replay creation based on path/name equality.
        try { await loadWorkspaces() } catch { /* Preserve the original outcome. */ }
      }
      throw cause
    }
  }

  async function deleteWorkspaceHistory(
    workspaceId: string,
  ): Promise<WorkspaceHistoryDeletion> {
    requireOwner()
    error.value = null
    try {
      const response = await workspaceCatalog.deleteHistory(workspaceId)
      try {
        await loadWorkspaces()
      } catch {
        // The destructive operation remains authoritative when a refresh fails.
      }
      return response
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
  }

  async function removeWorkspace(workspaceId: string): Promise<void> {
    requireOwner()
    error.value = null
    try {
      await workspaceCatalog.remove(workspaceId)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
    // Removal is authoritative once the destructive RPC succeeds. Publish it
    // locally before the best-effort canonical refresh so active tasks fail
    // closed even if the follow-up list request is temporarily unavailable.
    workspaces.value = workspaces.value.filter(item => item.id !== workspaceId)
    try {
      await loadWorkspaces()
    } catch {
      // loadWorkspaces records its own error for retry/status UI.
    }
    workspaces.value = workspaces.value.filter(item => item.id !== workspaceId)
  }

  const byId = computed(() => new Map(workspaces.value.map(item => [item.id, item])))

  watch(catalogScope, resetWorkspaces)

  watch(
    () => gatewayAccess.canManageProjectWorkspaces,
    allowed => {
      if (!allowed) resetWorkspaces()
    },
    { immediate: true },
  )

  return {
    workspaces,
    byId,
    isLoading,
    error,
    hasLoaded,
    creationDefaults,
    resetWorkspaces,
    loadWorkspaces,
    openWorkspace,
    createWorkspace,
    renameWorkspace: async (workspaceId: string, name: string) => {
      requireOwner()
      await workspaceCatalog.rename(workspaceId, name)
      await refreshAfterMutation()
    },
    setPinned: async (workspaceId: string, pinned: boolean) => {
      requireOwner()
      await workspaceCatalog.setPinned(workspaceId, pinned)
      await refreshAfterMutation()
    },
    removeWorkspace,
    deleteWorkspaceHistory,
  }
}
