import { computed, type Ref } from 'vue'
import type {
  ChatEnsembleMeta,
  ChatEnsembleMetaModel,
  ChatEnsembleTrace,
  ChatEnsembleUsageRow,
  ChatCreatedSessionLink,
  ChatMessage,
  ChatMessageMeta,
  ChatRenderedMessage,
  ChatRouterCell,
  ChatRouterTierConfig,
  ChatStreamTimelineItem,
  ChatTimelineSegment,
  ChatToolCall,
  ChatToolCallRenderItem,
  RawToolCallPayload,
} from '@/types/chat'
import {
  isInternalToolName,
  normalizeToolInputText,
  normalizeToolName,
  summarizeToolGroup,
  toolActionLabel,
  toolCallGroups,
  toolDisplayName,
  toolIconName,
  toolOperationKey,
  toolResultIsError,
  toolSecondaryText,
} from '@/utils/chat/toolDisplay'
import {
  normalizeRouterTextTier,
  normalizeRouterTier,
  sortRouterTiers,
} from '@/utils/chat/routerTiers'
import { clarifyRequestFromValue, userInputOutcomeFromValue } from '@/utils/chat/clarify'
import type { RouterVisualMode } from '@/utils/chat/routerVisualMode'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { InterruptViewState } from '@/types/parts'
import { toParts, toolState, type ToPartsInterrupt } from '@/utils/chat/toParts'
import { toSources } from '@/utils/chat/toSources'
import { createdSessionFromToolCall } from '@/utils/chat/createdSessions'
import { relativeTime, type TimeTranslator } from '@/utils/messageTime'
import { normalizeEnsembleMemberRole } from '@/utils/ensembleRoles'
import {
  isLegacySilentSentinelOnly,
  sanitizeAssistantPresentationSegments,
  sanitizeAssistantPresentationText,
} from '@/utils/chat/silentSentinels'
import type { AssistantPresentationProvenance } from '@/utils/chat/silentSentinels'

export interface NormalizedRouterDecision extends Record<string, unknown> {
  tier: string
  model: string
  baseline_model?: string
  source?: string
  routed_tier?: string
  routed_model?: string
  routing_applied?: boolean
  fallback?: boolean
  messageId?: string
  confidence?: number
  rollout_phase?: string
  accepted_routing_mode?: string
  acceptedRoutingMode?: string
}

export interface UseChatRenderedMessagesOptions {
  messages: Readonly<Ref<ChatMessage[]>>
  interruptState?: Ref<ReadonlyMap<string, InterruptViewState>>
  sessionKey: Ref<string>
  routerSlots: Ref<string[]>
  routerModels: Ref<Record<string, string>>
  routerTierConfigs: Ref<Record<string, ChatRouterTierConfig>>
  routerVisualEffectsEnabled: Ref<boolean>
  routerVisualMode: Ref<RouterVisualMode>
  /** Next-turn session state; retained for compatibility and never used to classify a turn. */
  modelRoutingMode?: Ref<ModelRoutingMode>
  isStreaming?: Ref<boolean>
  currentPlanRevisionId?: Readonly<Ref<string>>
  renderMarkdown: (text: string) => string
  stripGeneratedArtifactMarkers: (text: string) => string
  stripTimePrefix: (text: string) => string
  isSubagentCompletionMessage: (role: string, text: string, options?: ChatMessage) => boolean
  timeTranslator?: TimeTranslator
}

type ChatRouterRequestKind = 'text' | 'image'

const ROUTER_LEGACY_GRID_CELLS = 15
const ROUTER_LEGACY_REAL_ANCHORS = [1, 6, 8, 13, 11, 3, 5, 9, 12, 14, 0, 4, 7, 10, 2]
const ROUTER_LEGACY_DECOY_MODELS = [
  'gpt-5.5',
  'claude-opus-4.8',
  'gemini-3.5-flash',
  'qwen3-coder-plus',
  'grok-4.3',
  'gpt-5.4-mini',
  'claude-sonnet-4.6',
  'gemini-3.1-pro',
  'deepseek-v3.2',
  'kimi-k2.6',
  'command-a-plus',
  'grok-build-0.1',
  'glm-4.6',
  'mistral-medium-3.5',
  'claude-haiku-4.5',
]

function clarifyInterruptFromValue(value: unknown): ToPartsInterrupt | null {
  const data = clarifyRequestFromValue(value)
  if (!data) return null
  const composite = `${data.runId}|${data.step}`
  const approvalId = data.requestId
    || (composite === '|' ? 'clarify:history' : composite)
  return {
    kind: 'clarify',
    approvalId,
    data,
  }
}

function historicalClarifyInterrupts(segments: RawToolCallPayload[] | undefined): ToPartsInterrupt[] {
  if (!Array.isArray(segments) || !segments.length) return []
  const inputByToolId = new Map<string, unknown>()
  const out: ToPartsInterrupt[] = []
  const indexByApprovalId = new Map<string, number>()

  function upsert(interrupt: ToPartsInterrupt): void {
    const existingIndex = indexByApprovalId.get(interrupt.approvalId)
    if (existingIndex == null) {
      indexByApprovalId.set(interrupt.approvalId, out.length)
      out.push(interrupt)
      return
    }
    const existing = out[existingIndex]
    out[existingIndex] = {
      ...existing,
      data: { ...existing.data, ...interrupt.data },
      ...(interrupt.resolution ? { resolution: interrupt.resolution } : {}),
    } as ToPartsInterrupt
  }

  for (const segment of segments) {
    const type = String(segment?.type || '')
    const toolId = String(segment?.tool_use_id || segment?.toolId || segment?.id || '')
    if (type === 'tool_use' && toolId) {
      inputByToolId.set(toolId, segment.input)
      const direct = clarifyInterruptFromValue(segment.input)
      if (direct) upsert(direct)
      continue
    }
    if (type !== 'tool_result') continue
    const outcome = userInputOutcomeFromValue(segment.result)
    const direct = clarifyInterruptFromValue(segment.user_input_request)
      || clarifyInterruptFromValue(segment.result)
      || clarifyInterruptFromValue(segment.arguments)
    const fromMatchingInput = direct || clarifyInterruptFromValue(inputByToolId.get(toolId))
    if (fromMatchingInput) {
      upsert({
        ...fromMatchingInput,
        ...(outcome ? { resolution: 'replied' } : {}),
      })
    } else if (outcome) {
      const existingIndex = indexByApprovalId.get(outcome.requestId)
      if (existingIndex != null) {
        out[existingIndex] = { ...out[existingIndex], resolution: 'replied' }
      }
    }
  }
  return out
}

function terminatesPriorAssistant(message: ChatMessage, priorAssistant?: ChatMessage): boolean {
  if (message.role === 'error') return true
  if (message.role !== 'system') return false

  // Terminal errors are persisted inside the same causal turn scope as the
  // partial assistant row. Ordinary injected or scheduled system messages do
  // not share that turn identity. Require the positive causal signal rather
  // than guessing from localized error text or missing provenance.
  return message.restoredFromHistory === true
    && Boolean(message.messageId)
    && !message.provenanceKind
    && priorAssistant?.restoredFromHistory === true
    && Boolean(priorAssistant.messageId)
    && Boolean(message.turnId)
    && message.turnId === priorAssistant.turnId
}

function createdSessionLinksFromCalls(calls: ChatToolCall[]): ChatCreatedSessionLink[] {
  return calls.flatMap((call) => {
    const link = createdSessionFromToolCall(call)
    return link ? [link] : []
  })
}

function completionChildSessionKey(message: ChatMessage): string {
  const provenanceKey = String(message.provenanceSourceSessionKey || '').trim()
  if (provenanceKey) return provenanceKey
  try {
    const payload = JSON.parse(message.text) as Record<string, unknown>
    return typeof payload.child_session_key === 'string'
      ? payload.child_session_key.trim()
      : ''
  } catch {
    return ''
  }
}

