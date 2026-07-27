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

export function createDraftProjectHydrationGuard() {
  let generation = 0
  return {
    begin: () => {
      generation += 1
      return generation
    },
    invalidate: () => {
      generation += 1
    },
    isCurrent: (candidate: number) => candidate === generation,
  }
}

export function useActiveProjectWorkspace() {
  const pendingWorkspaceId = ref<string | null>(null)
  const boundWorkspaceId = ref<string | null>(null)
  const activeWorkspace = ref<ActiveProjectWorkspaceSnapshot | null>(null)
  const status = ref<ActiveProjectWorkspaceStatus>('none')
  let sessionGeneration = 0
  let resolvingSessionKey: string | null = null

  function clearActiveProject() {
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    status.value = 'none'
  }

  function clearDraft() {
    sessionGeneration += 1
    resolvingSessionKey = null
    pendingWorkspaceId.value = null
    clearActiveProject()
  }

  function beginProjectDraft(workspace: ActiveProjectWorkspaceSnapshot) {
    sessionGeneration += 1
    resolvingSessionKey = null
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
    pendingWorkspaceId.value = workspaceId
    boundWorkspaceId.value = workspaceId
    activeWorkspace.value = null
    status.value = 'unknown'
  }

  function acceptPendingBinding(workspaceId: string | null) {
    if (pendingWorkspaceId.value === workspaceId) pendingWorkspaceId.value = null
  }

  function beginSessionResolution(sessionKey: string): number {
    sessionGeneration += 1
    resolvingSessionKey = sessionKey
    pendingWorkspaceId.value = null
    boundWorkspaceId.value = null
    activeWorkspace.value = null
    status.value = 'resolving'
    return sessionGeneration
  }

  function applySessionSnapshot(
    sessionKey: string,
    generation: number,
    metadata: SessionProjectWorkspaceMetadata,
  ): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    const workspace = metadata.projectWorkspace || null
    boundWorkspaceId.value = metadata.workspaceId || workspace?.id || null
    activeWorkspace.value = workspace
    if (workspace?.removed) status.value = 'removed'
    else if (workspace?.available) status.value = 'ready'
    else if (workspace) status.value = 'unavailable'
    else if (metadata.workspaceId) status.value = 'unknown'
    else status.value = 'none'
    return true
  }

  function failSessionResolution(sessionKey: string, generation: number): boolean {
    if (sessionKey !== resolvingSessionKey || generation !== sessionGeneration) {
      return false
    }
    status.value = 'error'
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
      status.value = 'removed'
      return
    }
    boundWorkspaceId.value = workspace.id
    activeWorkspace.value = workspace
    status.value = workspace.removed
      ? 'removed'
      : workspace.available
        ? 'ready'
        : 'unavailable'
  }

  function failWorkspaceRefresh(): void {
    if (!boundWorkspaceId.value) return
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
    sendBlockedReason,
    beginProjectDraft,
    beginUnknownProjectDraft,
    acceptPendingBinding,
    beginSessionResolution,
    applySessionSnapshot,
    failSessionResolution,
    applyWorkspaceRefresh,
    failWorkspaceRefresh,
    clearDraft,
  }
}
