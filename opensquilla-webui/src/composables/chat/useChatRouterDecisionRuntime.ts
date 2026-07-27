import { ref, type Ref } from 'vue'
import type { ChatEnsembleMeta, ChatEnsembleMetaModel, ChatMessage } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { EnsembleProgressPayload, RouterDecisionPayload } from '@/types/rpc'
import {
  type NormalizedRouterDecision,
  normalizeRouterDecision,
  shortModelName,
} from '@/composables/chat/useChatRenderedMessages'

const LEGACY_QUORUM_CANCELLED_ERROR =
  /^proposer cancelled after \d+(?:\.\d+)?s ensemble quorum grace$/

export interface UseChatRouterDecisionRuntimeOptions {
  messages: Ref<ChatMessage[]>
  sessionKey: Ref<string>
  isStreaming: Ref<boolean>
  autoScroll: Ref<boolean>
  modelRoutingMode: Ref<ModelRoutingMode>
  streamBubble: Ref<boolean>
  streamHasVisibleOutput: Ref<boolean>
  startStreaming: () => void
  resetStreamForRouterReplay: () => void
  resetStreamIdleTimer: () => void
  setStreamActivity: (label: string) => void
  scrollToBottom: () => void
}

export function useChatRouterDecisionRuntime(options: UseChatRouterDecisionRuntimeOptions) {
  const pendingRouterDecision = ref<{ payload: RouterDecisionPayload; decision: NormalizedRouterDecision } | null>(null)

  // Router and ensemble events can arrive throughout a long streamed answer.
  // They should follow the live edge only while the reader has elected to stay
  // there; otherwise every event would pull an upward-scrolled reader back down.
  function scrollToBottomIfFollowing() {
    if (options.autoScroll.value) options.scrollToBottom()
  }

  function handleRouterControlReplay() {
    if (!options.isStreaming.value) options.startStreaming()
    pendingRouterDecision.value = null
    options.resetStreamForRouterReplay()
    options.resetStreamIdleTimer()
    scrollToBottomIfFollowing()
  }

  function appendRouterDecision(payload: RouterDecisionPayload, decision = normalizeRouterDecision(payload)) {
    if (!decision) return
    const messageId = payload?.stream_seq
      ? `router-${options.sessionKey.value}-${payload.stream_seq}`
      : `router-${options.sessionKey.value}-${Date.now()}`
    const last = options.messages.value[options.messages.value.length - 1]
    if (last?.messageId === messageId) return

    if (options.isStreaming.value) {
      for (let i = options.messages.value.length - 1; i >= 0; i--) {
        const message = options.messages.value[i]
        if (message.role === 'user') break
        if (message.role === 'router' && message.provenanceKind === 'router_decision') {
          message.routerDecision = decision
          message.messageId = messageId
          message.ts = new Date().toISOString()
          message.routerSettled = true
          scrollToBottomIfFollowing()
          return
        }
      }
    }

    options.messages.value.push({
      role: 'router',
      text: '',
      ts: new Date().toISOString(),
      routerDecision: decision,
      provenanceKind: 'router_decision',
      messageId,
    })
    scrollToBottomIfFollowing()
  }

  function queueRouterDecision(payload: RouterDecisionPayload) {
    const decision = normalizeRouterDecision(payload)
    if (!decision) return
    if (options.isStreaming.value && options.streamBubble.value && !options.streamHasVisibleOutput.value) {
      const model = shortModelName(decision.model || decision.routed_model || '')
      options.setStreamActivity(model ? `Router selected · ${model}` : 'Router selected')
    }
    pendingRouterDecision.value = { payload, decision }
    appendRouterDecision(payload, decision)
  }

  function flushPendingRouterDecision() {
    const pending = pendingRouterDecision.value
    if (!pending) return
    pendingRouterDecision.value = null
    appendRouterDecision(pending.payload, pending.decision)
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

  function memberFromEnsembleProgress(payload: EnsembleProgressPayload): ChatEnsembleMetaModel | null {
    const model = String(payload.proposer_model || '').trim()
    const isAggregator = payload.event_type === 'aggregator_start' || payload.event_type === 'aggregator_finish'
    if (!model && !isAggregator) return null
    const label = String(payload.proposer_label || '').trim() || (isAggregator ? 'aggregator' : 'proposer')
    const finished = payload.event_type === 'proposer_finish' || payload.event_type === 'aggregator_finish'
    const error = String(payload.error || '').trim()
    const explicitErrorCode = String(payload.error_code || '').trim()
    const errorCode = explicitErrorCode
      || (LEGACY_QUORUM_CANCELLED_ERROR.test(error) ? 'quorum_cancelled' : '')
    return {
      role: isAggregator ? 'aggregator' : label,
      label,
      provider: String(payload.proposer_provider || '').trim(),
      model,
      modelShort: shortModelName(model),
      input: Number(payload.input_tokens || 0),
      output: Number(payload.output_tokens || 0),
      costUsd: Number(payload.cost_usd || 0),
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
    const key = `${member.role}:${member.provider}:${member.model}`
    const idx = ensemble.models.findIndex(m => `${m.role}:${m.provider}:${m.model}` === key)
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
    const source = String(decision?.source || decision?.routing_source || '').toLowerCase()
    return source.includes('ensemble') || options.modelRoutingMode.value === 'llm_ensemble' || Boolean(message.ensemble)
  }

  function findLiveRouterMessage(): ChatMessage | undefined {
    if (!options.isStreaming.value) return undefined
    for (let i = options.messages.value.length - 1; i >= 0; i--) {
      const message = options.messages.value[i]
      if (message.role === 'user') break
      if (message.role === 'router' && message.provenanceKind === 'router_decision') {
        return message
      }
    }
    return undefined
  }

  function synthesizeHandoffRouterMessage(): ChatMessage {
    const message: ChatMessage = {
      role: 'router',
      text: '',
      ts: new Date().toISOString(),
      routerDecision: { tier: 'c1', model: '', source: 'llm_ensemble' },
      provenanceKind: 'router_decision',
      messageId: `router-${options.sessionKey.value}-ensemble-handoff`,
      routerState: 'handoff',
    }
    options.messages.value.push(message)
    return message
  }

  function markEnsembleHandoff() {
    if (!options.isStreaming.value) return
    let target = findLiveRouterMessage()
    if (!target) {
      if (options.modelRoutingMode.value !== 'llm_ensemble') return
      target = synthesizeHandoffRouterMessage()
    }
    if (!isEnsembleRouterMessage(target)) return
    target.routerState = 'handoff'
    scrollToBottomIfFollowing()
  }

  // Accumulate an ensemble_progress delta onto the live turn's router message so
  // the strip reveals members incrementally. Mirrors appendRouterDecision: find
  // the in-flight router message, else synthesize one.
  function appendEnsembleProgress(payload: EnsembleProgressPayload) {
    const member = memberFromEnsembleProgress(payload)
    if (!member) return

    let target = findLiveRouterMessage()

    if (!target) {
      options.messages.value.push({
        role: 'router',
        text: '',
        ts: new Date().toISOString(),
        routerDecision: { tier: 'c1', model: member.model, source: 'llm_ensemble' },
        provenanceKind: 'router_decision',
        messageId: `router-${options.sessionKey.value}-ensemble`,
        ensemble: emptyEnsemble(),
      })
      // Re-read through the reactive array so nested mutations below trigger.
      target = options.messages.value[options.messages.value.length - 1]
    }

    // Keep the strip on the ensemble branch even if a prior squilla-router
    // decision stamped a non-ensemble source on this same turn's message.
    if (target.routerDecision) target.routerDecision.source = 'llm_ensemble'
    if (!target.ensemble) target.ensemble = emptyEnsemble()
    upsertEnsembleMember(target.ensemble, member)
    scrollToBottomIfFollowing()
  }

  return {
    pendingDecision: pendingRouterDecision,
    handleRouterControlReplay,
    queueRouterDecision,
    flushPendingRouterDecision,
    clearPendingRouterDecision,
    appendEnsembleProgress,
    markEnsembleHandoff,
  }
}