function rehomeCompletedSessionCards(
  rawMessages: readonly ChatMessage[],
  renderedMessages: ChatRenderedMessage[],
  isCompletion: UseChatRenderedMessagesOptions['isSubagentCompletionMessage'],
): void {
  const completionIndices = new Map<string, number>()
  rawMessages.forEach((message, index) => {
    if (!isCompletion(message.role, message.text, message)) return
    const childSessionKey = completionChildSessionKey(message)
    if (childSessionKey && !completionIndices.has(childSessionKey)) {
      completionIndices.set(childSessionKey, index)
    }
  })

  for (const source of renderedMessages) {
    const links = source.createdSessionLinks ?? []
    if (!links.length || source.sourceIndex === undefined) continue
    const sourceOwnsCards = (source.toolCalls ?? [])
      .some(call => createdSessionFromToolCall(call) !== null)
    if (!sourceOwnsCards) continue
    const boundaries = links.map(link => completionIndices.get(link.sessionKey))
    if (boundaries.some(index => index === undefined)) continue
    const completionBoundary = Math.max(source.sourceIndex, ...boundaries as number[])
    const nextVisibleUserIndex = rawMessages.findIndex((message, index) => (
      index > source.sourceIndex!
      && message.role === 'user'
      && (message.text.trim().length > 0 || (message.attachments?.length ?? 0) > 0)
    ))
    const target = renderedMessages.find(message => (
      message.displayRole === 'assistant'
      && message.sourceIndex !== undefined
      && message.sourceIndex > completionBoundary
      && (nextVisibleUserIndex < 0 || message.sourceIndex < nextVisibleUserIndex)
      && message.text.trim().length > 0
    ))
    if (!target) continue

    const existing = target.createdSessionLinks ?? []
    const seen = new Set(existing.map(link => link.callId))
    target.createdSessionLinks = [
      ...existing,
      ...links.filter(link => !seen.has(link.callId)),
    ]
    source.createdSessionLinks = []
  }
}

