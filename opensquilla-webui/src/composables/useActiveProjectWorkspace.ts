import { computed, ref } from 'vue'

export type ActiveProjectWorkspaceStatus =
  | 'none'
  | 'resolving'
  | 'ready'
  | 'unavailable'
  | 'removed'
  | 'unknown'
  | 'error'

export interface ActiveProjectWorkspaceSnapshot {
  id: string
  name: string
  path: string
  available: boolean
  removed: boolean
  availabilityReason?: string
}

export interface SessionProjectWorkspaceMetadata {
  workspaceId?: string
  projectWorkspace?: ActiveProjectWorkspaceSnapshot | null
}

/** Hydration may intentionally defer filesystem validation to the workspace API. */
export async function applySessionProjectWorkspaceMetadata(options: {
  state: ReturnType<typeof useActiveProjectWorkspace>
  sessionKey: string
  generation: number
  metadata: SessionProjectWorkspaceMetadata & { projectWorkspaceDeferred?: boolean }
  cachedWorkspace?: ActiveProjectWorkspaceSnapshot | null
  loadWorkspaces: () => Promise<readonly ActiveProjectWorkspaceSnapshot[]>
  isCurrent: () => boolean
}): Promise<void> {
  if (!options.isCurrent()) return
  const { state, sessionKey, generation, metadata } = options
  const deferred = Boolean(metadata.projectWorkspaceDeferred && metadata.workspaceId && !metadata.projectWorkspace)
  const known = state.activeWorkspace.value?.id === metadata.workspaceId
    ? state.activeWorkspace.value : options.cachedWorkspace?.id === metadata.workspaceId
      ? options.cachedWorkspace : null
  if (!state.applySessionSnapshot(sessionKey, generation, {
    ...metadata,
    projectWorkspace: metadata.projectWorkspace || (deferred ? known : null),
  }, { settled: !deferred }) || !deferred) return
  try {
    const workspaces = await options.loadWorkspaces()
    if (!options.isCurrent()) return
    const workspace = workspaces.find(item => item.id === metadata.workspaceId)
      || (known ? { ...known, available: false, removed: true } : null)
    state.applySessionSnapshot(sessionKey, generation, {
      workspaceId: metadata.workspaceId,
      projectWorkspace: workspace,
    })
  } catch {
    if (options.isCurrent()) state.failSessionResolution(sessionKey, generation)
  }
}

export function createDraftProjectHydrationGuard() {
  let generation = 0
  let activeController: AbortController | null = null
  const abortActive = () => {
    activeController?.abort()
    activeController = null
  }
  return {
    begin: () => {
      abortActive()
      generation += 1
      return generation
    },
    invalidate: () => {
      abortActive()
      generation += 1
    },
    isCurrent: (candidate: number) => candidate === generation,
    createController: (candidate: number): AbortController | null => {
      if (candidate !== generation) return null
      abortActive()
      activeController = new AbortController()
      return activeController
    },
    complete: (candidate: number, controller: AbortController) => {
      if (candidate === generation && activeController === controller) {
        activeController = null
      }
    },
  }
}

