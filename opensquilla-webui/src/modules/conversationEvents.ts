import type { InjectionKey } from 'vue'
import type { ConversationEventSource } from './conversationEventHub'

/** Protocol-neutral meanings emitted by the Conversation event Adapter. */
export type ConversationSemanticEventKind =
  | 'answer-generation-reset'
  | 'approval-requested'
  | 'approval-resolved'
  | 'artifact-created'
  | 'artifact-state-changed'
  | 'collaboration-mode-changed'
  | 'compaction-progress'
  | 'cron-result'
  | 'ensemble-progress'
  | 'goal-changed'
  | 'goal-run-changed'
  | 'input-disposition'
  | 'meta-preflight'
  | 'meta-run-announced'
  | 'meta-run-completed'
  | 'meta-step-state'
  | 'plan-revision'
  | 'plan-run'
  | 'provider-activity'
  | 'router-control-replay'
  | 'router-decision'
  | 'run-heartbeat'
  | 'session-epoch-changed'
  | 'state-changed'
  | 'steer-received'
  | 'subagent-completed'
  | 'task-abandoned'
  | 'task-cancelled'
  | 'task-failed'
  | 'task-group-completed'
  | 'task-group-failed'
  | 'task-group-synthesizing'
  | 'task-group-waiting'
  | 'task-queued'
  | 'task-running'
  | 'task-succeeded'
  | 'task-timed-out'
  | 'text-delta'
  | 'thinking-delta'
  | 'thinking-ended'
  | 'thinking-started'
  | 'tool-result'
  | 'tool-use-delta'
  | 'tool-use-ended'
  | 'tool-use-started'
  | 'turn-committed'
  | 'turn-completed'
  | 'turn-failed'
  | 'warning'
  | 'unknown'

export interface ConversationEventProjection {
  readonly kind: 'known' | 'unknown'
  readonly semanticKind: ConversationSemanticEventKind
  readonly isKnown: boolean
  readonly payload: Readonly<Record<string, unknown>> | null
  readonly rawPayload: unknown
  readonly meta: Readonly<Record<string, unknown>> | null
  readonly sessionKey: string | null
  readonly taskId: string | null
  readonly turnId: string | null
  readonly streamGeneration: string | null
  readonly streamSeq: number | null
  readonly connectionSeq: number | null
  readonly generationEpoch: number | null
  readonly schemaVersion: number | null
  readonly legacy: boolean
}

export type ConversationEvent =
  | {
      kind: 'conversation'
      event: ConversationEventProjection
      /** Opaque producer-owned payload; the Adapter never rewrites tool/user data. */
      payload: unknown
      meta: unknown
    }
  | {
      kind: 'sessions-changed'
      payload: unknown
      meta: unknown
    }
  | {
      kind: 'approval'
      action: 'requested' | 'resolved'
      sessionKey: string | null
      payload: unknown
      meta: unknown
    }
  | {
      kind: 'invalid'
      error: unknown
    }

export type ConversationEvents = ConversationEventSource<ConversationEvent>

export function conversationEventSessionKey(event: ConversationEvent): string | null {
  if (event.kind === 'conversation') return event.event.sessionKey
  if (event.kind === 'approval') return event.sessionKey
  return null
}

export const CONVERSATION_EVENTS_KEY: InjectionKey<ConversationEvents> = Symbol(
  'opensquilla.conversation-events',
)