export function useChatRenderedMessages(options: UseChatRenderedMessagesOptions) {
  // A live route card exists before the first provider delta reveals its
  // physical call id. Preserve the event key while teaching that call (and a
  // later history projection) to reuse it, so Vue never remounts the card.
  const stableRouterKeys = new Map<string, string>()

  function stableRouterStripRenderKey(
    turnIdentity: string,
    message: Pick<ChatMessage, 'routerModelCallId' | 'messageId'>
      | Pick<ChatRenderedMessage, 'routerModelCallId' | 'messageId' | 'sourceIndex' | 'id'>,
    messageId?: string,
    index = 0,
  ): string {
    const sessionIdentity = options.sessionKey.value || 'session'
    const identityPrefix = `${sessionIdentity}\u0000${turnIdentity}\u0000`
    const callId = String(message.routerModelCallId || '').trim()
    const eventId = routerStripEventId(message, messageId, index)
    const eventIdentity = `${identityPrefix}event:${eventId}`
    const callIdentity = callId ? `${identityPrefix}call:${callId}` : ''
    const key = (callIdentity ? stableRouterKeys.get(callIdentity) : undefined)
      || stableRouterKeys.get(eventIdentity)
      || routerStripRenderKey(turnIdentity, message, messageId, index)
    stableRouterKeys.set(eventIdentity, key)
    if (callIdentity) stableRouterKeys.set(callIdentity, key)
    return key
  }

  const renderedMessages = computed((): ChatRenderedMessage[] => {
    const result: ChatRenderedMessage[] = []
    let prevDay = ''
    let prevRole = ''
    let turnRouterIndexes = new Map<string, number>()
    let turnRouterOrder: number[] = []
    let turnIdx = 0
    let turnIdentity = 'turn-0'
    let explicitTurnId = ''
    let turnResultStartIndex = 0
    let currentTurnHasUserAnchor = false
    let lastAssistantResultIndex = -1
    let turnRequestKind: ChatRouterRequestKind = 'text'
    let turnRouterDecision: NormalizedRouterDecision | null = null

    // Index of the last user turn — anything after it belongs to the in-flight
    // turn, whose live ensemble strip must survive its own mid-turn done event.
    let lastUserIdx = -1
    for (let k = options.messages.value.length - 1; k >= 0; k--) {
      if (options.messages.value[k].role === 'user') {
        lastUserIdx = k
        break
      }
    }

    for (let i = 0; i < options.messages.value.length; i++) {
      const msg = options.messages.value[i]
      const day = dayKey(msg.ts)

      if (day && day !== prevDay) {
        prevDay = day
        prevRole = ''
      }

      const messageTurnId = String(msg.turnId || '').trim()
      const explicitTurnChanged = Boolean(
        messageTurnId && messageTurnId !== explicitTurnId,
      )
      const legacyUserStartsTurn = msg.role === 'user' && !messageTurnId
      const adoptsLegacyTurnId = Boolean(
        messageTurnId
        && !explicitTurnId
        && currentTurnHasUserAnchor,
      )
      if (adoptsLegacyTurnId) {
        turnIdentity = messageTurnId
        const adoptedTurnKey = `turn:${messageTurnId}`
        for (let index = turnResultStartIndex; index < result.length; index++) {
          result[index]!.turnKey = adoptedTurnKey
          if (result[index]!.isRouterStrip) {
            result[index]!.routerTurnKey = stableRouterStripRenderKey(
              messageTurnId,
              result[index]!,
            )
          }
        }
      } else if (explicitTurnChanged || legacyUserStartsTurn) {
        turnRouterIndexes = new Map()
        turnRouterOrder = []
        turnRouterDecision = null
        lastAssistantResultIndex = -1
        turnRequestKind = msg.role === 'user'
          ? routerRequestKindFromAttachments(msg.attachments)
          : 'text'
        turnIdx++
        turnIdentity = messageTurnId
          || msg.clientId
          || msg.messageId
          || String(msg.ts || `turn-${turnIdx}`)
        turnResultStartIndex = result.length
        currentTurnHasUserAnchor = msg.role === 'user'
      }
      if (messageTurnId) {
        explicitTurnId = messageTurnId
      } else if (legacyUserStartsTurn) {
        explicitTurnId = ''
      }
      if (msg.role === 'user') currentTurnHasUserAnchor = true

      // Internal control turns can intentionally carry an empty displayText
      // while retaining their provider-facing text in the transcript. They
      // still establish a new turn identity for the following router and
      // assistant rows, but must not leave an empty user bubble in the UI.
      if (
        msg.role === 'user'
        && !msg.text.trim()
        && !msg.attachments?.length
        && !msg.promptAnnotations?.length
      ) {
        prevRole = ''
        continue
      }

      if (lastAssistantResultIndex >= 0) {
        const priorAssistant = result[lastAssistantResultIndex]
        const priorAssistantMessage = priorAssistant?.sourceIndex === undefined
          ? undefined
          : options.messages.value[priorAssistant.sourceIndex]
        if (
          priorAssistant?.displayRole === 'assistant'
          && terminatesPriorAssistant(msg, priorAssistantMessage)
        ) {
          priorAssistant.terminalFailure = true
        }
      }

      // Subagent completion is a durable control-plane row used to wake the
      // parent model. It belongs in the parent transcript, but it is not a
      // parent-chat message and must never project its JSON (or any metadata
      // attached by compatibility gateways) into the visible conversation.
      if (options.isSubagentCompletionMessage(msg.role, msg.text, msg)) {
        prevRole = ''
        continue
      }

      const routerDecision = normalizeRouterDecision(msg.routerDecision || (msg.provenanceKind === 'router_decision' ? msg : null))
      if (routerDecision) {
        turnRouterDecision = routerDecision
        const stripItem = renderedRouterStrip(
          msg,
          routerDecision,
          turnIdx,
          i,
          undefined,
          turnRequestKind,
          turnIdentity,
        )
        if (stripItem) {
          upsertRouterStrip(result, stripItem, turnRouterIndexes, turnRouterOrder)
        }
        prevRole = ''
        continue
      }

      const usageEnsemble = ensembleMetaFromMessage(msg)
      if (usageEnsemble) {
        const usageRouterDecision = routerDecisionFromUsage(msg)
        if (usageRouterDecision) turnRouterDecision = usageRouterDecision
        const inLiveTurn = options.isStreaming?.value === true && i > lastUserIdx
        const settledMessage = {
          ...msg,
          routerSettled: msg.routerSettled === true || !inLiveTurn,
        }
        const callIdentity = routerModelCallIdFromMessage(msg)
        const priorIndex = routerSettlementCandidateIndex(
          result,
          turnRouterIndexes,
          turnRouterOrder,
          callIdentity,
        )
        const priorStrip = priorIndex === undefined ? undefined : result[priorIndex]
        const stripItem = turnRouterDecision
          && shouldCombineRouterAndEnsemble(turnRouterDecision, true, msg.restoredFromHistory === true)
          ? renderedCombinedRouterStrip(
              settledMessage,
              turnRouterDecision,
              usageEnsemble,
              turnIdx,
              i,
              priorStrip?.messageId || `${msg.messageId || i}-router`,
              turnRequestKind,
              turnIdentity,
            )
          : renderedEnsembleRouterStrip(
              settledMessage,
              usageEnsemble,
              turnIdx,
              i,
              `${msg.messageId || i}-ensemble-router`,
              turnIdentity,
            )
        if (stripItem) {
          upsertRouterStrip(result, stripItem, turnRouterIndexes, turnRouterOrder, {
            settlement: true,
            settleReplacement: false,
          })
        }
        prevRole = ''
      } else {
        const usageRouterDecision = routerDecisionFromUsage(msg, inheritedSubagentRoute(msg))
        if (usageRouterDecision) {
          turnRouterDecision = usageRouterDecision
          const stripItem = renderedRouterStrip(
            msg,
            usageRouterDecision,
            turnIdx,
            i,
            `${msg.messageId || i}-router`,
            turnRequestKind,
            turnIdentity,
          )
          if (stripItem) {
            upsertRouterStrip(result, stripItem, turnRouterIndexes, turnRouterOrder, {
              settlement: true,
            })
          }
          prevRole = ''
        }
      }

      const displayRole = msg.role
      const roleLabel = displayRole === 'user' ? 'You' : displayRole === 'assistant' ? 'Assistant' : displayRole.charAt(0).toUpperCase() + displayRole.slice(1)
      const collapsible = displayRole === 'user' || displayRole === 'assistant'
      const sameGroup = collapsible && displayRole === prevRole && day === prevDay && day !== ''
      if (collapsible) prevRole = displayRole
      else if (displayRole === 'maintenance') prevRole = ''

      const ownerKey = msg.messageId || msg.clientId || `${msg.role}-${i}`
      const planRevisions = (msg.planRevisions ?? []).map(plan => ({
        ...plan,
        current: Boolean(options.currentPlanRevisionId?.value)
          && plan.revisionId === options.currentPlanRevisionId?.value,
      }))
      const isPlanMessage = msg.role === 'assistant' && planRevisions.length > 0
      const normalizedToolCalls = normalizeToolCalls(msg.tool_calls)
      const assistantRawText = msg.role === 'assistant'
        ? options.stripGeneratedArtifactMarkers(msg.text)
        : msg.text
      const assistantProvenance: AssistantPresentationProvenance = {
        inputMode: msg.turnInputMode,
        runKind: msg.turnRunKind,
      }
      const assistantDisplayText = msg.role === 'assistant'
        ? sanitizeAssistantPresentationText(assistantRawText, assistantProvenance)
        : assistantRawText
      const legacySilentOnly = msg.role === 'assistant'
        && isLegacySilentSentinelOnly(assistantRawText)
      const rendered: ChatRenderedMessage = {
        id: `${msg.role}-${i}`,
        ...(msg.clientId ? { clientId: msg.clientId } : {}),
        sourceIndex: i,
        role: msg.role,
        displayRole,
        roleLabel,
        text: isPlanMessage
          ? ''
          : assistantDisplayText,
        timeStr: relativeTime(msg.ts, Date.now(), options.timeTranslator),
        ts: msg.ts ?? null,
        showHeader: !sameGroup,
        messageId: msg.messageId,
        restoredFromHistory: msg.restoredFromHistory,
        turnKey: `turn:${turnIdentity === 'turn-0' ? ownerKey : turnIdentity}`,
        turnId: messageTurnId || undefined,
        turnInputMode: msg.turnInputMode,
        turnRunKind: msg.turnRunKind,
        inputDisposition: msg.inputDisposition,
        inputDispositionRevision: msg.inputDispositionRevision,
        maintenance: msg.maintenance,
        turnOutcome: msg.turnOutcome,
        hasAttachments: !!msg.attachments?.length,
        attachments: msg.attachments,
        promptAnnotations: msg.promptAnnotations,
        createdSessionLinks: createdSessionLinksFromCalls(normalizedToolCalls),
        // submit_plan is a transport/control detail. Once a typed immutable
        // plan part exists, the plan card is the authoritative visible item;
        // do not also render the same payload as an expandable tool timeline.
        toolCalls: isPlanMessage ? [] : normalizedToolCalls,
        timelineItems: isPlanMessage ? [] : normalizeMessageTimeline(msg, ownerKey),
        planRevisions,
        artifacts: msg.artifacts,
        meta: messageMeta(msg),
        reasoning: msg.role === 'assistant' ? msg.reasoning : undefined,
        reasoningBlocks: msg.role === 'assistant'
          ? msg.reasoningBlocks?.map(block => ({ ...block }))
          : undefined,
        reasoningPresentationPending: msg.role === 'assistant'
          ? msg.reasoningPresentationPending
          : undefined,
        interrupted: msg.interrupted,
        provenanceKind: msg.provenanceKind,
        provenanceSourceSessionKey: msg.provenanceSourceSessionKey,
        provenanceSourceTool: msg.provenanceSourceTool,
        stopNotice: msg.stopNotice,
        errorCode: msg.errorCode,
      }
      // Additive: derive discriminated parts from the finished rendered
      // object so they cannot drift from the fields the components read. Only
      // assistant turns fold a parts body; other roles render through the
      // ChatMessageList role branch and stay parts:[].
      rendered.parts = rendered.displayRole === 'assistant'
        ? toParts(
            rendered,
            options.renderMarkdown,
            toolCallGroups,
            ownerKey,
            historicalClarifyInterrupts(msg.tool_calls),
            options.interruptState?.value,
          )
        : []
      rendered.sources = rendered.displayRole === 'assistant' ? toSources(rendered) : []
      // statusHistory is a stored snapshot (not re-derivable from tool_calls), so
      // read it straight off the message for assistant turns. A reloaded thread
      // has no snapshot → []; non-assistant roles stay [] like parts/sources.
      rendered.statusHistory = rendered.displayRole === 'assistant'
        ? (msg.statusHistory ?? [])
        : []
      // Match the live suppressed-turn behavior for legacy exact-sentinel rows:
      // no empty assistant bubble, while a turn that contains tools, artifacts,
      // reasoning, interrupts, or activity history remains inspectable.
      if (
        legacySilentOnly
        && rendered.parts.length === 0
        && rendered.statusHistory.length === 0
        && !rendered.artifacts?.length
        && !rendered.stopNotice
      ) {
        prevRole = ''
        continue
      }
      if (import.meta.env.DEV && rendered.displayRole === 'assistant') {
        assertPartsParity(rendered, ownerKey)
      }
      result.push(rendered)
      if (rendered.displayRole === 'assistant') {
        lastAssistantResultIndex = result.length - 1
      }
    }

    rehomeCompletedSessionCards(
      options.messages.value,
      result,
      options.isSubagentCompletionMessage,
    )
    return result
  })

  function renderedRouterStrip(
    msg: ChatMessage,
    decision: NormalizedRouterDecision,
    turnIdx: number,
    index: number,
    messageId = msg.messageId,
    requestKind: ChatRouterRequestKind = 'text',
    turnIdentity = `turn-${turnIdx}`,
  ): ChatRenderedMessage | null {
    if (!options.routerVisualEffectsEnabled.value) return null
    const restoredFromHistory = msg.restoredFromHistory === true
    if (isDirectEnsembleRouterDecision(decision)) {
      return renderedEnsembleRouterStrip(
        msg,
        msg.ensemble,
        turnIdx,
        index,
        messageId,
        turnIdentity,
      )
    }
    if (shouldCombineRouterAndEnsemble(decision, Boolean(msg.ensemble), restoredFromHistory)) {
      return renderedCombinedRouterStrip(
        msg,
        decision,
        msg.ensemble,
        turnIdx,
        index,
        messageId,
        requestKind,
        turnIdentity,
      )
    }
    return renderedTierRouterStrip(
      msg,
      decision,
      turnIdx,
      index,
      messageId,
      requestKind,
      turnIdentity,
    )
  }

  function renderedTierRouterStrip(
    msg: ChatMessage,
    decision: NormalizedRouterDecision,
    turnIdx: number,
    index: number,
    messageId = msg.messageId,
    requestKind: ChatRouterRequestKind = 'text',
    turnIdentity = `turn-${turnIdx}`,
  ): ChatRenderedMessage | null {
    const cells = routerDecisionCellsForRequest(decision, requestKind)
    const fixedSessionRoute = decision.source === 'session_model'
    if (cells.length === 0 || (cells.length === 1 && !fixedSessionRoute)) return null
    return {
      id: `router-turn-${turnIdx}`,
      role: 'router',
      displayRole: 'router',
      roleLabel: 'Router',
      text: '',
      timeStr: relativeTime(msg.ts, Date.now(), options.timeTranslator),
      ts: msg.ts ?? null,
      showHeader: false,
      sourceIndex: index,
      isRouterStrip: true,
      routerTurnKey: stableRouterStripRenderKey(
        turnIdentity,
        { routerModelCallId: routerModelCallIdFromMessage(msg), messageId },
        messageId,
        index,
      ),
      turnKey: `turn:${turnIdentity}`,
      routerModelCallId: routerModelCallIdFromMessage(msg) || undefined,
      routerIteration: routerIterationFromMessage(msg) || undefined,
      routerState: routerDecisionState(decision),
      routerSource: decision.source || 'none',
      routerObserve: decision.routing_applied === false,
      routerStatic: msg.restoredFromHistory === true,
      routerSettled: msg.routerSettled === true,
      routerPanel: routerPanelDataset(options.routerVisualMode.value),
      routerMode: 'squilla_router',
      gridCells: cells,
      winnerIdx: routerWinnerCellIndex(cells, decision.tier),
      messageId: messageId || `${index}-router`,
    }
  }

  function inheritedSubagentRoute(msg: ChatMessage): NormalizedRouterDecision | null {
    const currentSessionKey = options.sessionKey.value.trim().toLowerCase()
    if (!(
      currentSessionKey.startsWith('subagent:')
      || /^agent:[^:]+:subagent:[^:]+$/.test(currentSessionKey)
    )) return null
    const usage = msg.usage || msg.turn_usage
    const routingSource = String(usage?.routing_source || '').trim().toLowerCase()
    if (!usage || !['', 'none', 'session_model'].includes(routingSource)) {
      return null
    }
    const model = String(usage.routed_model || usage.model || msg.model || '').trim()
    if (!model) return null

    const configuredTier = options.routerSlots.value.find((slot) => {
      const configured = String(
        options.routerModels.value[slot]
        || options.routerTierConfigs.value[slot]?.model
        || '',
      ).trim()
      return configured === model
    })
    // Explicit sessions_spawn models and older session histories need not match
    // the current router configuration. A synthetic fixed tier preserves the
    // durable model identity without pretending the router selected it now.
    const tier = configuredTier || 'session_model'
    return normalizeRouterDecision({
      tier,
      model,
      source: 'session_model',
      routing_applied: true,
      rollout_phase: usage.rollout_phase || 'full',
    })
  }

  function renderedCombinedRouterStrip(
    msg: ChatMessage,
    decision: NormalizedRouterDecision,
    ensemble: ChatEnsembleMeta | undefined,
    turnIdx: number,
    index: number,
    messageId = msg.messageId,
    requestKind: ChatRouterRequestKind = 'text',
    turnIdentity = `turn-${turnIdx}`,
  ): ChatRenderedMessage | null {
    const routerStrip = renderedTierRouterStrip(
      msg,
      decision,
      turnIdx,
      index,
      messageId,
      requestKind,
      turnIdentity,
    )
    if (!routerStrip) {
      return renderedEnsembleRouterStrip(
        msg,
        ensemble,
        turnIdx,
        index,
        messageId,
        turnIdentity,
      )
    }
    return {
      ...routerStrip,
      routerPanel: 'router-ensemble-sequence',
      routerState: msg.routerState || routerStrip.routerState,
      ensemble,
    }
  }

  function renderedEnsembleRouterStrip(
    msg: ChatMessage,
    ensemble: ChatEnsembleMeta | undefined,
    turnIdx: number,
    index: number,
    messageId = msg.messageId,
    turnIdentity = `turn-${turnIdx}`,
  ): ChatRenderedMessage | null {
    if (!options.routerVisualEffectsEnabled.value) return null
    return {
      id: `router-turn-${turnIdx}`,
      role: 'router',
      displayRole: 'router',
      roleLabel: 'Router',
      text: '',
      timeStr: relativeTime(msg.ts, Date.now(), options.timeTranslator),
      ts: msg.ts ?? null,
      showHeader: false,
      sourceIndex: index,
      isRouterStrip: true,
      routerTurnKey: stableRouterStripRenderKey(
        turnIdentity,
        { routerModelCallId: routerModelCallIdFromMessage(msg), messageId },
        messageId,
        index,
      ),
      turnKey: `turn:${turnIdentity}`,
      routerModelCallId: routerModelCallIdFromMessage(msg) || undefined,
      routerIteration: routerIterationFromMessage(msg) || undefined,
      routerState: msg.routerSettled === true ? 'settled' : 'pending',
      routerSource: 'llm_ensemble',
      routerObserve: false,
      routerStatic: msg.restoredFromHistory === true,
      // Live strips stay unsettled (animating) while members stream in; a member
      // list alone no longer forces 'settled'. History strips are frozen instead.
      routerSettled: msg.routerSettled === true || msg.restoredFromHistory === true,
      routerPanel: 'llm-ensemble',
      routerMode: 'llm_ensemble',
      ensemble,
      gridCells: [],
      winnerIdx: -1,
      messageId: messageId || `${index}-ensemble-router`,
    }
  }

  function messageMeta(msg: ChatMessage): ChatMessageMeta | undefined {
    if (!msg.usage && !msg.turn_usage) return undefined
    const u = msg.usage || msg.turn_usage || {}
    const model = String(msg.model || u.model || u.routed_model || '')
    const input = Number(msg.input ?? msg.input_tokens ?? u.input_tokens ?? u.inputTokens ?? 0)
    const output = Number(msg.output ?? msg.output_tokens ?? u.output_tokens ?? u.outputTokens ?? 0)
    const cached = numeric(u.cached_tokens ?? u.cachedTokens)
    const reasoning = numeric(u.reasoning_tokens ?? u.reasoningTokens)
    const cost = numeric(u.cost_usd ?? u.costUsd)
    const hasTier = !!(u.routed_tier && u.routing_source && u.routing_source !== 'none')
    const turnSavedPct = typeof u.total_savings_pct === 'number' && u.total_savings_pct > 0 ? u.total_savings_pct : 0
    const hasSaved = hasTier && turnSavedPct > 0 && !u.__savings_ui_suppressed
    const ensemble = ensembleMeta(u)
    const rawCoverageStatus = u.coverage_status ?? u.coverageStatus
    const coverageStatus = typeof rawCoverageStatus === 'string'
      ? rawCoverageStatus.trim()
      : ''
    const unknownUsageEvents = Math.max(
      0,
      Math.floor(numeric(u.unknown_usage_events ?? u.unknownUsageEvents)),
    )
    const usageUnknown = (u.usage_unknown ?? u.usageUnknown) === true
      || unknownUsageEvents > 0
      || Boolean(coverageStatus && coverageStatus.toLowerCase() !== 'complete')
    const hasKnownUsage = [
      input,
      output,
      cached,
      reasoning,
      cost,
      numeric(u.total_tokens ?? u.totalTokens),
      numeric(u.cache_write_tokens ?? u.cacheWriteTokens ?? u.cache_write),
      numeric(u.billed_cost ?? u.billedCost),
      numeric(u.estimated_cost_component_usd ?? u.estimatedCostComponentUsd),
    ].some(value => value > 0) || Boolean(ensemble && (
      ensemble.costUsd > 0
      || ensemble.models.some(member =>
        member.input > 0 || member.output > 0 || member.costUsd > 0,
      )
    ))
    return {
      model,
      modelShort: model.includes('/') ? (model.split('/').pop() || model) : model,
      input,
      output,
      hasTokens: input > 0 || output > 0,
      cachedTokens: cached,
      reasoningTokens: reasoning,
      costUsd: cost,
      hasSaved,
      turnSavedPct,
      savedLabel: turnSavedPct > 0 ? `Saved ~${Math.round(turnSavedPct)}%` : 'Cost optimized',
      ensemble,
      coverageStatus: coverageStatus || undefined,
      usageUnknown,
      unknownUsageEvents,
      hasKnownUsage,
      decisionId: typeof u.decision_id === 'string' && u.decision_id ? u.decision_id : undefined,
    }
  }

  function ensembleMeta(usage: Record<string, unknown>): ChatEnsembleMeta | undefined {
    const breakdown = normalizeEnsembleUsageRows(
      usage.model_usage_breakdown || usage.modelUsageBreakdown,
    )
    const trace = normalizeEnsembleTrace(usage.ensemble_trace || usage.ensembleTrace)
    const hasTrace = Boolean(trace?.profile || trace?.mode)
    if (!breakdown.length && !hasTrace) return undefined

    const traceCandidates = normalizeEnsembleUsageRows(trace?.candidates)
    const usedBreakdownIndexes = new Set<number>()
    const physicalModels = traceCandidates
      .map(candidate => {
        const candidateKey = ensembleCandidateIdentity(candidate)
        const breakdownIndex = breakdown.findIndex((row, index) =>
          !usedBreakdownIndexes.has(index)
          && ensembleCandidateIdentity(row) === candidateKey,
        )
        if (breakdownIndex >= 0) usedBreakdownIndexes.add(breakdownIndex)
        const usageRow = breakdownIndex >= 0
          ? breakdown[breakdownIndex]
          : {
              ...candidate,
              role: String(candidate.role || candidate.label || 'proposer'),
            }
        return rowToEnsembleModel(usageRow, candidate)
      })
      .concat(
        breakdown
          .filter((_, index) => !usedBreakdownIndexes.has(index))
          .map(row => rowToEnsembleModel(row)),
      )
      .filter((row): row is ChatEnsembleMetaModel => row !== null)
    const models = foldAggregatorRequests(physicalModels)
    const uniqueModels = new Set(models.map(row => `${row.role}:${row.provider}:${row.model}:${row.sampleIndex || 0}`))
    const rowCost = models.reduce((sum, row) => sum + row.costUsd, 0)
    // The ledger total is authoritative whenever it is present, including a
    // legitimate 0. Probe for the field rather than for a positive number, or
    // a settled $0 turn falls back to stale non-zero breakdown subtotals that
    // a richer historical projection may still carry.
    const rawCost = usage.cost_usd ?? usage.costUsd
    const hasExplicitCost = rawCost !== undefined && rawCost !== null && Number.isFinite(Number(rawCost))
    const explicitCost = numeric(rawCost)
    const savedUsd = Math.max(0, numeric(usage.total_savings_usd ?? usage.totalSavingsUsd ?? usage.savings_usd ?? usage.savingsUsd))
    const savedPct = Math.max(0, numeric(usage.total_savings_pct ?? usage.totalSavingsPct ?? usage.savings_pct ?? usage.savingsPct))

    return {
      profile: String(trace?.profile || breakdown[0]?.profile || 'llm_ensemble'),
      modelCount: uniqueModels.size || models.length || numeric(trace?.selected_candidate_count) || numeric(trace?.total_candidates),
      totalCandidates: numeric(trace?.total_candidates),
      // A settled trace may include display-only rows for members whose
      // provider request never started. Keep the trace's physical request
      // count authoritative, with actual usage rows as the legacy lower bound.
      requestCount: Math.max(0, numeric(trace?.llm_request_count), breakdown.length),
      fallbackUsed: trace?.fallback_used === true || trace?.fallbackUsed === true,
      fallbackReason: String(trace?.fallback_reason || trace?.fallbackReason || ''),
      costUsd: hasExplicitCost ? explicitCost : rowCost,
      savedUsd,
      savedPct,
      models,
    }
  }

  function ensembleMetaFromMessage(msg: ChatMessage): ChatEnsembleMeta | undefined {
    const usage = msg.routerUsage || msg.usage || msg.turn_usage
    return usage ? ensembleMeta(usage) : undefined
  }

  function isDirectEnsembleRouterDecision(
    decision: NormalizedRouterDecision,
  ): boolean {
    const source = String(decision.source || decision.routing_source || '').toLowerCase()
    if (source.includes('ensemble')) return true
    const acceptedMode = String(
      decision.accepted_routing_mode || decision.acceptedRoutingMode || '',
    ).toLowerCase()
    return acceptedMode === 'ensemble' || acceptedMode === 'llm_ensemble'
  }

  function shouldCombineRouterAndEnsemble(
    decision: NormalizedRouterDecision,
    hasEnsembleEvidence: boolean,
    restoredFromHistory: boolean,
  ): boolean {
    if (isDirectEnsembleRouterDecision(decision)) return false
    if (hasEnsembleEvidence) return true
    // Current tier configuration is only authoritative for a live decision.
    // Restored history combines the two stages only when its own usage proves
    // that ensemble execution actually happened.
    return !restoredFromHistory && routerTierConfig(decision.tier).ensembleEnabled === true
  }

  function normalizeEnsembleUsageRows(value: unknown): ChatEnsembleUsageRow[] {
    if (!Array.isArray(value)) return []
    return value.filter((row): row is ChatEnsembleUsageRow => !!row && typeof row === 'object' && !Array.isArray(row))
  }

  function normalizeEnsembleTrace(value: unknown): ChatEnsembleTrace | null {
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as ChatEnsembleTrace
      : null
  }

  function ensembleCandidateIdentity(row: ChatEnsembleUsageRow): string {
    return [
      String(row.label || '').trim(),
      String(row.provider || '').trim(),
      String(row.model || '').trim(),
      String(Math.max(0, numeric(row.sample_index))),
    ].join('\u0000')
  }

  function rowToEnsembleModel(
    row: ChatEnsembleUsageRow,
    candidate?: ChatEnsembleUsageRow,
  ): ChatEnsembleMetaModel | null {
    const model = String(row.model || '').trim()
    if (!model) return null
    const provider = String(row.provider || '').trim()
    const role = normalizeEnsembleMemberRole(row.role)
    const label = role
    const error = String(candidate?.error || '').trim()
    const errorCode = String(candidate?.error_code || candidate?.errorCode || '').trim()
    const status = errorCode === 'quorum_cancelled'
      ? 'skipped'
      : error || candidate?.ok === false
        ? 'failed'
        : candidate?.ok === true
          ? 'done'
          : undefined
    return {
      role,
      label,
      provider,
      model,
      modelShort: shortModelName(model),
      input: numeric(row.input_tokens ?? row.inputTokens),
      output: numeric(row.output_tokens ?? row.outputTokens),
      costUsd: numeric(row.cost_usd ?? row.costUsd ?? row.billed_cost ?? row.billedCost),
      elapsedMs: Math.max(0, numeric(row.elapsed_ms ?? row.elapsedMs)),
      sampleIndex: Math.max(0, numeric(row.sample_index)),
      status,
      error: error || undefined,
      errorCode: errorCode || undefined,
    }
  }

  function foldAggregatorRequests(
    models: ChatEnsembleMetaModel[],
  ): ChatEnsembleMetaModel[] {
    // A tool continuation is another physical request made by the configured
    // aggregator, not another logical member. rowToEnsembleModel already maps
    // legacy execution names onto the public Aggregator role.
    const logicalModels: ChatEnsembleMetaModel[] = []
    const aggregatorIndexByIdentity = new Map<string, number>()

    for (const model of models) {
      if (model.role !== 'aggregator') {
        logicalModels.push(model)
        continue
      }

      const identity = [
        model.provider,
        model.model,
        String(model.sampleIndex || 0),
      ].join('\u0000')
      const existingIndex = aggregatorIndexByIdentity.get(identity)
      if (existingIndex == null) {
        aggregatorIndexByIdentity.set(identity, logicalModels.length)
        logicalModels.push(model)
        continue
      }

      const existing = logicalModels[existingIndex]
      logicalModels.splice(existingIndex, 1, {
        ...existing,
        input: existing.input + model.input,
        output: existing.output + model.output,
        costUsd: existing.costUsd + model.costUsd,
        elapsedMs: Math.max(0, Number(existing.elapsedMs || 0))
          + Math.max(0, Number(model.elapsedMs || 0)),
        status: model.status ?? existing.status,
        error: model.error ?? existing.error,
        errorCode: model.errorCode ?? existing.errorCode,
      })
    }

    return logicalModels
  }

  function numeric(value: unknown): number {
    const n = Number(value)
    return Number.isFinite(n) ? n : 0
  }

  function routerDecisionCells(decision: NormalizedRouterDecision): ChatRouterCell[] {
    return routerDecisionCellsForRequest(decision, 'text')
  }

  function routerDecisionCellsForRequest(decision: NormalizedRouterDecision, requestKind: ChatRouterRequestKind): ChatRouterCell[] {
    const realCells = realRouterDecisionCellsForRequest(decision, requestKind)
    if (realCells.length <= 1 || options.routerVisualMode.value !== 'legacy_grid') return realCells
    return legacyRouterGridCells(realCells)
  }

  function realRouterDecisionCellsForRequest(decision: NormalizedRouterDecision, requestKind: ChatRouterRequestKind): ChatRouterCell[] {
    const winnerTier = normalizeRouterTier(decision.tier)
    const configuredTiers = options.routerSlots.value.length
      ? options.routerSlots.value.map(normalizeRouterTier).filter(Boolean)
      : Object.keys(options.routerTierConfigs.value).map(normalizeRouterTier).filter(Boolean)
    if (winnerTier && !configuredTiers.includes(winnerTier)) configuredTiers.push(winnerTier)
    const sourceTiers = sortRouterTiers(configuredTiers.length ? configuredTiers : (winnerTier ? [winnerTier] : []))
    const realByModel = new Map<string, ChatRouterCell>()

    for (const tier of sourceTiers) {
      const tierConfig = routerTierConfig(tier)
      if (tier !== winnerTier && !routerTierMatchesRequestKind(tierConfig, requestKind)) continue
      const model = tierConfig.model || options.routerModels.value[tier] || (tier === winnerTier ? String(decision.model || '') : '')
      if (!model && tier !== winnerTier) continue
      const displayName = shortModelName(routerFxStripProvider(model)) || (tier === winnerTier ? 'selected model' : tier)
      const executionKind = tierConfig.ensembleEnabled === true ? 'ensemble' : 'single_model'
      const key = `${executionKind}:${displayName || model || `winner:${tier}`}`
      const existing = realByModel.get(key)
      if (existing) {
        existing.tiers = [...(existing.tiers || []), tier]
        continue
      }
      realByModel.set(key, {
        kind: 'real',
        tier,
        tiers: [tier],
        displayName,
        model,
        executionKind,
      })
    }

    return Array.from(realByModel.values())
      .sort((a, b) => (a.displayName || a.tier).localeCompare(b.displayName || b.tier))
  }

  function legacyRouterGridCells(realCells: ChatRouterCell[]): ChatRouterCell[] {
    const cells = Array.from({ length: ROUTER_LEGACY_GRID_CELLS }, (_, index): ChatRouterCell => ({
      kind: 'decoy',
      tier: '',
      tiers: [],
      displayName: ROUTER_LEGACY_DECOY_MODELS[index % ROUTER_LEGACY_DECOY_MODELS.length],
      model: '',
    }))
    realCells.slice(0, ROUTER_LEGACY_GRID_CELLS).forEach((cell, index) => {
      cells[ROUTER_LEGACY_REAL_ANCHORS[index] ?? index] = cell
    })
    return cells
  }

  function routerTierConfig(tier: string): ChatRouterTierConfig {
    const normalized = normalizeRouterTier(tier)
    return options.routerTierConfigs.value[normalized] || {
      model: options.routerModels.value[normalized] || '',
      supportsImage: false,
      imageOnly: false,
    }
  }

  function routerTierMatchesRequestKind(tierConfig: ChatRouterTierConfig, requestKind: ChatRouterRequestKind): boolean {
    if (requestKind === 'image') return tierConfig.supportsImage || tierConfig.imageOnly
    return !tierConfig.imageOnly
  }

  function normalizeMessageTimeline(msg: ChatMessage, ownerKey: string): ChatStreamTimelineItem[] {
    if (msg.role !== 'assistant') return []
    const explicitTimeline = Array.isArray(msg.timeline) ? msg.timeline : []
    if (explicitTimeline.length) {
      const calls = normalizeToolCalls(msg.tool_calls)
      return sanitizeAssistantTimelineItems(
        timelineFromSegments(explicitTimeline, calls, ownerKey, msg.interrupts),
        { inputMode: msg.turnInputMode, runKind: msg.turnRunKind },
      )
    }
    const rawSegments = Array.isArray(msg.tool_calls) ? msg.tool_calls : []
    const hasPersistedTimeline = rawSegments.some(seg => ['text', 'tool_use', 'tool_result'].includes(String(seg?.type || '')))
    if (!hasPersistedTimeline) return []
    return sanitizeAssistantTimelineItems(
      timelineFromPersistedSegments(rawSegments, ownerKey),
      { inputMode: msg.turnInputMode, runKind: msg.turnRunKind },
    )
  }

  function sanitizeAssistantTimelineItems(
    items: ChatStreamTimelineItem[],
    provenance: AssistantPresentationProvenance,
  ): ChatStreamTimelineItem[] {
    const textItems = items.filter(
      (item): item is Extract<ChatStreamTimelineItem, { type: 'text' }> => item.type === 'text',
    )
    if (!textItems.length) return items
    const projected = sanitizeAssistantPresentationSegments(
      textItems.map(item => item.rawText || ''),
      provenance,
    )
    let textIndex = 0
    return items.flatMap((item): ChatStreamTimelineItem[] => {
      if (item.type !== 'text') return [item]
      const rawText = projected[textIndex++] ?? ''
      if (!rawText) return []
      if (rawText === item.rawText) return [item]
      return [{
        ...item,
        rawText,
        html: options.renderMarkdown(rawText),
      }]
    })
  }

  function timelineFromSegments(
    segments: ChatTimelineSegment[],
    calls: ChatToolCall[],
    ownerKey: string,
    interrupts: ChatMessage['interrupts'] = [],
  ): ChatStreamTimelineItem[] {
    const groupsById = new Map(toolCallGroups(calls, ownerKey).map(group => [group.groupId, group]))
    const interruptsById = new Map(
      (interrupts ?? []).flatMap(part => {
        const directId = part.approval?.approvalId
        if (directId) return [[directId, part] as const]
        const marker = ':interrupt:'
        const markerIndex = part.key.indexOf(marker)
        return markerIndex >= 0
          ? [[part.key.slice(markerIndex + marker.length), part] as const]
          : []
      }),
    )
    return segments.flatMap((seg, idx): ChatStreamTimelineItem[] => {
      if (seg?.type === 'text') {
        const raw = String(seg.raw ?? seg.text ?? '')
        const presentation = seg.presentation === 'intermediate'
          || seg.presentation === 'answer'
          ? seg.presentation
          : undefined
        return raw ? [{
          type: 'text',
          key: `${ownerKey}:timeline:text:${idx}`,
          html: options.renderMarkdown(raw),
          rawText: raw,
          presentation,
        }] : []
      }
      if (seg?.type === 'tool-group') {
        const groupId = String(seg.groupId || seg.group_id || '')
        const group = groupId ? groupsById.get(groupId) : null
        return group ? [{ type: 'tool-group', key: groupId, group }] : []
      }
      if (seg?.type === 'interrupt') {
        const approvalId = String(seg.approvalId || seg.approval_id || '')
        const part = approvalId ? interruptsById.get(approvalId) : null
        return part
          ? [{
              type: 'interrupt',
              key: part.key || `${ownerKey}:interrupt:${approvalId}`,
              approvalId,
              part,
            }]
          : []
      }
      return []
    })
  }

  function timelineFromPersistedSegments(segments: RawToolCallPayload[], ownerKey: string): ChatStreamTimelineItem[] {
    const items: ChatStreamTimelineItem[] = []
    const callsById = new Map<string, ChatToolCall>()
    let groupSeq = 0

    const appendToolItem = (segment: RawToolCallPayload, index: number): ChatToolCall | null => {
      const name = normalizeToolName(segment)
      if (!name || isInternalToolName(name)) return null
      const toolId = String(segment.tool_use_id || segment.toolId || segment.id || `${name}:${index}`)
      let call = callsById.get(toolId)
      if (!call) {
        const operationKey = toolOperationKey(name)
        const last = items[items.length - 1]
        let group = last?.type === 'tool-group' && last.group.operationKey === operationKey
          ? last.group
          : null
        if (!group) {
          group = {
            groupId: `${ownerKey}:timeline:tool-group:${operationKey}:${groupSeq++}`,
            operationKey,
            label: toolActionLabel(name),
            iconName: toolIconName(name),
            calls: [],
            secondary: '',
            isRunning: false,
            isError: false,
            status: '',
          }
          items.push({ type: 'tool-group', key: group.groupId, group })
        }
        const input = normalizeToolInputText(segment)
        call = {
          toolId,
          name,
          displayName: toolDisplayName(name, input),
          groupId: group.groupId,
          inputRaw: input,
          inputPreview: truncate(input, 200),
          isRunning: false,
          status: '',
          isError: false,
          result: '',
          resultPreview: '',
          isOpen: false,
          renderKey: `${ownerKey}:tool:${toolId}:${group.calls.length}`,
        } as ChatToolCallRenderItem
        group.calls.push(call as ChatToolCallRenderItem)
        callsById.set(toolId, call)
      }
      return call
    }

    segments.forEach((segment, index) => {
      const type = String(segment?.type || '')
      if (type === 'text') {
        const raw = String(segment.text || segment.raw || '')
        const presentation = segment.presentation === 'intermediate'
          || segment.presentation === 'answer'
          ? segment.presentation
          : undefined
        if (raw) items.push({
          type: 'text',
          key: `${ownerKey}:timeline:text:${index}`,
          html: options.renderMarkdown(raw),
          rawText: raw,
          presentation,
        })
        return
      }
      if (type === 'tool_use') {
        appendToolItem(segment, index)
        return
      }
      if (type === 'tool_result') {
        const call = appendToolItem(segment, index)
        if (!call) return
        const result = segment.result || segment.content || segment.output || ''
        const resultStr = typeof result === 'string' ? result : JSON.stringify(result, null, 2)
        const input = normalizeToolInputText(segment)
        if (input && !call.inputPreview) {
          call.inputRaw = input
          call.inputPreview = truncate(input, 200)
          call.displayName = toolDisplayName(call.name, input)
        }
        call.isRunning = false
        call.isError = toolResultIsError(segment)
        call.status = call.isError ? 'error' : 'success'
        call.result = resultStr
        call.resultPreview = truncate(resultStr, 200)
        if (segment.sources !== undefined) call.sources = segment.sources
      }
    })

    for (const item of items) {
      if (item.type !== 'tool-group') continue
      item.group.isRunning = item.group.calls.some(tc => tc.isRunning)
      item.group.isError = item.group.calls.some(tc => tc.isError || tc.status === 'error')
      item.group.status = item.group.isError ? 'error' : (item.group.calls.every(tc => tc.status === 'success') ? 'success' : '')
      item.group.secondary = item.group.calls.length === 1
        ? toolSecondaryText(item.group.calls[0])
        : summarizeToolGroup(item.group.calls)
    }

    return items
  }

  return {
    renderedMessages,
    normalizeRouterDecision,
    routerDecisionCells,
    routerWinnerCellIndex,
    routerDecisionState,
    shortModelName,
    routerFxSortTiers,
  }
}

