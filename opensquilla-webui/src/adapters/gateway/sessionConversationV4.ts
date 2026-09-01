import type { RpcCallOptions, RpcEventHandler, RpcConnectionWaitOptions } from '@/lib/rpc'
import type {
  ChatHistoryResponse,
  SessionEventPayload,
} from '@/types/chat'
import type {
  SessionMessagesSnapshotResponse,
  SessionMessagesSubscribeResponse,
} from '@/modules/sessionConversation'
import { conversationSemanticEventKind } from './conversationEventsV4'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'
import type {
  RouteFeedbackRating,
  SessionAbortResult,
  SessionCompactResult,
  SessionConversation,
  SessionConversationCapability,
  SessionConversationRequestOptions,
  SessionForkRequest,
  SessionForkResult,
  SessionHistoryRequest,
  SessionPreviewResult,
  SlashCommandCatalogResult,
  UsageStatusResult,
  PromptCacheKeepaliveUpdate,
  RouteFeedbackResult,
} from '@/modules/sessionConversation'

interface SessionConversationRpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  ready(options?: RpcConnectionWaitOptions & { timeoutMs?: number; signal?: AbortSignal }): Promise<void>
  supports?(method: string): boolean
}

interface SessionConversationEventTransport {
  subscribe(event: string, handler: RpcEventHandler): { close(): void }
  supports?(event: string): boolean
}

const METHODS = {
  subscribe: 'sessions.messages.subscribe',
  hydrate: 'sessions.messages.hydrate',
  snapshot: 'sessions.messages.snapshot',
  unsubscribe: 'sessions.messages.unsubscribe',
  history: 'chat.history',
  preview: 'sessions.preview',
  abort: 'sessions.abort',
  fork: 'sessions.fork',
  forkThroughTurn: 'sessions.forkThroughTurn',
  reset: 'sessions.reset',
  compact: 'sessions.contextCompact',
  usage: 'usage.status',
  commands: 'commands.list_for_surface',
  feedback: 'router.feedback.submit',
  promptStatus: 'sessions.promptCacheKeepalive.status',
  promptSet: 'sessions.promptCacheKeepalive.set',
  clarify: 'chat.clarify_submit',
} as const

function requestOptions(options?: SessionConversationRequestOptions): RpcCallOptions | undefined {
  if (!options) return undefined
  return {
    signal: options.signal,
    timeoutMs: options.timeoutMs,
    timeoutAction: options.timeoutAction,
    abortAction: options.abortAction,
    expectedGeneration: options.expectedGeneration,
    onSent: options.onSent,
  }
}

