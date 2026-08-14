import type { Ref } from 'vue'
import type {
  ChatMessage,
  ChatRunStatusSource,
} from '@/types/chat'
import type { PersistSessionOptions } from '@/composables/chat/useChatSessionRoute'
import type { SessionBootstrapRun } from '@/composables/chat/useChatSessionBootstrap'
import type { SessionSubscriptionResult } from '@/composables/chat/useChatSessionSubscription'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'

export interface ChatUsageAccumulator {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  cost: number | null
  routedTurns: number
  sessionSaved: number
}

export interface ResponseSessionAdoptionResult {
  authoritative: boolean
  authoritativeIdle: boolean
  backgroundOnly: boolean
}

export interface UseChatSessionRuntimeOptions {
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  pendingSessionIntent: Ref<string | null>
  routerDecisionPending: Ref<unknown | null>
  currentEpoch: Ref<number>
  lastStreamSeq: Ref<number>
  activeTaskGroups: Ref<Set<string>>
  taskOwnership?: ChatTaskOwnershipApi
  activeStreamTaskId?: Ref<string>
  activeStreamSessionKey?: Ref<string>
  acceptanceStopPending?: Ref<boolean>
  aborted: Ref<boolean>
  lastHeaderRole: Ref<string>
  lastHeaderDay: Ref<string>
  usageAccum: Ref<ChatUsageAccumulator>
  usageModel: Ref<string>
  createSessionKey: (agentId?: string) => string
  persistSession: (key: string, options?: PersistSessionOptions) => void
  cancelSessionBootstrap: () => void
  startSessionBootstrap: (options?: {
    includeHistory?: boolean
    force?: boolean
  }) => SessionBootstrapRun
  loadCurrentSessionUsage: () => void | Promise<void>
  applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void
  setCompactInFlight: (active: boolean, key?: string) => void
  hideCompactStatus: () => void
  clearPendingQueue: () => void
  switchPendingQueue: (targetSessionKey: string) => void | Promise<void>
  adoptPendingQueue: (
    targetSessionKey: string,
    ownerRequestId: string,
  ) => void | Promise<void>
  resetSavingsPopupCooldown: () => void
  restoreWidgetState: () => void
  resetStreamLiveTurnState: () => void
  resetDraftComposer?: () => void
}

const EMPTY_USAGE: ChatUsageAccumulator = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  cost: null,
  routedTurns: 0,
  sessionSaved: 0,
}

function createEmptyUsage(): ChatUsageAccumulator {
  return { ...EMPTY_USAGE }
}

