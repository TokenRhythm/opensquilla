import { readonly, shallowRef } from 'vue'

export interface FreshTaskDraftRequest {
  id: number
  agentId: string
  workspaceId: string | null
}

const request = shallowRef<FreshTaskDraftRequest | null>(null)
const materializedWorkspaceBySession = shallowRef<Record<string, string>>({})
let nextRequestId = 0

/**
 * App-wide signal for an explicit "new task" action.
 *
 * Route navigation alone cannot represent clicking the pencil twice while the
 * user is already on that project's draft URL. The monotonically increasing
 * request id makes every click observable without leaking a nonce into the URL.
 */
export function useFreshTaskDraft() {
  function requestFreshTask(agentId = 'main', workspaceId?: string | null) {
    request.value = {
      id: ++nextRequestId,
      agentId: agentId || 'main',
      workspaceId: workspaceId || null,
    }
  }

  function bindMaterializedProjectTask(sessionKey: string, workspaceId: string) {
    if (!sessionKey || !workspaceId) return
    if (materializedWorkspaceBySession.value[sessionKey] === workspaceId) return
    materializedWorkspaceBySession.value = {
      ...materializedWorkspaceBySession.value,
      [sessionKey]: workspaceId,
    }
  }

  function confirmMaterializedProjectTask(sessionKey: string, workspaceId?: string | null) {
    const optimisticWorkspaceId = materializedWorkspaceBySession.value[sessionKey]
    if (!optimisticWorkspaceId || !workspaceId) return
    const { [sessionKey]: _confirmed, ...remaining } = materializedWorkspaceBySession.value
    materializedWorkspaceBySession.value = remaining
  }

  function forgetMaterializedProjectTask(sessionKey: string) {
    if (!materializedWorkspaceBySession.value[sessionKey]) return
    const { [sessionKey]: _forgotten, ...remaining } = materializedWorkspaceBySession.value
    materializedWorkspaceBySession.value = remaining
  }

  return {
    request: readonly(request),
    materializedWorkspaceBySession: readonly(materializedWorkspaceBySession),
    requestFreshTask,
    bindMaterializedProjectTask,
    confirmMaterializedProjectTask,
    forgetMaterializedProjectTask,
  }
}