function objectResult<T extends object>(value: unknown, method: string): T {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${method} returned an invalid response`)
  }
  return value as T
}

type WireSessionMessagesSnapshotResponse = Omit<SessionMessagesSnapshotResponse, 'events'> & {
  events?: Array<{ event?: unknown, payload?: unknown }>
}

function semanticSnapshotResult(value: unknown): SessionMessagesSnapshotResponse {
  const snapshot = objectResult<WireSessionMessagesSnapshotResponse>(value, METHODS.snapshot)
  const { events, ...base } = snapshot
  if (!Array.isArray(events)) return base
  return {
    ...base,
    events: events.map(entry => ({
      event: conversationSemanticEventKind(String(entry?.event || '')),
      payload: (
        entry?.payload && typeof entry.payload === 'object' && !Array.isArray(entry.payload)
          ? entry.payload
          : {}
      ) as SessionEventPayload,
    })),
  }
}

function normalizeHistoryParams(request: SessionHistoryRequest): Record<string, unknown> {
  const params: Record<string, unknown> = {
    sessionKey: request.sessionKey,
  }
  if (request.limit !== undefined) params.limit = request.limit
  if (request.before !== undefined && request.before !== null) params.before = request.before
  if (request.after !== undefined && request.after !== null) params.after = request.after
  if (request.includeCanonical !== undefined) params.includeCanonical = request.includeCanonical
  if (request.includeSummaries !== undefined) params.includeSummaries = request.includeSummaries
  return params
}

function hasRpcMethod(
  rpc: SessionConversationRpcTransport,
  method: string,
): boolean {
  return rpc.supports?.(method) !== false
}

export function createV4SessionConversation(
  rpc: SessionConversationRpcTransport,
  events: SessionConversationEventTransport,
): SessionConversation {
  const request = <T>(
    method: string,
    params: Record<string, unknown> | undefined,
    options?: SessionConversationRequestOptions,
  ) => {
    const callOptions = requestOptions(options)
    return callOptions === undefined
      ? rpc.request<T>(method, params)
      : rpc.request<T>(method, params, callOptions)
  }

  return {
    ready: options => rpc.ready(options
        ? {
            timeoutMs: options.timeoutMs,
            signal: options.signal,
            timeoutAction: 'reject',
            abortAction: 'reject',
          }
        : undefined),

    subscribe: (params, options) => request<SessionMessagesSubscribeResponse>(METHODS.subscribe, params, options),

    hydrate: async (key, options) => objectResult<SessionMessagesSubscribeResponse>(
      await request(METHODS.hydrate, { key }, options),
      METHODS.hydrate,
    ),

    snapshot: async (key, options) => semanticSnapshotResult(
      await request(METHODS.snapshot, { key }, options),
    ),

    unsubscribe: async (key, options) => {
      await request(METHODS.unsubscribe, { key }, options)
    },

    history: async (historyRequest, options) => objectResult<ChatHistoryResponse>(
      await request(METHODS.history, normalizeHistoryParams(historyRequest), options),
      METHODS.history,
    ),

    preview: async (keys, options) => objectResult<SessionPreviewResult>(
      await request(METHODS.preview, { keys: [...keys] }, options),
      METHODS.preview,
    ),

    abort: async (key, options) => objectResult<SessionAbortResult>(
      await request(METHODS.abort, { key }, options),
      METHODS.abort,
    ),

    fork: async (forkRequest: SessionForkRequest, options): Promise<SessionForkResult> => {
      const params: Record<string, unknown> = { key: forkRequest.key }
      if (forkRequest.beforeMessageId) params.beforeMessageId = forkRequest.beforeMessageId
      if (forkRequest.throughTurnId) {
        params.throughTurnId = forkRequest.throughTurnId
        const method = hasRpcMethod(rpc, METHODS.forkThroughTurn)
          ? METHODS.forkThroughTurn
          : METHODS.fork
        return objectResult<SessionForkResult>(await request(method, params, options), method)
      }
      return objectResult<SessionForkResult>(await request(METHODS.fork, params, options), METHODS.fork)
    },

    reset: async (key, options) => objectResult<Record<string, unknown>>(
      await request(METHODS.reset, { key }, options),
      METHODS.reset,
    ),

    compact: async (key, wait = false, options) => objectResult<SessionCompactResult>(
      await request(METHODS.compact, { key, wait }, options),
      METHODS.compact,
    ),

    usage: async (sessionKey, options) => objectResult<UsageStatusResult>(
      await request(
        METHODS.usage,
        sessionKey ? { sessionKey } : undefined,
        options,
      ),
      METHODS.usage,
    ),

    listCommands: async (surface, options) => objectResult<SlashCommandCatalogResult>(
      await request(METHODS.commands, { surface }, options),
      METHODS.commands,
    ),

    submitRouteFeedback: async (
      decisionId: string,
      rating: RouteFeedbackRating,
      options,
    ) => objectResult<RouteFeedbackResult>(
      await request(METHODS.feedback, { decisionId, rating }, options),
      METHODS.feedback,
    ),

    promptCacheStatus: async (key, options) => objectResult<PromptCacheKeepaliveStatus>(
      await request(METHODS.promptStatus, { key }, options),
      METHODS.promptStatus,
    ),

    setPromptCacheStatus: async (update: PromptCacheKeepaliveUpdate, options) => objectResult<PromptCacheKeepaliveStatus>(
      await request(METHODS.promptSet, {
        key: update.key,
        enabled: update.enabled,
        ttlSeconds: update.ttlSeconds,
        idleTimeoutSeconds: update.idleTimeoutSeconds,
      }, options),
      METHODS.promptSet,
    ),

    submitClarify: async (params, options) => objectResult<Record<string, unknown>>(
      await request(METHODS.clarify, params, options),
      METHODS.clarify,
    ),

    subscribeToolResults(listener) {
      return events.subscribe('session.event.tool_result', listener)
    },

    subscribeRoutingChanged(listener) {
      return events.subscribe('models.routing.changed', payload => {
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return
        listener(payload)
      })
    },

    supports(capability: SessionConversationCapability): boolean {
      if (capability === 'turn-committed') {
        return events.supports?.('session.event.turn_committed') !== false
      }
      const method = capability === 'messages' ? METHODS.subscribe
        : capability === 'history' ? METHODS.history
          : capability === 'preview' ? METHODS.preview
            : capability === 'abort' ? METHODS.abort
              : capability === 'fork' ? METHODS.fork
                : capability === 'reset' ? METHODS.reset
                  : capability === 'compact' ? METHODS.compact
                    : capability === 'usage' ? METHODS.usage
                      : capability === 'slash-catalog' ? METHODS.commands
                        : capability === 'route-feedback' ? METHODS.feedback
                          : METHODS.promptStatus
      return hasRpcMethod(rpc, method)
    },
  }
}
