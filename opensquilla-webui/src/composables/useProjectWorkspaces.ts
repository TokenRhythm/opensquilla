import { computed, hasInjectionContext, inject, ref, watch, type Ref } from 'vue'
import { WORKSPACE_CATALOG_KEY, type WorkspaceCatalog, type WorkspaceCreationDefaults, type WorkspaceHistoryDeletion, type WorkspaceItem } from '@/modules/workspaceCatalog'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'

type ProjectWorkspaceItem = WorkspaceItem
interface WorkspaceCatalogReadOptions {
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
  /** Operation preflight must not reuse a read begun before the operation. */
  readonly fresh?: boolean
}
export type { WorkspaceItem as ProjectWorkspaceItem } from '@/modules/workspaceCatalog'

const workspaces = ref<ProjectWorkspaceItem[]>([])
const isLoading = ref(false)
const error = ref<string | null>(null)
const hasLoaded = ref(false)
const creationDefaults = ref<WorkspaceCreationDefaults | null>(null)
let loadSequence = 0

interface CatalogFlight {
  sequence: number
  controller: AbortController
  promise: Promise<ProjectWorkspaceItem[]>
  waiters: number
  settled: boolean
}
interface CatalogReads {
  scope: string | null
  mutationGeneration: number
  flight: CatalogFlight | null
  validated: readonly ProjectWorkspaceItem[] | null
  validationGeneration: Ref<number>
}
const catalogReads = new WeakMap<WorkspaceCatalog, CatalogReads>()
const REPLACED_READ = Symbol('Workspace catalog read replaced by fresh preflight')

