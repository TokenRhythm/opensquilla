import { nextTick, ref, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatTimelineSegment,
  ChatUsagePayload,
  RawToolCallPayload,
} from '@/types/chat'
import type { ChatHistoryMessage, ChatHistoryResponse } from '@/types/rpc'
import { normalizeDisplayAttachments } from '@/utils/chat/attachments'
import {
  historyWindowsOverlap,
  reconcileClientStopNotices,
  reconcileClientTerminalNotices,
  reconcileHistoryWindow,
  reconcileRunningHistoryMessages,
} from '@/utils/chat/historyMerge'
import {
  captureVisibleMessageAnchor,
  restoreMessageAnchor,
  stabilizeMessageAnchor,
} from '@/utils/chat/scrollAnchor'

type RpcClient = {
  waitForConnection: () => Promise<void>
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

function recordArray<T extends Record<string, unknown>>(value: unknown): T[] {
  return Array.isArray(value)
    ? value.filter((item): item is T => !!item && typeof item === 'object' && !Array.isArray(item))
    : []
}

function usagePayload(value: unknown): ChatUsagePayload | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as ChatUsagePayload
}

function historyTurnId(value: unknown): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const turnId = (value as Record<string, unknown>).turn_id
  return typeof turnId === 'string' && turnId ? turnId : undefined
}

export interface UseChatHistoryOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  threadRef?: Ref<HTMLElement | null>
  lastHeaderRole: Ref<string>
  lastHeaderDay: Ref<string>
  preserveLiveTail?: Ref<boolean>
  autoScroll?: Ref<boolean>
  stripTimePrefix: (text: string) => string
  scrollToBottom: () => void
}

export interface ChatHistoryState {
  hasMore: boolean
  oldestCursor: string | number | null
  newestCursor: string | number | null
  historyScope: string
  canonicalAvailable: boolean | null
  canonicalComplete: boolean | null
  loading: boolean
  loadingEarlier: boolean
  loadEarlierError: boolean
}

interface HistoryLoadParams {
  before?: string | number | null
  prepend?: boolean
  bridgeRetry?: boolean
}

type FailedHistoryRequest =
  | {
      kind: 'page'
      key: string
      before: string | number | null
      prepend: boolean
    }
  | {
      kind: 'bridge'
      key: string
    }

const MAX_FORWARD_BRIDGE_PAGES = 2