/**
 * DEV-only soft parity check: confirms the derived `parts[]` cover exactly what
 * the assistant message components render today (text, tools, artifacts,
 * reasoning) and that tool keys/state match their originating calls. Logs
 * console.error on any mismatch and NEVER throws, so it is invisible in
 * production and only surfaces fold regressions during `npm run dev` / e2e.
 */
function assertPartsParity(rendered: ChatRenderedMessage, ownerKey: string): void {
  try {
    const parts = rendered.parts ?? []
    const problems: string[] = []

    // (1) text/timeline coverage
    const textPartKeys = new Set(parts.filter(p => p.type === 'text').map(p => p.key))
    if (rendered.timelineItems?.length) {
      const timelineTextKeys = new Set(
        rendered.timelineItems.filter(item => item.type === 'text').map(item => item.key),
      )
      if (!sameSet(textPartKeys, timelineTextKeys)) {
        problems.push(`text keys diverge from timeline: parts=${[...textPartKeys].join(',')} timeline=${[...timelineTextKeys].join(',')}`)
      }
    } else {
      const expectsText = !!rendered.text
      const hasTextPart = textPartKeys.has(`${ownerKey}:text`)
      if (expectsText !== hasTextPart) {
        problems.push(`plain text part presence ${hasTextPart} != text non-empty ${expectsText}`)
      }
    }

    // (2) tool coverage — callIds + keys vs the originating calls
    const expectedCalls: ChatToolCallRenderItem[] = rendered.timelineItems?.length
      ? rendered.timelineItems.flatMap(item => (item.type === 'tool-group' ? item.group.calls : []))
      : toolCallGroups(rendered.toolCalls, ownerKey).flatMap(g => g.calls)
    const expectedToolKeys = multiset(expectedCalls.map(call => call.renderKey))
    const toolParts = parts.filter(p => p.type === 'tool')
    const actualToolKeys = multiset(toolParts.map(p => p.key))
    if (!sameMultiset(expectedToolKeys, actualToolKeys)) {
      problems.push('tool part keys diverge from originating call renderKeys')
    }
    const callByKey = new Map(expectedCalls.map(call => [call.renderKey, call]))
    for (const part of toolParts) {
      if (part.type !== 'tool') continue
      const call = callByKey.get(part.key)
      if (!call) continue
      if (part.callId !== call.toolId) problems.push(`tool callId ${part.callId} != ${call.toolId}`)
      if (part.state !== toolState(call)) problems.push(`tool state ${part.state} != ${toolState(call)} for ${part.key}`)
    }

    // (3) artifact coverage
    const artifactParts = parts.filter(p => p.type === 'artifact').length
    if (artifactParts !== (rendered.artifacts?.length ?? 0)) {
      problems.push(`artifact parts ${artifactParts} != artifacts ${rendered.artifacts?.length ?? 0}`)
    }

    // (4) reasoning coverage
    const reasoningParts = parts.filter(p => p.type === 'reasoning').length
    const expectedReasoning = rendered.reasoning ? 1 : 0
    if (reasoningParts !== expectedReasoning) {
      problems.push(`reasoning parts ${reasoningParts} != expected ${expectedReasoning}`)
    }

    // (5) source coverage — folded list stays consistent and within the cap
    const sources = rendered.sources ?? []
    if (sources.length > 12) problems.push(`sources ${sources.length} exceeds MAX_SOURCES`)
    sources.forEach((source, index) => {
      if (source.sourceId !== index + 1) problems.push(`source ${index} has sourceId ${source.sourceId}`)
    })

    if (problems.length) {
      console.error('[live-turn parity]', { id: rendered.id, messageId: rendered.messageId, problems })
    }
  } catch (err) {
    console.error('[live-turn parity]', { id: rendered.id, error: String(err) })
  }
}

