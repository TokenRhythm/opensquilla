import type { RpcCallOptions, RpcEventHandler, RpcConnectionWaitOptions } from '@/lib/rpc'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'
import type {
  RouteFeedbackRating,
  SessionCompactResult,
  SessionConversation,
  SessionConversationCapability,
  SessionConversationRequestOptions,
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
  }
}

function objectResult<T extends object>(value: unknown, method: string): T {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${method} returned an invalid response`)
  }
  return value as T
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
      const method = capability === 'reset' ? METHODS.reset
          : capability === 'compact' ? METHODS.compact
            : capability === 'usage' ? METHODS.usage
              : capability === 'slash-catalog' ? METHODS.commands
                : capability === 'route-feedback' ? METHODS.feedback
                  : METHODS.promptStatus
      return hasRpcMethod(rpc, method)
    },
  }
}