export function useChatSessionRuntime(options: UseChatSessionRuntimeOptions) {
  function resetLiveTurnState() {
    options.resetStreamLiveTurnState()
    options.aborted.value = false
    options.routerDecisionPending.value = null
  }

  function resetSessionRuntimeState() {
    options.currentEpoch.value = 0
    options.lastStreamSeq.value = 0
    options.activeTaskGroups.value.clear()
    options.taskOwnership?.reset(false)
    // Stream identity is session-local control state. Keeping A's owner while
    // switching to an idle B can make Stop target A or let B's idle hydrate
    // release B's pending queue based on stale evidence.
    if (options.activeStreamTaskId) options.activeStreamTaskId.value = ''
    if (options.activeStreamSessionKey) options.activeStreamSessionKey.value = ''
    if (options.acceptanceStopPending) options.acceptanceStopPending.value = false
    resetLiveTurnState()
  }

  function resetSessionViewState() {
    options.messages.value = []
    options.lastHeaderRole.value = ''
    options.lastHeaderDay.value = ''
    options.usageAccum.value = createEmptyUsage()
    options.usageModel.value = ''
    options.resetSavingsPopupCooldown()
  }

  function resetCompactState() {
    options.setCompactInFlight(false)
    options.hideCompactStatus()
  }

  function resetCurrentSessionAfterSlash() {
    resetSessionRuntimeState()
    resetCompactState()
    options.clearPendingQueue()
    resetSessionViewState()
  }

  async function switchSession(
    key: string,
    pendingQueuePolicy:
      | { kind: 'navigate' }
      | { kind: 'response_handoff'; ownerRequestId: string },
  ): Promise<ResponseSessionAdoptionResult | undefined> {
    if (!key || key === options.sessionKey.value) return

    options.cancelSessionBootstrap()
    resetCompactState()
    if (pendingQueuePolicy.kind === 'response_handoff') {
      await options.adoptPendingQueue(key, pendingQueuePolicy.ownerRequestId)
    } else {
      const pendingQueueSwitch = options.switchPendingQueue(key)
      if (pendingQueueSwitch) await pendingQueueSwitch
    }
    options.persistSession(key, { source: 'runtime.switchToSession' })
    resetSessionRuntimeState()
    options.pendingSessionIntent.value = null
    options.applySessionRunState({ run_status: 'idle' })
    resetSessionViewState()
    options.restoreWidgetState()
    // History and live are launched together by the coordinator but remain
    // orthogonal. Response hand-off only waits for the authoritative live
    // snapshot; history can recover independently without blocking adoption.
    const bootstrap = options.startSessionBootstrap({ includeHistory: true })
    // Usage is optional metadata. Start it once the critical request frames are
    // queued; a slow history response must not withhold the rest of the UI.
    void bootstrap.criticalRequestsQueued.then(() => {
      if (options.sessionKey.value === key) void options.loadCurrentSessionUsage()
    })
    const subscriptionOutcome = await bootstrap.live
    return {
      authoritative: subscriptionOutcome?.authoritative === true,
      authoritativeIdle: subscriptionOutcome?.authoritative === true
        && subscriptionOutcome.live === false,
      backgroundOnly: subscriptionOutcome?.authoritative === true
        && subscriptionOutcome.backgroundOnly === true,
    }
  }

  function switchToSession(key: string) {
    return switchSession(key, { kind: 'navigate' })
  }

  function adoptResponseSession(key: string, ownerRequestId: string) {
    return switchSession(key, { kind: 'response_handoff', ownerRequestId })
  }

  async function rebindDraftSession(
    key: string,
    guard: DraftSessionRebindGuard,
  ): Promise<SessionSubscriptionResult> {
    const sourceSessionKey = options.sessionKey.value
    if (!key || key === sourceSessionKey || !guard(sourceSessionKey)) return false

    options.cancelSessionBootstrap()
    if (options.sessionKey.value !== sourceSessionKey) return false
    if (!guard(sourceSessionKey)) {
      return options.startSessionBootstrap({ includeHistory: false, force: true }).live
    }

    resetCompactState()
    const pendingQueueSwitch = options.switchPendingQueue(key)
    if (pendingQueueSwitch) await pendingQueueSwitch
    // A recovered provisional draft remains a draft: do not write it to the URL
    // or active-session storage before the first accepted send.
    options.sessionKey.value = key
    resetSessionRuntimeState()
    options.pendingSessionIntent.value = 'new_chat'
    options.applySessionRunState({ run_status: 'idle' })
    resetSessionViewState()
    options.restoreWidgetState()
    return options.startSessionBootstrap({ includeHistory: false }).live
  }

  // Drafts keep their provisional key out of the URL and local storage; it
  // only persists once the first message actually goes out.
  async function startDraftSession(agentId?: string) {
    options.cancelSessionBootstrap()
    const key = options.createSessionKey(agentId)
    resetCompactState()
    const pendingQueueSwitch = options.switchPendingQueue(key)
    if (pendingQueueSwitch) await pendingQueueSwitch
    options.sessionKey.value = key
    resetSessionRuntimeState()
    // A brand-new provisional key cannot own a durable Gateway task yet. Its
    // first send must not wait for optional draft bootstrap metadata.
    options.taskOwnership?.reset(true)
    options.pendingSessionIntent.value = 'new_chat'
    options.resetDraftComposer?.()
    resetSessionViewState()
    options.startSessionBootstrap({ includeHistory: false })
  }

  return {
    resetCurrentSessionAfterSlash,
    startDraftSession,
    switchToSession,
    adoptResponseSession,
    rebindDraftSession,
  }
}

export type DraftSessionRebindGuard = (sourceSessionKey: string) => boolean