export function useChatHistory(options: UseChatHistoryOptions) {
  let historySyncTimer: ReturnType<typeof setTimeout> | null = null
  let historyRequestSeq = 0
  let historySyncPending = false
  let loadingHistoryKey = ''
  let historySessionKey = ''
  let hasLoadedEarlier = false
  let loadEarlierPending = false
  let failedHistoryRequest: FailedHistoryRequest | null = null
  let stopAnchorStabilization: () => void = () => {}
  const loadedEarlierCursors = new Set<string>()
  const historyState = ref<ChatHistoryState>({
    hasMore: false,
    oldestCursor: null,
    newestCursor: null,
    historyScope: '',
    canonicalAvailable: null,
    canonicalComplete: null,
    loading: false,
    loadingEarlier: false,
    loadEarlierError: false,
  })

  function cancelAnchorStabilization() {
    const stop = stopAnchorStabilization
    stopAnchorStabilization = () => {}
    stop()
  }

  function scheduleHistorySync() {
    if (historySyncTimer) clearTimeout(historySyncTimer)
    historySyncTimer = setTimeout(() => {
      historySyncTimer = null
      if (historyState.value.loading) {
        historySyncPending = true
        return
      }
      void loadHistory()
    }, 50)
  }

  function flushPendingHistorySync() {
    if (historyState.value.loading || failedHistoryRequest) return
    if (loadEarlierPending) {
      loadEarlierPending = false
      void loadEarlierHistory()
      return
    }
    if (!historySyncPending) return
    historySyncPending = false
    scheduleHistorySync()
  }

  function mapHistoryMessage(msg: ChatHistoryMessage): ChatMessage {
    // History rows carry the turn's reasoning text but not the measured
    // thinking duration; live turn records re-fill seconds after sync.
    const reasoningText = typeof msg.reasoning_content === 'string' ? msg.reasoning_content.trim() : ''
    const messageId = msg.message_id || msg.id || ''
    return {
      role: msg.role || 'assistant',
      text: msg.role === 'user' ? options.stripTimePrefix(msg.text || '') : msg.text || '',
      ts: msg.timestamp || msg.ts || null,
      reasoning: reasoningText ? { text: reasoningText, seconds: 0 } : undefined,
      routerDecision: msg.router_decision || msg.routerDecision || null,
      artifacts: msg.artifacts || [],
      tool_calls: recordArray<RawToolCallPayload>(msg.tool_calls),
      timeline: recordArray<ChatTimelineSegment>(msg.timeline),
      attachments: normalizeDisplayAttachments(msg.attachments, { messageId }),
      provenanceKind: msg.provenance_kind || '',
      provenanceSourceSessionKey: msg.provenance_source_session_key || '',
      provenanceSourceTool: msg.provenance_source_tool || '',
      turnId: historyTurnId(msg.turn_context),
      usage: usagePayload(msg.usage) || usagePayload(msg.turn_usage),
      model: msg.model || undefined,
      input: msg.input || msg.input_tokens || undefined,
      output: msg.output || msg.output_tokens || undefined,
      messageId,
      restoredFromHistory: true,
    }
  }

  function messageKey(msg: ChatMessage): string {
    return msg.messageId || `${msg.role}:${msg.ts || ''}:${msg.text || ''}`
  }

  function hasLocalOptimisticRows(messages: ChatMessage[]): boolean {
    return messages.some(msg => msg.restoredFromHistory !== true)
  }

  function responseCanonicalComplete(data: ChatHistoryResponse): boolean | null {
    const value = data.canonical_complete ?? data.canonicalComplete
    return typeof value === 'boolean' ? value : historyState.value.canonicalComplete
  }

  function responseCanonicalAvailable(data: ChatHistoryResponse): boolean | null {
    const value = data.canonical_available ?? data.canonicalAvailable
    return typeof value === 'boolean' ? value : historyState.value.canonicalAvailable
  }

  function updateHistoryState(data: ChatHistoryResponse, prepend: boolean) {
    const nextOldestCursor = data.oldest_cursor ?? data.oldestCursor ?? null
    const requestedCursor = prepend ? historyState.value.oldestCursor : null
    const cursorAdvanced = !prepend || nextOldestCursor !== requestedCursor
    const preserveLoadedBoundary = !prepend && hasLoadedEarlier
    historyState.value = {
      hasMore: preserveLoadedBoundary
        ? historyState.value.hasMore
        : Boolean(data.has_more ?? data.hasMore) && cursorAdvanced,
      oldestCursor: preserveLoadedBoundary ? historyState.value.oldestCursor : nextOldestCursor,
      newestCursor: prepend
        ? historyState.value.newestCursor
        : data.newest_cursor ?? data.newestCursor ?? null,
      historyScope: data.history_scope ?? data.historyScope ?? '',
      canonicalAvailable: responseCanonicalAvailable(data),
      canonicalComplete: responseCanonicalComplete(data),
      loading: false,
      loadingEarlier: false,
      loadEarlierError: false,
    }
  }

  function resetForSession(key: string): boolean {
    if (historySessionKey === key) return false
    cancelAnchorStabilization()
    const crossedSession = Boolean(historySessionKey)
    historySessionKey = key
    hasLoadedEarlier = false
    loadEarlierPending = false
    failedHistoryRequest = null
    loadedEarlierCursors.clear()
    historyState.value = {
      hasMore: false,
      oldestCursor: null,
      newestCursor: null,
      historyScope: '',
      canonicalAvailable: null,
      canonicalComplete: null,
      loading: false,
      loadingEarlier: false,
      loadEarlierError: false,
    }
    return crossedSession
  }

  async function loadHistory(params: HistoryLoadParams = {}) {
    if (!options.sessionKey.value) return
    const key = options.sessionKey.value
    const crossedSession = resetForSession(key)
    if (historyState.value.loading && loadingHistoryKey === key) {
      if (params.prepend) {
        if (!historyState.value.loadingEarlier) loadEarlierPending = true
      } else {
        historySyncPending = true
      }
      return
    }
    cancelAnchorStabilization()
    const requestSeq = ++historyRequestSeq
    let bridgeAttempted = Boolean(params.bridgeRetry)
    loadingHistoryKey = key
    historyState.value = {
      ...historyState.value,
      loading: true,
      loadingEarlier: Boolean(params.prepend || params.bridgeRetry),
      loadEarlierError: false,
    }
    const isCurrentRequest = () => key === options.sessionKey.value && requestSeq === historyRequestSeq
    try {
      await options.rpc.waitForConnection()
      if (!isCurrentRequest()) {
        if (requestSeq === historyRequestSeq) {
          loadingHistoryKey = ''
          historyState.value = { ...historyState.value, loading: false, loadingEarlier: false }
          flushPendingHistorySync()
        }
        return
      }
      const request: Record<string, unknown> = {
        sessionKey: key,
        limit: !params.prepend && options.messages.value.length > 50
          ? Math.min(200, options.messages.value.length)
          : 50,
        includeCanonical: true,
        includeSummaries: false,
      }
      if (params.before != null) request.before = params.before
      const data = await options.rpc.call<ChatHistoryResponse>('chat.history', request)
      if (!isCurrentRequest()) return
      const msgs = data.messages || []
      const canonicalAvailable = data.canonical_available ?? data.canonicalAvailable
      if (canonicalAvailable === false) {
        failedHistoryRequest = hasLoadedEarlier && !params.prepend
          ? { kind: 'bridge', key }
          : {
              kind: 'page',
              key,
              before: params.before ?? null,
              prepend: Boolean(params.prepend),
            }
        if (params.prepend || hasLoadedEarlier) {
          loadingHistoryKey = ''
          historyState.value = {
            ...historyState.value,
            canonicalAvailable: false,
            canonicalComplete: responseCanonicalComplete(data),
            loading: false,
            loadingEarlier: false,
            loadEarlierError: false,
          }
          flushPendingHistorySync()
          return
        }
      }

      let mapped = msgs.map(mapHistoryMessage)
      const previousMessages = crossedSession ? [] : options.messages.value
      let historyData = data
      let bridgeContinuationNeeded = false
      const needsForwardBridge = canonicalAvailable !== false
        && !params.prepend
        && hasLoadedEarlier
        && mapped.length > 0
        && !historyWindowsOverlap(previousMessages, mapped)
      if (needsForwardBridge) {
        bridgeAttempted = true
        failedHistoryRequest = { kind: 'bridge', key }
        historyState.value = {
          ...historyState.value,
          loadingEarlier: true,
          loadEarlierError: false,
        }

        const anchor = [...previousMessages]
          .reverse()
          .find(message => message.restoredFromHistory === true && Boolean(message.messageId))
        const bridgeStart = historyState.value.newestCursor
        if (!anchor || bridgeStart == null) {
          throw new Error('Cannot bridge a history window without a canonical anchor')
        }

        const bridged: ChatMessage[] = []
        const bridgedKeys = new Set<string>()
        const visitedCursors = new Set<string>()
        let after: string | number = bridgeStart
        let finalBridgeData: ChatHistoryResponse | null = null
        let bridgeComplete = responseCanonicalComplete(data)
        let bridgePageCount = 0
        let bridgeTruncated = false

        while (true) {
          const afterKey = String(after)
          if (visitedCursors.has(afterKey)) {
            throw new Error('History forward pagination stalled')
          }
          visitedCursors.add(afterKey)

          const bridgeData = await options.rpc.call<ChatHistoryResponse>('chat.history', {
            sessionKey: key,
            limit: 200,
            after,
            includeCanonical: true,
            includeSummaries: false,
          })
          if (!isCurrentRequest()) return
          const bridgeAvailable = bridgeData.canonical_available ?? bridgeData.canonicalAvailable
          if (bridgeAvailable === false) {
            loadingHistoryKey = ''
            historyState.value = {
              ...historyState.value,
              canonicalAvailable: false,
              canonicalComplete: responseCanonicalComplete(bridgeData),
              loading: false,
              loadingEarlier: false,
              loadEarlierError: false,
            }
            flushPendingHistorySync()
            return
          }

          const page = (bridgeData.messages || []).map(mapHistoryMessage)
          for (const message of page) {
            const keyValue = messageKey(message)
            if (bridgedKeys.has(keyValue)) continue
            bridgedKeys.add(keyValue)
            bridged.push(message)
          }
          finalBridgeData = bridgeData
          bridgePageCount += 1
          const pageComplete = bridgeData.canonical_complete ?? bridgeData.canonicalComplete
          if (pageComplete === false) bridgeComplete = false

          const hasMore = Boolean(bridgeData.has_more ?? bridgeData.hasMore)
          const nextCursor = bridgeData.newest_cursor ?? bridgeData.newestCursor ?? null
          if (page.length === 0 || nextCursor == null || String(nextCursor) === afterKey) {
            throw new Error('History forward pagination did not advance')
          }
          if (!hasMore) break
          if (bridgePageCount >= MAX_FORWARD_BRIDGE_PAGES) {
            bridgeTruncated = true
            break
          }
          after = nextCursor
        }

        if (bridged.length === 0 || finalBridgeData == null) {
          throw new Error('History forward pagination returned no bridge')
        }
        // A disconnected latest window is only safe to merge after the bounded
        // bridge reaches it. Otherwise keep this refresh contiguous and advance
        // newestCursor so a later sync can resume from the new boundary.
        if (!bridgeTruncated) {
          for (const message of mapped) {
            const keyValue = messageKey(message)
            if (bridgedKeys.has(keyValue)) continue
            bridgedKeys.add(keyValue)
            bridged.push(message)
          }
        }

        mapped = [anchor, ...bridged.filter(message => messageKey(message) !== messageKey(anchor))]
        bridgeContinuationNeeded = bridgeTruncated
        historyData = {
          ...data,
          newest_cursor: finalBridgeData.newest_cursor ?? finalBridgeData.newestCursor ?? null,
          canonical_available: true,
          canonical_complete: bridgeComplete ?? undefined,
        }
      }

      if (canonicalAvailable !== false) failedHistoryRequest = null
      updateHistoryState(historyData, Boolean(params.prepend))
      loadingHistoryKey = ''
      if (params.prepend && params.before != null) {
        hasLoadedEarlier = true
        loadedEarlierCursors.add(String(params.before))
      }
      const preserveLiveTail = !crossedSession && Boolean(options.preserveLiveTail?.value)

      if (msgs.length === 0 && !params.prepend) {
        options.messages.value = preserveLiveTail
          ? reconcileRunningHistoryMessages(options.messages.value, [])
          : !crossedSession && hasLocalOptimisticRows(options.messages.value)
            ? options.messages.value
            : []
        if (options.messages.value.length === 0) {
          options.lastHeaderRole.value = ''
          options.lastHeaderDay.value = ''
        }
        flushPendingHistorySync()
        return
      }

      const prependContainer = params.prepend ? options.threadRef?.value ?? null : null
      const prependAnchor = captureVisibleMessageAnchor(prependContainer)
      const prependFallbackHeight = prependAnchor ? 0 : prependContainer?.scrollHeight ?? 0
      if (params.prepend) {
        const existing = new Set(options.messages.value.map(messageKey))
        options.messages.value = [
          ...mapped.filter(msg => !existing.has(messageKey(msg))),
          ...options.messages.value,
        ]
      } else {
        const refreshedWindow = reconcileHistoryWindow(previousMessages, mapped)
        let nextMessages: ChatMessage[]
        if (preserveLiveTail) {
          nextMessages = reconcileRunningHistoryMessages(previousMessages, refreshedWindow)
        } else {
          nextMessages = refreshedWindow
        }
        options.messages.value = reconcileClientTerminalNotices(
          previousMessages,
          reconcileClientStopNotices(previousMessages, nextMessages),
        )
      }

      options.lastHeaderRole.value = ''
      options.lastHeaderDay.value = ''

      if (params.prepend) {
        await nextTick()
        if (prependAnchor) {
          restoreMessageAnchor(prependAnchor)
          stopAnchorStabilization = stabilizeMessageAnchor(prependAnchor, {
            isCurrent: () => options.sessionKey.value === key
              && historySessionKey === key
              && historyRequestSeq === requestSeq,
          })
        } else if (prependContainer) {
          prependContainer.scrollTop += Math.max(
            0,
            prependContainer.scrollHeight - prependFallbackHeight,
          )
        }
      } else if (options.autoScroll?.value ?? true) {
        await nextTick()
        options.scrollToBottom()
      }
      // Keep reconnect catch-up moving even when no later live event arrives.
      // Each scheduled request is still bounded to MAX_FORWARD_BRIDGE_PAGES,
      // and the existing timer/session cleanup makes the continuation yielding
      // and cancellable rather than one unbounded request or DOM update.
      if (bridgeContinuationNeeded) historySyncPending = true
      flushPendingHistorySync()
    } catch {
      // History endpoint may not exist yet.
      if (isCurrentRequest()) {
        failedHistoryRequest = bridgeAttempted
          ? { kind: 'bridge', key }
          : {
              kind: 'page',
              key,
              before: params.before ?? null,
              prepend: Boolean(params.prepend),
            }
        loadingHistoryKey = ''
        historyState.value = {
          ...historyState.value,
          loading: false,
          loadingEarlier: false,
          loadEarlierError: Boolean(
            params.prepend || bridgeAttempted || options.messages.value.length === 0
          ),
        }
        flushPendingHistorySync()
      }
    }
  }

  function loadEarlierHistory() {
    if (!historyState.value.hasMore) return
    if (historyState.value.loading) {
      if (!historyState.value.loadingEarlier) loadEarlierPending = true
      return
    }
    const cursor = historyState.value.oldestCursor
    if (cursor == null || loadedEarlierCursors.has(String(cursor))) return
    return loadHistory({ before: cursor, prepend: true })
  }

  function retryHistory() {
    const failed = failedHistoryRequest
    if (failed?.key === options.sessionKey.value) {
      if (failed.kind === 'bridge') return loadHistory({ bridgeRetry: true })
      return loadHistory({ before: failed.before, prepend: failed.prepend })
    }
    if (historyState.value.canonicalAvailable === false) return loadHistory()
    return loadEarlierHistory()
  }

  function cleanup() {
    if (historySyncTimer) {
      clearTimeout(historySyncTimer)
      historySyncTimer = null
    }
    historySyncPending = false
    loadEarlierPending = false
    cancelAnchorStabilization()
  }

  return {
    historyState,
    loadHistory,
    loadEarlierHistory,
    retryHistory,
    scheduleHistorySync,
    cancelAnchorStabilization,
    cleanup,
  }
}