function sameSet(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) if (!b.has(value)) return false
  return true
}

function multiset(values: string[]): Map<string, number> {
  const map = new Map<string, number>()
  for (const value of values) map.set(value, (map.get(value) ?? 0) + 1)
  return map
}

function sameMultiset(a: Map<string, number>, b: Map<string, number>): boolean {
  if (a.size !== b.size) return false
  for (const [key, count] of a) if (b.get(key) !== count) return false
  return true
}

function routerUsageFromMessage(msg: ChatMessage): ChatMessage['usage'] {
  return msg.routerUsage || msg.usage || msg.turn_usage
}

function routerModelCallIdFromMessage(msg: ChatMessage): string {
  const usage = routerUsageFromMessage(msg)
  return String(
    msg.routerModelCallId
    || usage?.router_model_call_id
    || usage?.routerModelCallId
    || '',
  ).trim()
}

function routerIterationFromMessage(msg: ChatMessage): number {
  const usage = routerUsageFromMessage(msg)
  return Number(
    msg.routerIteration
    || usage?.router_iteration
    || usage?.routerIteration
    || 0,
  ) || 0
}

function routerStripIdentity(strip: ChatRenderedMessage): string {
  if (strip.routerModelCallId) return `call:${strip.routerModelCallId}`
  return `event:${strip.messageId || strip.sourceIndex || strip.id || 'router'}`
}

