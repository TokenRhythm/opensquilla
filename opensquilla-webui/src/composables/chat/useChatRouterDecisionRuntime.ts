import type {
  ConversationEnsembleProgress,
  ConversationEventIdentity,
  ConversationRoutingDecision,
} from '@/modules/conversationEventContent'
import { ref, type Ref } from 'vue'
import type {
  ChatEnsembleMeta,
  ChatEnsembleMetaModel,
  ChatMessage,
} from '@/types/chat'

import { normalizeEnsembleMemberRole } from '@/utils/ensembleRoles'
import {
  type NormalizedRouterDecision,
  normalizeRouterDecision,
  shortModelName,
} from '@/composables/chat/useChatRenderedMessages'

export interface UseChatRouterDecisionRuntimeOptions {
  messages: Ref<ChatMessage[]>
  sessionKey: Ref<string>
  isStreaming: Ref<boolean>
  autoScroll: Ref<boolean>
  activeTurnUsesEnsemble: Readonly<Ref<boolean>>
  activeTurnId: Readonly<Ref<string>>
  streamBubble: Ref<boolean>
  streamHasVisibleOutput: Ref<boolean>
  startStreaming: () => void
  resetStreamForRouterReplay: () => void
  resetStreamIdleTimer: () => void
  setStreamActivity: (label: string) => void
  scrollToBottom: () => void
}

