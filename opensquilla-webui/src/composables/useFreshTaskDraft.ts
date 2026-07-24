import { readonly, shallowRef } from 'vue'

export interface FreshTaskDraftRequest {
  id: number
  agentId: string
  workspaceId: string | null
}

const request = shallowRef<FreshTaskDraftRequest | null>(null)
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

  return {
    request: readonly(request),
    requestFreshTask,
  }
}