function routerStripRenderKey(
  turnIdentity: string,
  message: Pick<ChatMessage, 'routerModelCallId' | 'messageId'>
    | Pick<ChatRenderedMessage, 'routerModelCallId' | 'messageId' | 'sourceIndex' | 'id'>,
  messageId?: string,
  index = 0,
): string {
  const callId = String(message.routerModelCallId || '').trim()
  const eventId = routerStripEventId(message, messageId, index)
  return callId
    ? `router-call:${turnIdentity}:${callId}`
    : `router-event:${turnIdentity}:${eventId}`
}

function routerStripEventId(
  message: Pick<ChatMessage, 'messageId'>
    | Pick<ChatRenderedMessage, 'messageId' | 'sourceIndex' | 'id'>,
  messageId?: string,
  index = 0,
): string | number {
  return messageId
    || message.messageId
    || ('sourceIndex' in message ? message.sourceIndex : undefined)
    || ('id' in message ? message.id : undefined)
    || index
}

function routerSettlementCandidateIndex(
  result: ChatRenderedMessage[],
  indexes: Map<string, number>,
  order: number[],
  modelCallId: string | undefined,
): number | undefined {
  const normalizedCallId = String(modelCallId || '').trim()
  if (normalizedCallId) {
    const exactIndex = indexes.get(`call:${normalizedCallId}`)
    if (exactIndex !== undefined) return exactIndex
  }
  for (let cursor = order.length - 1; cursor >= 0; cursor--) {
    const candidateIndex = order[cursor]!
    const candidate = result[candidateIndex]
    if (!candidate?.isRouterStrip) continue
    if (
      !normalizedCallId
      || !candidate.routerModelCallId
      || candidate.routerModelCallId === normalizedCallId
    ) {
      return candidateIndex
    }
  }
  return undefined
}