export function useChatRouterDecisionRuntime(options: UseChatRouterDecisionRuntimeOptions) {
  const pendingRouterDecision = ref<{
    payload: ConversationRoutingDecision
    decision: NormalizedRouterDecision
    messageId: string
  } | null>(null)
  let localRouterMessageSeq = 0
  let localReplaySeq = 0
  let routerSessionKey = options.sessionKey.value
  const replayKeys = new Map<string, string>()
  const routerMessageAttempts = new Map<string, {
    turnId: string
    replayKey: string
    provisional: boolean
  }>()

  function syncRouterSession() {
    if (routerSessionKey === options.sessionKey.value) return
    routerSessionKey = options.sessionKey.value
    replayKeys.clear()
    routerMessageAttempts.clear()
    pendingRouterDecision.value = null
  }

  function replayKeyForTurn(turnId: string): string {
    syncRouterSession()
    return replayKeys.get(turnId) || ''
  }

  function rememberRouterMessage(message: ChatMessage, provisional: boolean) {
    const turnId = message.turnId || ''
    if (provisional) message.clientId = message.messageId
    routerMessageAttempts.set(message.messageId!, {
      turnId,
      replayKey: replayKeyForTurn(turnId),
      provisional,
    })
  }

  function belongsToCurrentAttempt(message: ChatMessage, turnId: string): boolean {
    const replayKey = replayKeyForTurn(turnId)
    const attempt = routerMessageAttempts.get(message.messageId || '')
    // History rows have no local attempt metadata. A replay must never reuse
    // one as the destination for a new physical attempt's live state.
    return attempt
      ? attempt.turnId === turnId && attempt.replayKey === replayKey
      : !replayKey
  }

  function resetRouterReplayCursor() {
    syncRouterSession()
    // An authoritative snapshot starts at the beginning of the turn. Keep
    // card ownership, but rewind the cursor before replaying its boundaries.
    replayKeys.clear()
  }

  // Router and ensemble events can arrive throughout a long streamed answer.
  // They should follow the live edge only while the reader has elected to stay
  // there; otherwise every event would pull an upward-scrolled reader back down.
  function scrollToBottomIfFollowing() {
    if (options.autoScroll.value) options.scrollToBottom()
  }

  function handleRouterControlReplay(payload: ConversationEventIdentity = {}, identityStreamSeq?: number) {
    syncRouterSession()
    if (payload.key && payload.key !== options.sessionKey.value) return
    const turnId = payloadTurnId(payload) || latestExplicitTurnId()
    const seq = validIdentityStreamSeq(payload.stream_seq) ?? validIdentityStreamSeq(identityStreamSeq)
    // Snapshot restoration supplies the original sequence as identity even
    // though it removes it from the payload to bypass live cursor deduplication.
    replayKeys.set(turnId, seq === null
      ? `local:${++localReplaySeq}`
      : JSON.stringify([payload.stream_generation || '', seq]))
    if (!options.isStreaming.value) options.startStreaming()
    pendingRouterDecision.value = null
    options.resetStreamForRouterReplay()
    options.resetStreamIdleTimer()
    scrollToBottomIfFollowing()
  }

  function payloadTurnId(payload: ConversationEventIdentity): string {
    return String(payload.turn_id || payload.task_id || '').trim()
  }

  function latestExplicitTurnId(): string {
    for (let i = options.messages.value.length - 1; i >= 0; i--) {
      const turnId = String(options.messages.value[i]?.turnId || '').trim()
      if (turnId) return turnId
    }
    return ''
  }

  function findRouterMessageForTurn(targetTurnId: string): ChatMessage | undefined {
    for (let i = options.messages.value.length - 1; i >= 0; i--) {
      const message = options.messages.value[i]
      if (
        message.role === 'router'
        && message.provenanceKind === 'router_decision'
        && (!targetTurnId || message.turnId === targetTurnId)
        && belongsToCurrentAttempt(message, targetTurnId)
      ) {
        return message
      }
      if (
        message.role === 'user'
        && (!targetTurnId || !message.turnId || message.turnId !== targetTurnId)
      ) break
    }
    return undefined
  }

  function bindRouterDecisionToModelCall(
    modelCallId: string,
    iteration = 0,
    targetTurnId = latestExplicitTurnId(),
  ) {
    const normalizedCallId = String(modelCallId || '').trim()
    if (!normalizedCallId) return
    targetTurnId = String(targetTurnId || latestExplicitTurnId()).trim()
    // Text/thinking may precede the decision, including just after a replay.
    // Create the same provisional handoff card before binding its call identity.
    if (!findRouterMessageForTurn(targetTurnId)) markEnsembleHandoff(targetTurnId)
    for (let i = options.messages.value.length - 1; i >= 0; i--) {
      const message = options.messages.value[i]
      if (
        message.role === 'router'
        && message.provenanceKind === 'router_decision'
        && (!targetTurnId || message.turnId === targetTurnId)
        && belongsToCurrentAttempt(message, targetTurnId)
      ) {
        if (message.routerModelCallId === normalizedCallId) return
        if (!message.routerModelCallId) {
          message.routerModelCallId = normalizedCallId
          if (iteration > 0) message.routerIteration = iteration
          return
        }
      }
      if (
        message.role === 'user'
        && (!targetTurnId || !message.turnId || message.turnId !== targetTurnId)
      ) break
    }
  }

  function freezeAcceptedRoutingMode(
    decision: NormalizedRouterDecision,
    turnId: string,
  ): NormalizedRouterDecision {
    const acceptedMode = String(
      decision.accepted_routing_mode || '',
    ).trim()
    const expectedTurnId = String(options.activeTurnId.value || '').trim()
    if (
      acceptedMode
      || !options.activeTurnUsesEnsemble.value
      || !expectedTurnId
      || !turnId
      || turnId !== expectedTurnId
    ) return decision
    return { ...decision, accepted_routing_mode: 'ensemble' }
  }

  function freezeActiveTurnRoutingMode(targetTurnId: string): boolean {
    const expectedTurnId = String(options.activeTurnId.value || '').trim()
    if (
      !options.activeTurnUsesEnsemble.value
      || !targetTurnId
      || targetTurnId !== expectedTurnId
    ) return false
    const message = findRouterMessageForTurn(targetTurnId)
    const decision = message?.routerDecision
      ? normalizeRouterDecision(message.routerDecision)
      : null
    if (!message || !decision) return false
    message.routerDecision = freezeAcceptedRoutingMode(decision, targetTurnId)
    return true
  }

  function validIdentityStreamSeq(value: unknown): number | null {
    return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
      ? value
      : null
  }

  function routerDecisionMessageId(
    payload: ConversationRoutingDecision,
    identityStreamSeq?: number,
  ): string {
    const streamSeq = validIdentityStreamSeq(payload.stream_seq)
      ?? validIdentityStreamSeq(identityStreamSeq)
    if (streamSeq !== null) return `router-${options.sessionKey.value}-${streamSeq}`
    localRouterMessageSeq += 1
    return `router-${options.sessionKey.value}-${Date.now()}-${localRouterMessageSeq}`
  }

  function appendRouterDecision(
    payload: ConversationRoutingDecision,
    decision: NormalizedRouterDecision,
    messageId: string,
  ) {
    if (!decision) return
    const turnId = payloadTurnId(payload)
    const acceptedDecision = freezeAcceptedRoutingMode(decision, turnId)
    if (options.messages.value.some(message => message.messageId === messageId)) return

    if (turnId) {
      for (let i = options.messages.value.length - 1; i >= 0; i--) {
        const message = options.messages.value[i]
        const provisionalMessageId = message.messageId || ''
        if (
          message.role === 'router'
          && message.provenanceKind === 'router_decision'
          && provisionalMessageId.startsWith(`router-${options.sessionKey.value}-`)
          && message.turnId === turnId
          && routerMessageAttempts.get(provisionalMessageId)?.provisional
          && belongsToCurrentAttempt(message, turnId)
        ) {
          message.routerDecision = acceptedDecision
          message.messageId = messageId
          message.turnId = turnId
          routerMessageAttempts.delete(provisionalMessageId)
          rememberRouterMessage(message, false)
          scrollToBottomIfFollowing()
          return
        }
      }
    }

    const message: ChatMessage = {
      role: 'router',
      text: '',
      ts: new Date().toISOString(),
      routerDecision: acceptedDecision,
      provenanceKind: 'router_decision',
      messageId,
      ...(turnId ? { turnId } : {}),
    }
    rememberRouterMessage(message, false)
    options.messages.value.push(message)
    scrollToBottomIfFollowing()
  }

  function queueRouterDecision(payload: ConversationRoutingDecision, identityStreamSeq?: number) {
    syncRouterSession()
    if (payload.key && payload.key !== options.sessionKey.value) return
    const normalizedDecision = normalizeRouterDecision(payload)
    if (!normalizedDecision) return
    const decision = freezeAcceptedRoutingMode(
      normalizedDecision,
      payloadTurnId(payload),
    )
    if (options.isStreaming.value && options.streamBubble.value && !options.streamHasVisibleOutput.value) {
      const model = shortModelName(decision.model || decision.routed_model || '')
      options.setStreamActivity(model ? `Router selected · ${model}` : 'Router selected')
    }
    const messageId = routerDecisionMessageId(payload, identityStreamSeq)
    pendingRouterDecision.value = { payload, decision, messageId }
    appendRouterDecision(payload, decision, messageId)
  }

  function flushPendingRouterDecision() {
    syncRouterSession()
    const pending = pendingRouterDecision.value
    if (!pending) return
    pendingRouterDecision.value = null
    appendRouterDecision(pending.payload, pending.decision, pending.messageId)
  }

  function clearPendingRouterDecision() {
    pendingRouterDecision.value = null
  }

  function emptyEnsemble(): ChatEnsembleMeta {
    return {
      profile: 'llm_ensemble',
      modelCount: 0,
      totalCandidates: 0,
      requestCount: 0,
      fallbackUsed: false,
      fallbackReason: '',
      costUsd: 0,
      savedUsd: 0,
      savedPct: 0,
      models: [],
    }
  }

  function memberFromEnsembleProgress(payload: ConversationEnsembleProgress): ChatEnsembleMetaModel | null {
    const model = String(payload.proposer_model || '').trim()
    const isAggregator = payload.event_type === 'aggregator_start' || payload.event_type === 'aggregator_finish'
    if (!model && !isAggregator) return null
    const role = normalizeEnsembleMemberRole(isAggregator ? 'aggregator' : 'proposer')
    const finished = payload.event_type === 'proposer_finish' || payload.event_type === 'aggregator_finish'
    const error = String(payload.error || '').trim()
    const errorCode = String(payload.error_code || '').trim()
    return {
      role,
      label: role,
      provider: String(payload.proposer_provider || '').trim(),
      model,
      modelShort: shortModelName(model),
      input: Number(payload.input_tokens || 0),
      output: Number(payload.output_tokens || 0),
      costUsd: Number(payload.cost_usd || 0),
      sampleIndex: Math.max(0, Number(payload.proposer_index || 0)),
      status: finished
        ? errorCode === 'quorum_cancelled'
          ? 'skipped'
          : error
            ? 'failed'
            : 'done'
        : 'running',
      elapsedMs: Math.max(0, Number(payload.elapsed_ms || 0)),
      error: error || undefined,
      errorCode: errorCode || undefined,
    }
  }

  function upsertEnsembleMember(ensemble: ChatEnsembleMeta, member: ChatEnsembleMetaModel) {
    const identity = (model: ChatEnsembleMetaModel) => (
      `${model.role}:${model.provider}:${model.model}:${model.sampleIndex || 0}`
    )
    const key = identity(member)
    const idx = ensemble.models.findIndex(model => identity(model) === key)
    if (idx >= 0) {
      // Merge so a later 'done' delta keeps the row identity while adding usage.
      ensemble.models.splice(idx, 1, { ...ensemble.models[idx], ...member })
    } else {
      ensemble.models.push(member)
    }
    ensemble.modelCount = ensemble.models.filter(model => model.role !== 'aggregator').length
    ensemble.requestCount = ensemble.models.length
    ensemble.totalCandidates = Math.max(ensemble.totalCandidates, ensemble.modelCount)
  }

  function isEnsembleRouterMessage(message: ChatMessage): boolean {
    const decision = message.routerDecision || null
    const source = String(decision?.source || '').toLowerCase()
    const acceptedMode = String(
      decision?.accepted_routing_mode || '',
    ).toLowerCase()
    return source.includes('ensemble')
      || acceptedMode === 'ensemble'
      || acceptedMode === 'llm_ensemble'
      || Boolean(message.ensemble)
  }

  function findLiveRouterMessage(targetTurnId = latestExplicitTurnId()): ChatMessage | undefined {
    if (!options.isStreaming.value) return undefined
    return findRouterMessageForTurn(targetTurnId)
  }

  function synthesizeHandoffRouterMessage(turnId: string): ChatMessage {
    const message: ChatMessage = {
      role: 'router',
      text: '',
      ts: new Date().toISOString(),
      routerDecision: { tier: 'c1', model: '', source: 'llm_ensemble' },
      provenanceKind: 'router_decision',
      messageId: `router-${options.sessionKey.value}-ensemble-handoff-${++localRouterMessageSeq}`,
      routerState: 'handoff',
      ...(turnId ? { turnId } : {}),
    }
    rememberRouterMessage(message, true)
    options.messages.value.push(message)
    return message
  }

  function markEnsembleHandoff(targetTurnId = latestExplicitTurnId()) {
    if (!options.isStreaming.value) return
    let target = findLiveRouterMessage(targetTurnId)
    if (!target) {
      const expectedTurnId = String(options.activeTurnId.value || '').trim()
      if (
        !options.activeTurnUsesEnsemble.value
        || !expectedTurnId
        || targetTurnId !== expectedTurnId
      ) return
      target = synthesizeHandoffRouterMessage(targetTurnId)
    }
    if (options.activeTurnUsesEnsemble.value && target.routerDecision) {
      const decision = normalizeRouterDecision(target.routerDecision)
      if (decision) {
        target.routerDecision = freezeAcceptedRoutingMode(
          decision,
          String(target.turnId || '').trim(),
        )
      }
    }
    if (!isEnsembleRouterMessage(target)) return
    target.routerState = 'handoff'
    scrollToBottomIfFollowing()
  }

  // Accumulate an ensemble_progress delta onto the live turn's router message so
  // the strip reveals members incrementally. Mirrors appendRouterDecision: find
  // the in-flight router message, else synthesize one.
  function appendEnsembleProgress(payload: ConversationEnsembleProgress) {
    syncRouterSession()
    if (payload.key && payload.key !== options.sessionKey.value) return
    const member = memberFromEnsembleProgress(payload)
    if (!member) return

    // Older accepted progress events omit the turn/task id. Use the same
    // transcript anchor as handoff and call binding, including after replay.
    const turnId = payloadTurnId(payload) || latestExplicitTurnId()
    let target = findLiveRouterMessage(turnId)

    if (!target) {
      const provisionalMessage: ChatMessage = {
        role: 'router',
        text: '',
        ts: new Date().toISOString(),
        routerDecision: { tier: 'c1', model: member.model, source: 'llm_ensemble' },
        provenanceKind: 'router_decision',
        messageId: `router-${options.sessionKey.value}-ensemble-${++localRouterMessageSeq}`,
        ensemble: emptyEnsemble(),
        ...(turnId ? { turnId } : {}),
      }
      rememberRouterMessage(provisionalMessage, true)
      options.messages.value.push(provisionalMessage)
      // Re-read through the reactive array so nested mutations below trigger.
      target = options.messages.value[options.messages.value.length - 1]
    }

    // Keep the original router decision intact. When Squilla Router selected an
    // ensemble-enabled tier, the renderer needs both that decision and these
    // member deltas to play the route stage before the ensemble stage.
    if (!target.ensemble) target.ensemble = emptyEnsemble()
    upsertEnsembleMember(target.ensemble, member)
    scrollToBottomIfFollowing()
  }

  return {
    pendingDecision: pendingRouterDecision,
    handleRouterControlReplay,
    resetRouterReplayCursor,
    queueRouterDecision,
    flushPendingRouterDecision,
    clearPendingRouterDecision,
    appendEnsembleProgress,
    markEnsembleHandoff,
    bindRouterDecisionToModelCall,
    freezeActiveTurnRoutingMode,
  }
}
