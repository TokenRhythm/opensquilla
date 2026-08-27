import type { Ref } from 'vue'
import type {
  ChatMessage,
  ChatRunStatusSource,
} from '@/types/chat'
import type { PersistSessionOptions } from '@/composables/chat/useChatSessionRoute'
import type { SessionBootstrapRun } from '@/composables/chat/useChatSessionBootstrap'
import type { SessionSubscriptionResult } from '@/composables/chat/useChatSessionSubscription'
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import {
  beginSessionHandoffDiag,
  finishSessionHandoffDiag,
} from '@/utils/chat/sessionNavigationDiag'

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
  beginSessionResolution?: (key: string) => void
  cancelSessionBootstrap: (unsubscribe?: boolean) => void
  setSessionHandoffTarget?: (
    targetKey: string | null,
    epoch: number,
    outcome?: 'committed' | 'unchanged' | 'failed' | 'superseded',
  ) => SessionBootstrapRun | undefined
  resumeSessionBootstrap?: (run: SessionBootstrapRun) => void
  startSessionBootstrap: (options?: {
    includeHistory?: boolean
    force?: boolean
  }) => SessionBootstrapRun
  loadCurrentSessionUsage: () => void | Promise<void>
  applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void
  setCompactInFlight: (active: boolean, key?: string) => void
  hideCompactStatus: () => void
  clearPendingQueue: () => void
  switchPendingQueue: (
    targetSessionKey: string,
    shouldCommit?: () => boolean,
    handoffSignal?: AbortSignal,
  ) => void | Promise<void>
  adoptPendingQueue: (
    targetSessionKey: string,
    ownerRequestId: string,
    shouldCommit?: () => boolean,
    handoffSignal?: AbortSignal,
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
  let handoffEpoch = 0
  let handoffTargetKey = ''
  let handoffController: AbortController | null = null

  function beginHandoff(targetKey: string) {
    handoffController?.abort()
    const controller = new AbortController()
    handoffController = controller
    const epoch = ++handoffEpoch
    handoffTargetKey = targetKey
    beginSessionHandoffDiag(epoch, targetKey)
    options.setSessionHandoffTarget?.(targetKey, epoch)
    return { epoch, signal: controller.signal }
  }

  function isCurrentHandoff(
    epoch: number,
    targetKey: string,
    sourceKey: string,
  ) {
    return (
      handoffEpoch === epoch
      && handoffTargetKey === targetKey
      && options.sessionKey.value === sourceKey
    )
  }

  function finishHandoff(
    epoch: number,
    outcome: 'committed' | 'unchanged' | 'failed' | 'superseded',
  ) {
    finishSessionHandoffDiag(epoch, outcome)
    if (handoffEpoch !== epoch) return
    handoffTargetKey = ''
    handoffController = null
    const resumed = options.setSessionHandoffTarget?.(null, epoch, outcome)
    if (resumed) options.resumeSessionBootstrap?.(resumed)
  }

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
    if (!key) return
    const sourceKey = options.sessionKey.value
    const { epoch, signal: handoffSignal } = beginHandoff(key)
    if (key === sourceKey) {
      finishHandoff(epoch, 'unchanged')
      return
    }
    const shouldCommit = () => isCurrentHandoff(epoch, key, sourceKey)

    try {
      if (pendingQueuePolicy.kind === 'response_handoff') {
        await options.adoptPendingQueue(
          key,
          pendingQueuePolicy.ownerRequestId,
          shouldCommit,
          handoffSignal,
        )
      } else {
        const pendingQueueSwitch = options.switchPendingQueue(
          key,
          shouldCommit,
          handoffSignal,
        )
        if (pendingQueueSwitch) await pendingQueueSwitch
      }
    } catch (error) {
      finishHandoff(epoch, 'failed')
      throw error
    }
    if (!shouldCommit()) {
      finishHandoff(epoch, 'superseded')
      return
    }

    // Commit is deliberately synchronous from the logical cancellation
    // through the next bootstrap. unsubscribeSession sends its generation-
    // pinned frame before cancelSessionBootstrap returns, so B never waits for
    // A's ACK and no connected event can observe a half-switched route.
    options.cancelSessionBootstrap()
    resetCompactState()
    options.beginSessionResolution?.(key)
    options.persistSession(key, { source: 'runtime.switchToSession' })
    resetSessionRuntimeState()
    options.pendingSessionIntent.value = null
    options.applySessionRunState({ run_status: 'idle' })
    resetSessionViewState()
    options.restoreWidgetState()
    // History and live are launched together by the coordinator but remain
    // orthogonal. Response hand-off only waits for the authoritative live
    // snapshot; history can recover independently without blocking adoption.
    let bootstrap: SessionBootstrapRun
    try {
      bootstrap = options.startSessionBootstrap({ includeHistory: true })
    } finally {
      finishHandoff(epoch, 'committed')
    }
    // Usage is optional metadata. Start it once the critical request frames are
    // queued; a slow history response must not withhold the rest of the UI.
    void bootstrap.criticalRequestsQueued.then(() => {
      if (
        handoffEpoch === epoch
        && options.sessionKey.value === key
      ) void options.loadCurrentSessionUsage()
    })
    const subscriptionOutcome = await bootstrap.live
    if (handoffEpoch !== epoch || options.sessionKey.value !== key) return
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
    if (!key || !guard(sourceSessionKey)) return false
    const { epoch, signal: handoffSignal } = beginHandoff(key)
    if (key === sourceSessionKey) {
      finishHandoff(epoch, 'unchanged')
      return false
    }
    const shouldCommit = () => (
      isCurrentHandoff(epoch, key, sourceSessionKey)
      && guard(sourceSessionKey)
    )

    try {
      const pendingQueueSwitch = options.switchPendingQueue(
        key,
        shouldCommit,
        handoffSignal,
      )
      if (pendingQueueSwitch) await pendingQueueSwitch
    } catch (error) {
      finishHandoff(epoch, 'failed')
      throw error
    }
    if (!shouldCommit()) {
      finishHandoff(epoch, 'superseded')
      return false
    }
    options.cancelSessionBootstrap()
    resetCompactState()
    // A recovered provisional draft remains a draft: do not write it to the URL
    // or active-session storage before the first accepted send.
    options.sessionKey.value = key
    resetSessionRuntimeState()
    options.pendingSessionIntent.value = 'new_chat'
    options.applySessionRunState({ run_status: 'idle' })
    resetSessionViewState()
    options.restoreWidgetState()
    let live: Promise<SessionSubscriptionResult>
    try {
      live = options.startSessionBootstrap({ includeHistory: false }).live
    } finally {
      finishHandoff(epoch, 'committed')
    }
    const outcome = await live
    return handoffEpoch === epoch && options.sessionKey.value === key
      ? outcome
      : false
  }

  // Drafts keep their provisional key out of the URL and local storage; it
  // only persists once the first message actually goes out.
  async function startDraftSession(agentId?: string) {
    const key = options.createSessionKey(agentId)
    const sourceKey = options.sessionKey.value
    const { epoch, signal: handoffSignal } = beginHandoff(key)
    const shouldCommit = () => isCurrentHandoff(epoch, key, sourceKey)
    try {
      const pendingQueueSwitch = options.switchPendingQueue(
        key,
        shouldCommit,
        handoffSignal,
      )
      if (pendingQueueSwitch) await pendingQueueSwitch
    } catch (error) {
      finishHandoff(epoch, 'failed')
      throw error
    }
    if (!shouldCommit()) {
      finishHandoff(epoch, 'superseded')
      return
    }
    options.cancelSessionBootstrap()
    resetCompactState()
    options.sessionKey.value = key
    resetSessionRuntimeState()
    // A brand-new provisional key cannot own a durable Gateway task yet. Its
    // first send must not wait for optional draft bootstrap metadata.
    options.taskOwnership?.reset(true)
    options.pendingSessionIntent.value = 'new_chat'
    options.resetDraftComposer?.()
    resetSessionViewState()
    try {
      options.startSessionBootstrap({ includeHistory: false })
    } finally {
      finishHandoff(epoch, 'committed')
    }
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