export function useActiveProjectWorkspace() {
  const pendingWorkspaceId = ref<string | null>(null)
  const boundWorkspaceId = ref<string | null>(null)
  const activeWorkspace = ref<ActiveProjectWorkspaceSnapshot | null>(null)
  const status = ref<ActiveProjectWorkspaceStatus>('none')
  const refreshing = ref(false)
  let sessionGeneration = 0
  let resolvingSessionKey: string | null = null
  let hasResolvedSession = false
  // Unlike the visible in-flight flag, this remains set after refresh failure:
  // a project-catalog result cannot revalidate the session's retained binding.
  let sessionRefreshUnresolved = false

  function clearActiveProject() {
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    status.value = 'none'
  }

  function clearDraft() {
    sessionGeneration += 1
    resolvingSessionKey = null
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    pendingWorkspaceId.value = null
    clearActiveProject()
  }

  function beginProjectDraft(workspace: ActiveProjectWorkspaceSnapshot) {
    sessionGeneration += 1
    resolvingSessionKey = null
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    pendingWorkspaceId.value = workspace.id
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    status.value = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
  }

  function beginUnknownProjectDraft(workspaceId: string) {
    sessionGeneration += 1
    resolvingSessionKey = null
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    pendingWorkspaceId.value = workspaceId
    boundWorkspaceId.value = workspaceId
    activeWorkspace.value = null
    status.value = 'unknown'
  }

  function acceptPendingBinding(workspaceId: string | null) {
    if (pendingWorkspaceId.value === workspaceId) pendingWorkspaceId.value = null
  }

  function beginSessionResolution(sessionKey: string, preserveCurrent = false): number {
    sessionGeneration += 1
    resolvingSessionKey = sessionKey
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    pendingWorkspaceId.value = null
    // Same-key draft materialization has already validated this project. Keep
    // its panel identity while refreshing the newly durable session metadata.
    if (preserveCurrent) return sessionGeneration
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    status.value = 'resolving'
    return sessionGeneration
  }

  function beginSessionRefresh(sessionKey: string): number {
    if (sessionKey !== resolvingSessionKey || !hasResolvedSession) {
      return beginSessionResolution(sessionKey)
    }
    sessionGeneration += 1
    pendingWorkspaceId.value = null
    // Keep the last same-owner presentation, but revalidate operation readiness.
    // The caller must use initial resolution when the Gateway identity changes.
    sessionRefreshUnresolved = true
    refreshing.value = true
    status.value = 'resolving'
    return sessionGeneration
  }

  function applySessionSnapshot(
    sessionKey: string,
    generation: number,
    metadata: SessionProjectWorkspaceMetadata,
    options: { settled?: boolean } = {},
  ): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    const workspace = metadata.projectWorkspace || null
    boundWorkspaceId.value = metadata.workspaceId || workspace?.id || null
    activeWorkspace.value = workspace
    // A deferred cache projection preserves the chip, not refreshed authority.
    // Retain the established initial/draft hydration behavior outside refreshes.
    if (sessionRefreshUnresolved && options.settled === false) return true
    if (workspace?.removed) status.value = 'removed'
    else if (workspace?.available) status.value = 'ready'
    else if (workspace) status.value = 'unavailable'
    else if (metadata.workspaceId) status.value = 'unknown'
    else status.value = 'none'
    if (options.settled !== false) {
      hasResolvedSession = true
      sessionRefreshUnresolved = false
      refreshing.value = false
    }
    return true
  }

  function failSessionResolution(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    status.value = 'error'
    refreshing.value = false
    return true
  }

  function applyWorkspaceRefresh(
    workspace: ActiveProjectWorkspaceSnapshot | null,
  ): void {
    if (!boundWorkspaceId.value) return
    if (!workspace) {
      if (activeWorkspace.value) {
        activeWorkspace.value = {
          ...activeWorkspace.value,
          available: false,
          removed: true,
          availabilityReason: 'removed',
        }
      }
      if (!sessionRefreshUnresolved) status.value = 'removed'
      return
    }
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    if (sessionRefreshUnresolved) return
    status.value = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
  }

  function failWorkspaceRefresh(): void {
    if (!boundWorkspaceId.value || sessionRefreshUnresolved) return
    status.value = 'error'
  }

  const sendBlockedReason = computed(() =>
    status.value === 'none' || status.value === 'ready'
      ? null
      : status.value,
  )

  return {
    pendingWorkspaceId,
    boundWorkspaceId,
    activeWorkspace,
    status,
    refreshing,
    sendBlockedReason,
    beginProjectDraft,
    beginUnknownProjectDraft,
    acceptPendingBinding,
    beginSessionResolution,
    beginSessionRefresh,
    applySessionSnapshot,
    failSessionResolution,
    applyWorkspaceRefresh,
    failWorkspaceRefresh,
    clearDraft,
  }
}