function cancelledRead(message = 'Workspace catalog read was superseded'): Error {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

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
  let reads = catalogReads.get(workspaceCatalog)
  if (!reads) {
    reads = { scope: null, mutationGeneration: 0, flight: null, validated: null, validationGeneration: ref(0) }
    catalogReads.set(workspaceCatalog, reads)
  }
  const sharedReads = reads

  function catalogScope() {
    return JSON.stringify([
      gatewayAccess.deliveryIdentity, gatewayAccess.loadConnectionEndpoint(),
      gatewayAccess.subscriptionEpoch,
    ])
  }

  function resetWorkspaces() {
    invalidateReads()
    sharedReads.scope = catalogScope()
    loadSequence += 1
    workspaces.value = []
    isLoading.value = false
    error.value = null
    hasLoaded.value = false
    creationDefaults.value = null
  }

  function invalidateReads(replaced = false) {
    sharedReads.mutationGeneration += 1
    sharedReads.validated = null
    sharedReads.validationGeneration.value += 1
    const prior = sharedReads.flight
    sharedReads.flight = null
    prior?.controller.abort(replaced ? REPLACED_READ : undefined)
  }

  function requireOwner() {
    if (!gatewayAccess.isLocalOwner) {
      throw new Error('Project workspaces require a local owner.')
    }
  }

  function loadWorkspaces(
    requestOptions?: WorkspaceCatalogReadOptions,
  ): Promise<ProjectWorkspaceItem[]> {
    if (!gatewayAccess.canManageProjectWorkspaces) {
      resetWorkspaces()
      return Promise.resolve([])
    }
    if (requestOptions?.signal?.aborted) return Promise.reject(cancelledRead('Workspace catalog read was cancelled'))
    const scope = catalogScope()
    if (sharedReads.scope !== scope) {
      resetWorkspaces()
    }
    if (requestOptions?.fresh) invalidateReads(true)
    let flight = sharedReads.flight
    if (!flight || flight.controller.signal.aborted) {
      const requestSequence = ++loadSequence
      const mutationGeneration = sharedReads.mutationGeneration
      const controller = new AbortController()
      isLoading.value = true
      // Keep an existing error visible while its retry is in progress.
      const current = () => !controller.signal.aborted
        && sharedReads.scope === scope && scope === catalogScope()
        && mutationGeneration === sharedReads.mutationGeneration
        && gatewayAccess.canManageProjectWorkspaces
      const created: CatalogFlight = { sequence: requestSequence, controller, waiters: 0, settled: false, promise: Promise.resolve([]) }
      sharedReads.flight = created
      // Each waiter owns its deadline. The shared transport is cancelled only
      // when all waiters leave, or its scope/mutation generation is retired.
      created.promise = Promise.resolve().then(() => {
        if (!current()) throw cancelledRead()
        return workspaceCatalog.list({ signal: controller.signal, timeoutMs: 0 })
      }).then(result => {
        if (!current()) throw cancelledRead()
        const loadedWorkspaces = [...result]
        sharedReads.validated = loadedWorkspaces
        // A background list can discover relocation/removal too. Retire old
        // per-session physical snapshots before publishing the new catalog.
        sharedReads.validationGeneration.value += 1
        if (requestSequence === loadSequence) {
          workspaces.value = loadedWorkspaces
          creationDefaults.value = workspaceCatalog.creationDefaults ?? null
          hasLoaded.value = true
          error.value = null
        }
        return loadedWorkspaces
      }).catch(cause => {
        if (current() && requestSequence === loadSequence) {
          sharedReads.validated = null
          sharedReads.validationGeneration.value += 1
          error.value = cause instanceof Error ? cause.message : String(cause)
        }
        throw cause
      }).finally(() => {
        created.settled = true
        if (sharedReads.flight === created) sharedReads.flight = null
        if (requestSequence === loadSequence) isLoading.value = false
      })
      flight = created
    }
    const joined = flight
    joined.waiters += 1
    return new Promise((resolve, reject) => {
      let done = false
      let timer: ReturnType<typeof setTimeout> | undefined
      const timeoutMs = requestOptions?.timeoutMs ?? 30_000
      const deadline = timeoutMs > 0 && Number.isFinite(timeoutMs) ? Date.now() + timeoutMs : null
      const finish = (cause?: unknown, result?: ProjectWorkspaceItem[] | Promise<ProjectWorkspaceItem[]>) => {
        if (done) return
        done = true
        if (timer !== undefined) clearTimeout(timer)
        requestOptions?.signal?.removeEventListener('abort', cancel)
        joined.controller.signal.removeEventListener('abort', superseded)
        joined.waiters -= 1
        if (!joined.waiters && !joined.settled) {
          if (sharedReads.flight === joined) sharedReads.flight = null
          joined.controller.abort()
          if (joined.sequence === loadSequence) {
            isLoading.value = false
            if (cause instanceof Error && cause.name === 'TimeoutError') {
              sharedReads.validated = null
              sharedReads.validationGeneration.value += 1
              error.value = cause.message
            }
          }
        }
        if (cause !== undefined) reject(cause)
        else resolve(result!)
      }
      const cancel = () => finish(cancelledRead('Workspace catalog read was cancelled'))
      const superseded = () => {
        if (joined.controller.signal.reason !== REPLACED_READ) {
          finish(cancelledRead())
          return
        }
        // A fresh preflight raises the freshness bar for existing readers too.
        // Follow its successor rather than turning harmless replacement into
        // a metadata error; never return the retired transport's response.
        finish(undefined, Promise.resolve().then(() => {
          if (scope !== catalogScope() || sharedReads.scope !== scope) throw cancelledRead()
          const remaining = deadline === null ? timeoutMs : deadline - Date.now()
          if (deadline !== null && remaining <= 0) {
            const cause = new Error('Workspace catalog read timed out')
            cause.name = 'TimeoutError'
            throw cause
          }
          return loadWorkspaces({ signal: requestOptions?.signal, timeoutMs: remaining })
        }))
      }
      requestOptions?.signal?.addEventListener('abort', cancel, { once: true })
      joined.controller.signal.addEventListener('abort', superseded, { once: true })
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) timer = setTimeout(() => {
        const cause = new Error('Workspace catalog read timed out')
        cause.name = 'TimeoutError'
        finish(cause)
      }, timeoutMs)
      joined.promise.then(result => finish(undefined, result), cause => finish(cause))
    })
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
    const scope = catalogScope()
    invalidateReads()
    let workspace: ProjectWorkspaceItem | null
    try { workspace = await workspaceCatalog.open(path) } finally {
      if (scope === catalogScope()) invalidateReads()
    }
    if (scope !== catalogScope()) throw cancelledRead()
    await loadWorkspaces()
    return workspace
  }

  async function createWorkspace(parentPath: string, name: string): Promise<ProjectWorkspaceItem | null> {
    requireOwner()
    if (!gatewayAccess.canCreateProjectWorkspace || !workspaceCatalog.create) {
      throw new Error('This Gateway does not support creating project directories.')
    }
    const scope = catalogScope()
    invalidateReads()
    try {
      const workspace = await workspaceCatalog.create(parentPath, name)
      if (scope === catalogScope()) invalidateReads()
      if (scope !== catalogScope()) throw cancelledRead()
      if (scope === catalogScope()) {
        // A returned registration is authoritative even if refreshing fails.
        if (workspace) workspaces.value = [
          ...workspaces.value.filter(item => item.id !== workspace.id), workspace,
        ]
        try { await loadWorkspaces() } catch { /* The known creation result remains valid. */ }
      }
      if (scope !== catalogScope()) throw cancelledRead()
      return workspace
    } catch (cause) {
      if (scope === catalogScope()) invalidateReads()
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
    const scope = catalogScope()
    error.value = null
    invalidateReads()
    try {
      const response = await workspaceCatalog.deleteHistory(workspaceId)
      if (scope !== catalogScope()) return response
      invalidateReads()
      try {
        await loadWorkspaces()
      } catch {
        // The destructive operation remains authoritative when a refresh fails.
      }
      return response
    } catch (cause) {
      if (scope === catalogScope()) {
        invalidateReads()
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
      throw cause
    }
  }

  async function removeWorkspace(workspaceId: string): Promise<void> {
    requireOwner()
    const scope = catalogScope()
    error.value = null
    invalidateReads()
    try {
      await workspaceCatalog.remove(workspaceId)
      if (scope !== catalogScope()) return
      invalidateReads()
    } catch (cause) {
      if (scope === catalogScope()) {
        invalidateReads()
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
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
    if (scope === catalogScope()) workspaces.value = workspaces.value.filter(item => item.id !== workspaceId)
  }

  const byId = computed(() => new Map(workspaces.value.map(item => [item.id, item])))

  function getValidatedWorkspace(id: string, path: string): { workspace: ProjectWorkspaceItem | null } | undefined {
    if (!gatewayAccess.canManageProjectWorkspaces || sharedReads.scope !== catalogScope()
      || !sharedReads.validated) return undefined
    const workspace = sharedReads.validated.find(item => item.id === id)
    // An ID alone cannot vouch for a relocated directory.
    if (workspace && workspace.path !== path) return undefined
    return { workspace: workspace ?? null }
  }

  watch(catalogScope, () => {
    if (sharedReads.scope !== catalogScope()) resetWorkspaces()
  }, { flush: 'sync' })

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
    getValidatedWorkspace,
    validationGeneration: sharedReads.validationGeneration,
    loadWorkspaces,
    openWorkspace,
    createWorkspace,
    renameWorkspace: async (workspaceId: string, name: string) => {
      requireOwner()
      const scope = catalogScope()
      invalidateReads()
      try { await workspaceCatalog.rename(workspaceId, name) } finally {
        if (scope === catalogScope()) invalidateReads()
      }
      if (scope !== catalogScope()) throw cancelledRead()
      await refreshAfterMutation()
    },
    setPinned: async (workspaceId: string, pinned: boolean) => {
      requireOwner()
      const scope = catalogScope()
      invalidateReads()
      try { await workspaceCatalog.setPinned(workspaceId, pinned) } finally {
        if (scope === catalogScope()) invalidateReads()
      }
      if (scope !== catalogScope()) throw cancelledRead()
      await refreshAfterMutation()
    },
    removeWorkspace,
    deleteWorkspaceHistory,
  }
}
