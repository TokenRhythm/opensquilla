import type { InjectionKey } from 'vue'
import type { ConversationEventSource } from './conversationEventHub'
import type { ConversationCronResult, ConversationEnsembleProgress, ConversationEventContext, ConversationEventData, ConversationProviderActivity } from './conversationEventContent'
import type { ConversationAnswerReset, ConversationSubagentCompletion } from './conversationEventContent'
import type { ConversationCompactionContent, ConversationTextContent, ConversationThinkingContent, ConversationToolContent } from './conversationEventContent'
import type { ConversationArtifact, ConversationCommittedTurn, ConversationEventIdentity, ConversationInputDisposition, ConversationLifecycle, ConversationRoutingDecision, ConversationTurnCompletion, ConversationWarning } from './conversationEventContent'

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

interface ConversationEventPosition {
  readonly meta: ConversationEventContext
  readonly sessionKey: string | null
  readonly taskId: string | null
  readonly turnId: string | null
  readonly streamGeneration: string | null
  readonly streamSeq: number | null
  readonly connectionSeq: number | null
  readonly generationEpoch: number | null
}

type ProjectedEvent<K extends ConversationSemanticEventKind, P> = ConversationEventPosition & {
  readonly kind: 'known'
  readonly semanticKind: K
  readonly payload: P
}

export type ConversationEventProjection =
  | ProjectedEvent<'cron-result', ConversationCronResult>
  | ProjectedEvent<'provider-activity', ConversationProviderActivity>
  | ProjectedEvent<'ensemble-progress', ConversationEnsembleProgress>
  | ProjectedEvent<'answer-generation-reset', ConversationAnswerReset>
  | ProjectedEvent<'subagent-completed', ConversationSubagentCompletion>
  | ProjectedEvent<'text-delta', ConversationTextContent>
  | ProjectedEvent<'tool-use-started' | 'tool-use-delta' | 'tool-use-ended' | 'tool-result', ConversationToolContent>
  | ProjectedEvent<'thinking-started' | 'thinking-delta' | 'thinking-ended', ConversationThinkingContent>
  | ProjectedEvent<'compaction-progress', ConversationCompactionContent>
  | ProjectedEvent<'turn-completed', ConversationTurnCompletion>
  | ProjectedEvent<'turn-committed', ConversationCommittedTurn>
  | ProjectedEvent<'input-disposition', ConversationInputDisposition>
  | ProjectedEvent<'router-decision', ConversationRoutingDecision>
  | ProjectedEvent<'warning', ConversationWarning>
  | ProjectedEvent<'artifact-created', ConversationArtifact>
  | ProjectedEvent<Extract<ConversationSemanticEventKind, `task-${string}`> | 'turn-failed' | 'state-changed' | 'run-heartbeat' | 'session-epoch-changed', ConversationLifecycle>
  | ProjectedEvent<'approval-requested' | 'approval-resolved' | 'artifact-state-changed' | 'collaboration-mode-changed' | 'goal-changed' | 'goal-run-changed' | 'meta-preflight' | 'meta-run-announced' | 'meta-run-completed' | 'meta-step-state' | 'plan-revision' | 'plan-run' | 'router-control-replay' | 'steer-received', ConversationEventIdentity>
  | (ConversationEventPosition & {
  readonly kind: 'unknown'
  readonly semanticKind: 'unknown'
  readonly diagnostic: { readonly eventName: string }
})

export type ConversationEvent =
  | {
      kind: 'conversation'
      event: ConversationEventProjection
    }
  | {
      kind: 'sessions-changed'
      payload: ConversationEventData
    }
  | {
      kind: 'approval'
      action: 'requested' | 'resolved'
      sessionKey: string | null
      payload: ConversationEventData
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
