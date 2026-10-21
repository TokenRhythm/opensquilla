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
  epoch?: number
}

/** Session metadata describes registration, not current filesystem availability. */
export async function applySessionProjectWorkspaceMetadata(options: {
  state: ReturnType<typeof useActiveProjectWorkspace>
  sessionKey: string
  generation: number
  metadata: SessionProjectWorkspaceMetadata & { projectWorkspaceDeferred?: boolean }
  cachedWorkspace?: ActiveProjectWorkspaceSnapshot | null
  /** A physical catalog result fenced by the current Gateway and mutation generation. */
  validatedWorkspace?: ActiveProjectWorkspaceSnapshot | null
  sameSessionIncarnation?: boolean
  loadWorkspaces: () => Promise<readonly ActiveProjectWorkspaceSnapshot[]>
  isCurrent: () => boolean
}): Promise<void> {
  if (!options.isCurrent()) return
  const { state, sessionKey, generation, metadata } = options
  const sameIncarnation = state.observeSessionIncarnation(sessionKey, generation, metadata.epoch)
  if ((options.sameSessionIncarnation === false || !sameIncarnation)
    && !state.invalidateSessionIncarnation(sessionKey, generation)) return
  const workspaceId = metadata.workspaceId || metadata.projectWorkspace?.id
  const known = state.activeWorkspace.value?.id === workspaceId
    ? state.activeWorkspace.value : options.cachedWorkspace?.id === workspaceId
      ? options.cachedWorkspace : null
  if (!workspaceId || metadata.projectWorkspace?.removed) {
    state.applySessionSnapshot(sessionKey, generation, metadata)
    return
  }
  // Prefer a current catalog result over an older session's physical result.
  // This also reflects removals discovered while another project was active.
  if (sameIncarnation && options.sameSessionIncarnation !== false
    && options.validatedWorkspace !== undefined) {
    const checked = options.validatedWorkspace
    const registration = metadata.projectWorkspace
    if (registration?.id === workspaceId
      && (!checked || (checked.id === workspaceId && checked.path === registration.path))) {
      state.applySessionSnapshot(sessionKey, generation, {
        ...metadata, workspaceId,
        projectWorkspace: checked ? { ...checked, name: registration.name }
          : { ...registration, available: false, removed: true, availabilityReason: 'removed' },
      })
      return
    }
  }
  if (state.reuseSessionRegistration(sessionKey, generation, metadata)) return
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
  let presentationScope: string | null = null
  let currentSessionScope: string | null = null
  let sessionIncarnation: number | undefined
  let physicalValidated = false
  // View-lifetime presentation only. A return visit still waits for current
  // session metadata, and every send performs its own physical preflight.
  const sessionPresentations = new Map<string, {
    workspaceId: string | null
    workspace: ActiveProjectWorkspaceSnapshot | null
    physicalStatus: ActiveProjectWorkspaceStatus
    presentationStatus: ActiveProjectWorkspaceStatus
    physicalValidated: boolean
    epoch?: number
  }>()

  function rememberSessionPresentation() {
    if (!presentationScope || currentSessionScope !== presentationScope
      || !resolvingSessionKey || !hasResolvedSession || sessionRefreshUnresolved) return
    sessionPresentations.set(resolvingSessionKey, {
      workspaceId: boundWorkspaceId.value,
      workspace: activeWorkspace.value ? { ...activeWorkspace.value } : null,
      physicalStatus, presentationStatus: presentationStatus.value, physicalValidated, epoch: sessionIncarnation,
    })
  }

  function setPresentationScope(scope: string | null) {
    if (scope === presentationScope) return
    presentationScope = scope
    currentSessionScope = null
    physicalValidated = false
    sessionPresentations.clear()
  }

  function beginSessionVisit(sessionKey: string): number {
    rememberSessionPresentation()
    const remembered = presentationScope ? sessionPresentations.get(sessionKey) : undefined
    const generation = beginSessionResolution(sessionKey)
    if (!remembered) return generation
    boundWorkspaceId.value = remembered.workspaceId
    activeWorkspace.value = remembered.workspace ? { ...remembered.workspace } : null
    physicalStatus = remembered.physicalStatus
    physicalValidated = remembered.physicalValidated
    presentationStatus.value = remembered.presentationStatus
    sessionIncarnation = remembered.epoch
    hasResolvedSession = true
    sessionRefreshUnresolved = true
    refreshing.value = true
    return generation
  }

  function invalidatePhysicalSnapshots() {
    physicalValidated = false
    for (const remembered of sessionPresentations.values()) remembered.physicalValidated = false
  }

  function observeSessionIncarnation(sessionKey: string, generation: number, epoch?: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) return false
    const same = sessionIncarnation === undefined || epoch === undefined || sessionIncarnation === epoch
    if (epoch !== undefined) sessionIncarnation = epoch
    return same
  }

  function settleStatus(value: ActiveProjectWorkspaceStatus) {
    status.value = presentationStatus.value = physicalStatus = value
  }

  function clearActiveProject() {
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    settleStatus('none')
  }

  function clearDraft() {
    rememberSessionPresentation()
    sessionGeneration += 1
    resolvingSessionKey = null
    hasResolvedSession = false
    sessionRefreshUnresolved = false
    refreshing.value = false
    pendingWorkspaceId.value = null
    clearActiveProject()
  }

  function beginProjectDraft(workspace: ActiveProjectWorkspaceSnapshot) {
    rememberSessionPresentation()
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
    rememberSessionPresentation()
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
    currentSessionScope = presentationScope
    sessionIncarnation = undefined
    physicalValidated = false
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
    physicalValidated = false
    sessionPresentations.delete(sessionKey)
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
    physicalValidated = true
    sessionRefreshUnresolved = false
    refreshing.value = false
    rememberSessionPresentation()
    return true
  }

  function reuseSessionRegistration(
    sessionKey: string,
    generation: number,
    metadata: SessionProjectWorkspaceMetadata,
  ): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration
      || !hasResolvedSession || !sessionRefreshUnresolved || !physicalValidated) return false
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
    rememberSessionPresentation()
    return true
  }

  function failSessionResolution(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    status.value = presentationStatus.value = 'error'
    const remembered = sessionPresentations.get(sessionKey)
    if (remembered) remembered.presentationStatus = 'error'
    refreshing.value = false
    return true
  }

  function applyWorkspaceRefresh(
    workspace: ActiveProjectWorkspaceSnapshot | null,
  ): void {
    // A completed physical check also updates inactive visits to this project.
    // Otherwise switching back could briefly resurrect a removed directory.
    for (const [key, remembered] of sessionPresentations) {
      if (!remembered.workspaceId || remembered.workspaceId !== boundWorkspaceId.value) continue
      if (workspace && remembered.workspace?.path !== workspace.path) {
        sessionPresentations.delete(key)
        continue
      }
      remembered.workspace = workspace ? { ...workspace } : remembered.workspace
        ? { ...remembered.workspace, available: false, removed: true, availabilityReason: 'removed' } : null
      remembered.physicalStatus = remembered.presentationStatus = !workspace || workspace.removed ? 'removed'
        : workspace.available ? 'ready' : 'unavailable'
      remembered.physicalValidated = true
    }
    if (!boundWorkspaceId.value) return
    physicalValidated = true
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
      rememberSessionPresentation()
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
    rememberSessionPresentation()
  }

  function failWorkspaceRefresh(): void {
    if (!boundWorkspaceId.value || sessionRefreshUnresolved) return
    settleStatus('error')
    physicalValidated = true
    for (const remembered of sessionPresentations.values()) {
      if (remembered.workspaceId !== boundWorkspaceId.value) continue
      remembered.physicalStatus = remembered.presentationStatus = 'error'
      remembered.physicalValidated = true
    }
    rememberSessionPresentation()
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
    beginSessionVisit,
    setPresentationScope,
    invalidatePhysicalSnapshots,
    observeSessionIncarnation,
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
