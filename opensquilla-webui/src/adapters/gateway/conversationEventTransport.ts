import type { RpcEventHandler } from '@/lib/rpc'
import {
  decodeConversationEvent,
  type DecodedConversationEvent,
} from './conversationEventsV4'

/**
 * The WebSocket client exposes one untyped wildcard event stream.  Keep that
 * transport detail in this adapter: composables receive named callbacks and
 * never register wire event strings themselves.
 *
 * The callback payload deliberately remains `unknown` here.  The adapter is
 * responsible for framing, aliases, and validation; the domain consumer owns
 * the payload projection and can preserve the existing v4 shapes during the
 * migration.
 */
export interface ConversationEventTransportHandlers {
  onAnswerGenerationReset?: (payload: unknown, meta?: unknown) => void
  onTextDelta?: (payload: unknown, meta?: unknown) => void
  onToolUseStart?: (payload: unknown, meta?: unknown) => void
  onToolUseDelta?: (payload: unknown, meta?: unknown) => void
  onToolUseEnd?: (payload: unknown, meta?: unknown) => void
  onToolResult?: (payload: unknown, meta?: unknown) => void
  onArtifact?: (payload: unknown, meta?: unknown) => void
  onStateChange?: (payload: unknown, meta?: unknown) => void
  onRunHeartbeat?: (payload: unknown, meta?: unknown) => void
  onProviderActivity?: (payload: unknown, meta?: unknown) => void
  onCompaction?: (payload: unknown, meta?: unknown) => void
  onWarning?: (payload: unknown, meta?: unknown) => void
  onInputDisposition?: (payload: unknown, meta?: unknown) => void
  onCronResult?: (payload: unknown, meta?: unknown) => void
  onSubagentCompletion?: (payload: unknown, meta?: unknown) => void
  onEpochChanged?: (payload: unknown, meta?: unknown) => void
  onSessionsChanged?: (payload: unknown, meta?: unknown) => void
  onTaskQueued?: (payload: unknown, meta?: unknown) => void
  onTaskRunning?: (payload: unknown, meta?: unknown) => void
  onTaskGroupWaiting?: (payload: unknown, meta?: unknown) => void
  onTaskGroupSynthesizing?: (payload: unknown, meta?: unknown) => void
  onTaskGroupDone?: (payload: unknown, meta?: unknown) => void
  onTaskGroupFailed?: (payload: unknown, meta?: unknown) => void
  onRouterDecision?: (payload: unknown, meta?: unknown) => void
  onEnsembleProgress?: (payload: unknown, meta?: unknown) => void
  onRouterControlReplay?: (payload: unknown, meta?: unknown) => void
  /** Preserve the legacy observation path for terminal/thinking/future events. */
  onAny?: (rawEvent: string, rawPayload: unknown) => void
  onConnectionState?: (state: string) => void
  onDecodeError?: (error: unknown, rawEvent: string, rawPayload: unknown) => void
}

type RpcSubscriptionClient = {
  on(event: string, handler: RpcEventHandler): () => void
}

type NamedHandler = keyof Omit<
  ConversationEventTransportHandlers,
  'onAny' | 'onConnectionState' | 'onDecodeError'
>

/** Wire names stay in one place, next to the decoder that owns them. */
const NAMED_HANDLERS: Readonly<Record<string, NamedHandler>> = Object.freeze({
  'session.event.answer_generation_reset': 'onAnswerGenerationReset',
  'session.event.text_delta': 'onTextDelta',
  'session.event.tool_use_start': 'onToolUseStart',
  'session.event.tool_use_delta': 'onToolUseDelta',
  'session.event.tool_use_end': 'onToolUseEnd',
  'session.event.tool_result': 'onToolResult',
  'session.event.artifact': 'onArtifact',
  'session.event.state_change': 'onStateChange',
  'session.event.run_heartbeat': 'onRunHeartbeat',
  'session.event.provider_activity': 'onProviderActivity',
  'session.event.compaction': 'onCompaction',
  'session.event.warning': 'onWarning',
  'session.event.input_disposition': 'onInputDisposition',
  'session.event.cron_result': 'onCronResult',
  'session.event.subagent_completion': 'onSubagentCompletion',
  'session.epoch_changed': 'onEpochChanged',
  'task.queued': 'onTaskQueued',
  'task.running': 'onTaskRunning',
  'session.event.task_group.waiting': 'onTaskGroupWaiting',
  'session.event.task_group.synthesizing': 'onTaskGroupSynthesizing',
  'session.event.task_group.done': 'onTaskGroupDone',
  'session.event.task_group.failed': 'onTaskGroupFailed',
  'session.event.router_decision': 'onRouterDecision',
  'session.event.ensemble_progress': 'onEnsembleProgress',
  'session.event.router_control_replay': 'onRouterControlReplay',
})

function dispatchNamed(
  handlers: ConversationEventTransportHandlers,
  decoded: DecodedConversationEvent,
  payload: unknown,
  meta: unknown,
) {
  if (decoded.kind !== 'known') return
  const handlerName = NAMED_HANDLERS[decoded.name]
  if (!handlerName) return
  handlers[handlerName]?.(payload, meta)
}

/** Create the one WebSocket event listener used by the Conversation lane. */
export function createConversationEventTransport(rpc: RpcSubscriptionClient) {
  let detach: (() => void) | null = null

  function subscribe(handlers: ConversationEventTransportHandlers): () => void {
    detach?.()
    const onEvent: RpcEventHandler = (
      rawEvent: unknown,
      rawPayload: unknown,
      rawMeta: unknown,
    ) => {
      const eventName = typeof rawEvent === 'string' ? rawEvent : String(rawEvent ?? '')

      // `sessions.changed` has its own Contract family.  It is intentionally
      // handled here as a directory event until the Session Event lane merges
      // both manifests; it must still pass through the same single listener.
      if (eventName === 'sessions.changed') {
        handlers.onSessionsChanged?.(rawPayload, rawMeta)
        handlers.onAny?.(eventName, rawPayload)
        return
      }

      try {
        const decoded = decodeConversationEvent(eventName, rawPayload, rawMeta)
        dispatchNamed(handlers, decoded, rawPayload, rawMeta)
      } catch (error) {
        // A malformed or unrelated frame must not take down the shared event
        // stream. Preserve the old wildcard observation path and report the
        // contract violation for diagnostics.
        handlers.onDecodeError?.(error, eventName, rawPayload)
      }
      handlers.onAny?.(eventName, rawPayload)
    }

    const offWildcard = rpc.on('*', onEvent)
    const offState = rpc.on('_state', (state: unknown) => {
      handlers.onConnectionState?.(String(state))
    })
    detach = () => {
      offWildcard()
      offState()
      detach = null
    }
    return detach
  }

  function unsubscribe() {
    detach?.()
  }

  return { subscribe, unsubscribe }
}

