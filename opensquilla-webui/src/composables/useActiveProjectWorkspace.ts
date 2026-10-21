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

/** Session metadata describes registration, not current filesystem availability. */
export async function applySessionProjectWorkspaceMetadata(options: {
  state: ReturnType<typeof useActiveProjectWorkspace>
  sessionKey: string
  generation: number
  metadata: SessionProjectWorkspaceMetadata & { projectWorkspaceDeferred?: boolean }
  cachedWorkspace?: ActiveProjectWorkspaceSnapshot | null
  sameSessionIncarnation?: boolean
  loadWorkspaces: () => Promise<readonly ActiveProjectWorkspaceSnapshot[]>
  isCurrent: () => boolean
}): Promise<void> {
  if (!options.isCurrent()) return
  const { state, sessionKey, generation, metadata } = options
  if (options.sameSessionIncarnation === false
    && !state.invalidateSessionIncarnation(sessionKey, generation)) return
  const workspaceId = metadata.workspaceId || metadata.projectWorkspace?.id
  const known = state.activeWorkspace.value?.id === workspaceId
    ? state.activeWorkspace.value : options.cachedWorkspace?.id === workspaceId
      ? options.cachedWorkspace : null
  // Only a same-owner refresh can reuse a physical check. The caller fences
  // Gateway identity and session incarnation before beginning that refresh.
  if (state.reuseSessionRegistration(sessionKey, generation, metadata)) return
  if (!workspaceId || metadata.projectWorkspace?.removed) {
    state.applySessionSnapshot(sessionKey, generation, metadata)
    return
  }
  if (!state.applySessionSnapshot(sessionKey, generation, {
    ...metadata,
    workspaceId,
    projectWorkspace: metadata.projectWorkspace || known,
  }, { settled: false })) return
  try {
    const workspaces = await options.loadWorkspaces()
    if (!options.isCurrent()) return
    const workspace = workspaces.find(item => item.id === workspaceId)
      || (metadata.projectWorkspace || known
        ? { ...(metadata.projectWorkspace || known)!, available: false, removed: true } : null)
    state.applySessionSnapshot(sessionKey, generation, {
      workspaceId,
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
  // Presentation stays settled during same-owner metadata refreshes. Operation
  // readiness remains fail-closed in `status`, including invisible refreshes.
  const presentationStatus = ref<ActiveProjectWorkspaceStatus>('none')
  const refreshing = ref(false)
  let physicalStatus: ActiveProjectWorkspaceStatus = 'none'
  let sessionGeneration = 0
  let resolvingSessionKey: string | null = null
  let hasResolvedSession = false
  // Unlike the visible in-flight flag, this remains set after refresh failure:
  // a project-catalog result cannot revalidate the session's retained binding.
  let sessionRefreshUnresolved = false

  function settleStatus(value: ActiveProjectWorkspaceStatus) {
    status.value = presentationStatus.value = physicalStatus = value
  }

  function clearActiveProject() {
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    settleStatus('none')
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
    settleStatus(workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable')
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
    settleStatus('unknown')
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
    settleStatus('resolving')
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

  function invalidateSessionIncarnation(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) return false
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    settleStatus('resolving')
    return true
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
    if (options.settled === false && (boundWorkspaceId.value !== (metadata.workspaceId || workspace?.id || null)
      || (workspace && activeWorkspace.value?.path !== workspace.path))) {
      hasResolvedSession = false
      physicalStatus = 'unknown'
      presentationStatus.value = 'resolving'
    }
    boundWorkspaceId.value = metadata.workspaceId || workspace?.id || null
    activeWorkspace.value = workspace
    // A registration or cached projection never authorizes an unchecked path.
    if (options.settled === false) {
      status.value = 'resolving'
      return true
    }
    if (workspace?.removed) settleStatus('removed')
    else if (workspace?.available) settleStatus('ready')
    else if (workspace) settleStatus('unavailable')
    else if (metadata.workspaceId) settleStatus('unknown')
    else settleStatus('none')
    hasResolvedSession = true
    sessionRefreshUnresolved = false
    refreshing.value = false
    return true
  }

  function reuseSessionRegistration(
    sessionKey: string,
    generation: number,
    metadata: SessionProjectWorkspaceMetadata,
  ): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration
      || !hasResolvedSession || !sessionRefreshUnresolved) return false
    const registered = metadata.projectWorkspace
    const id = metadata.workspaceId || registered?.id || null
    const known = activeWorkspace.value
    if (id !== boundWorkspaceId.value || (id && (!known || !registered
      || registered.id !== id || registered.path !== known.path || registered.removed
      || !registered.available))) return false
    // Refresh registration/name, but retain the last physical result, including
    // unavailable/error. A successful metadata read cannot heal a directory.
    if (registered && known) activeWorkspace.value = { ...known, name: registered.name }
    status.value = presentationStatus.value = physicalStatus
    sessionRefreshUnresolved = false
    refreshing.value = false
    return true
  }

  function failSessionResolution(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    status.value = presentationStatus.value = 'error'
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
      physicalStatus = 'removed'
      if (!sessionRefreshUnresolved || presentationStatus.value !== 'error') presentationStatus.value = physicalStatus
      if (!sessionRefreshUnresolved) status.value = physicalStatus
      return
    }
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    physicalStatus = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
    if (!sessionRefreshUnresolved || presentationStatus.value !== 'error') presentationStatus.value = physicalStatus
    if (!sessionRefreshUnresolved) status.value = physicalStatus
  }

  function failWorkspaceRefresh(): void {
    if (!boundWorkspaceId.value || sessionRefreshUnresolved) return
    settleStatus('error')
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
    presentationStatus,
    refreshing,
    sendBlockedReason,
    beginProjectDraft,
    beginUnknownProjectDraft,
    acceptPendingBinding,
    beginSessionResolution,
    beginSessionRefresh,
    invalidateSessionIncarnation,
    applySessionSnapshot,
    reuseSessionRegistration,
    failSessionResolution,
    applyWorkspaceRefresh,
    failWorkspaceRefresh,
    clearDraft,
  }
}