function upsertRouterStrip(
  result: ChatRenderedMessage[],
  stripItem: ChatRenderedMessage,
  indexes: Map<string, number>,
  order: number[],
  options: { settlement?: boolean; settleReplacement?: boolean } = {},
): void {
  const identity = routerStripIdentity(stripItem)
  let previousIndex = indexes.get(identity) ?? -1
  if (previousIndex < 0 && options.settlement === true) {
    previousIndex = routerSettlementCandidateIndex(
      result,
      indexes,
      order,
      stripItem.routerModelCallId,
    ) ?? -1
  }
  if (previousIndex >= 0) {
    if (options.settleReplacement !== false) stripItem.routerSettled = true
    stripItem.routerTurnKey = result[previousIndex]?.routerTurnKey || stripItem.routerTurnKey
    result[previousIndex] = stripItem
    indexes.set(identity, previousIndex)
    return
  }
  result.push(stripItem)
  const index = result.length - 1
  indexes.set(identity, index)
  order.push(index)
}

function routerRequestKindFromAttachments(attachments: ChatMessage['attachments']): ChatRouterRequestKind {
  return attachments?.some(att => String(att.mime || '').toLowerCase().startsWith('image/'))
    ? 'image'
    : 'text'
}

export function fmtTok(n: number): string {
  if (!n) return '0'
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${+(n / 1_000).toFixed(1)}k`
  return String(n)
}

export function dayKey(ts: string | number | null): string {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  return d.toISOString().slice(0, 10)
}

export function truncate(s: string, max = 200): string {
  if (!s || s.length <= max) return s || ''
  return s.slice(0, max) + '…'
}

export function normalizeRouterDecision(raw: unknown): NormalizedRouterDecision | null {
  if (!raw || typeof raw !== 'object') return null
  const source = raw as Record<string, unknown>
  const rawTier = String(source.tier || source.routed_tier || '').trim()
  const tier = normalizeRouterTextTier(rawTier) || normalizeRouterTier(rawTier)
  if (!tier) return null
  return {
    ...source,
    tier,
    model: String(source.model || source.routed_model || ''),
    baseline_model: String(source.baseline_model || source.baselineModel || ''),
  }
}

function routerDecisionFromUsage(
  msg: ChatMessage,
  inheritedRoute: NormalizedRouterDecision | null = null,
): NormalizedRouterDecision | null {
  const usage = msg.routerUsage || msg.usage || msg.turn_usage
  if (!usage) return inheritedRoute
  if (usage.routing_source === 'none') return inheritedRoute
  const routePlan = usage.route_plan
  const immutablePlan = (
    routePlan
    && typeof routePlan === 'object'
    && !Array.isArray(routePlan)
  )
    ? routePlan as Record<string, unknown>
    : null
  const tier = typeof immutablePlan?.tier === 'string'
    ? immutablePlan.tier
    : typeof usage.routed_tier === 'string' ? usage.routed_tier : ''
  if (!tier) return inheritedRoute
  const source = typeof immutablePlan?.source === 'string'
    ? immutablePlan.source
    : usage.routing_source || 'none'
  return normalizeRouterDecision({
    tier,
    model: immutablePlan?.model || usage.routed_model || usage.model || msg.model || '',
    source,
    confidence: typeof usage.routing_confidence === 'number' ? usage.routing_confidence : 0,
    fallback: source === 'fallback',
    routing_applied: typeof immutablePlan?.routing_applied === 'boolean'
      ? immutablePlan.routing_applied
      : usage.routing_applied !== false,
    rollout_phase: usage.rollout_phase || 'full',
    accepted_routing_mode: msg.turnOutcome?.acceptedRoutingMode,
  })
}

export function routerDecisionState(decision: NormalizedRouterDecision): string {
  if (decision.routing_applied === false) return 'observe'
  if (decision.fallback) return 'fallback'
  return 'settled'
}

export function shortModelName(model: string): string {
  const raw = String(model || '').trim()
  if (!raw) return ''
  const last = raw.includes('/') ? raw.split('/').pop() || raw : raw
  return last
}

function routerFxStripProvider(name: string): string {
  const raw = String(name || '').trim()
  if (!raw) return ''
  const idx = raw.lastIndexOf('/')
  return idx >= 0 ? raw.slice(idx + 1) : raw
}

export function routerFxSortTiers(list: string[]): string[] {
  return sortRouterTiers(list)
}

export function routerWinnerCellIndex(cells: ChatRouterCell[], tier: string): number {
  const norm = normalizeRouterTier(tier)
  return cells.findIndex(cell => cell.kind === 'real' && (cell.tiers || []).includes(norm))
}

export function routerPanelDataset(mode: RouterVisualMode): string {
  return mode === 'legacy_grid' ? 'legacy-grid' : 'real-candidates'
}

function normalizeToolCalls(raw: RawToolCallPayload[] | undefined): ChatToolCall[] {
  if (!raw || !Array.isArray(raw)) return []
  const merged: ChatToolCall[] = []
  const byId = new Map<string, ChatToolCall>()

  raw.forEach((tc, index) => {
    const name = normalizeToolName(tc)
    if (!name) return
    if (isInternalToolName(name)) return
    const input = normalizeToolInputText(tc)
    const result = tc.result || tc.content || tc.output || ''
    const resultStr = typeof result === 'string' ? result : JSON.stringify(result, null, 2)
    const executionStatus = String(tc.execution_status?.status || '')
    const isError = !!(tc.is_error || tc.isError || tc.error || ['error', 'timeout', 'cancelled'].includes(executionStatus))
    const toolId = String(tc.tool_use_id || tc.toolId || tc.id || `${name}:${index}`)
    let item = byId.get(toolId)
    if (!item) {
      item = {
        toolId,
        name,
        displayName: toolDisplayName(name, input),
        groupId: tc.groupId || tc.group_id,
        inputRaw: input,
        inputPreview: '',
        isRunning: false,
        status: '' as '' | 'success' | 'error',
        isError: false,
        result: '',
        resultPreview: '',
        sources: undefined,
        isOpen: false,
      }
      byId.set(toolId, item)
      merged.push(item)
    }
    if (!item.inputPreview && input) {
      item.inputRaw = input
      item.inputPreview = truncate(input, 200)
      item.displayName = toolDisplayName(item.name, input)
    }
    if (resultStr) {
      item.result = resultStr
      item.resultPreview = truncate(resultStr, 200)
      item.status = isError ? 'error' : 'success'
    }
    if (tc.sources !== undefined) item.sources = tc.sources
    if (isError) {
      item.isError = true
      item.status = 'error'
    }
  })

  return merged.map(item => ({
    toolId: item.toolId,
    name: item.name,
    displayName: item.displayName,
    groupId: item.groupId,
    inputRaw: item.inputRaw,
    inputPreview: item.inputPreview,
    isRunning: item.isRunning,
    status: item.status,
    isError: item.isError,
    result: item.result,
    resultPreview: item.resultPreview,
    sources: item.sources,
    isOpen: false,
  }))
}
