import type {
  AnswerGenerationResetPayload,
  ArtifactPayload,
  CompactionPayload,
  CronResultPayload,
  EnsembleProgressPayload,
  InputDispositionPayload,
  ProviderActivityPayload,
  RouterDecisionPayload,
  SessionEventPayload,
  SubagentCompletionPayload,
  TextDeltaPayload,
  ToolDeltaPayload,
  ToolEndPayload,
  ToolResultPayload,
  ToolUsePayload,
  WarningPayload,
} from '@/types/rpc'
import {
  createConversationEventTransport,
  type ConversationEventTransportHandlers,
} from '@/adapters/gateway/conversationEventTransport'

type RpcSubscriptionClient = {
  on(event: string, handler: (...args: unknown[]) => void): () => void
}

export type ChatRpcSubscriptionHandlers = {
  onAnswerGenerationReset: (payload: AnswerGenerationResetPayload) => void
  onTextDelta: (payload: TextDeltaPayload) => void
  onToolUseStart: (payload: ToolUsePayload) => void
  onToolUseDelta: (payload: ToolDeltaPayload) => void
  onToolUseEnd: (payload: ToolEndPayload) => void
  onToolResult: (payload: ToolResultPayload) => void
  onArtifact: (payload: ArtifactPayload) => void
  onStateChange: (payload: SessionEventPayload) => void
  onRunHeartbeat: (payload: SessionEventPayload) => void
  onProviderActivity: (payload: ProviderActivityPayload) => void
  onCompaction: (payload: CompactionPayload, meta: unknown) => void
  onWarning: (payload: WarningPayload) => void
  onInputDisposition: (payload: InputDispositionPayload) => void
  onCronResult: (payload: CronResultPayload) => void
  onSubagentCompletion: (payload: SubagentCompletionPayload) => void
  onEpochChanged: (payload: SessionEventPayload) => void
  onSessionsChanged: (payload: SessionEventPayload) => void
  onTaskQueued: (payload: SessionEventPayload) => void
  onTaskRunning: (payload: SessionEventPayload) => void
  onTaskGroupWaiting: (payload: SessionEventPayload) => void
  onTaskGroupSynthesizing: (payload: SessionEventPayload) => void
  onTaskGroupDone: (payload: SessionEventPayload) => void
  onTaskGroupFailed: (payload: SessionEventPayload) => void
  onRouterDecision: (payload: RouterDecisionPayload) => void
  onEnsembleProgress: (payload: EnsembleProgressPayload) => void
  onRouterControlReplay: (payload: SessionEventPayload) => void
  onAny: (rawEvent: string, rawPayload: unknown) => void
  onConnectionState: (state: string) => void
}

export function useChatRpcSubscriptions(
  rpc: RpcSubscriptionClient,
  handlers: ChatRpcSubscriptionHandlers,
) {
  const transport = createConversationEventTransport(rpc)
  let unsubscribeTransport: (() => void) | null = null

  function payloadHandler<T>(
    handler: (payload: T, meta?: unknown) => void,
  ) {
    return (payload: unknown, meta?: unknown) => handler(payload as T, meta)
  }

  function transportHandlers(): ConversationEventTransportHandlers {
    return {
      onAnswerGenerationReset: payloadHandler(handlers.onAnswerGenerationReset),
      onTextDelta: payloadHandler(handlers.onTextDelta),
      onToolUseStart: payloadHandler(handlers.onToolUseStart),
      onToolUseDelta: payloadHandler(handlers.onToolUseDelta),
      onToolUseEnd: payloadHandler(handlers.onToolUseEnd),
      onToolResult: payloadHandler(handlers.onToolResult),
      onArtifact: payloadHandler(handlers.onArtifact),
      onStateChange: payloadHandler(handlers.onStateChange),
      onRunHeartbeat: payloadHandler(handlers.onRunHeartbeat),
      onProviderActivity: payloadHandler(handlers.onProviderActivity),
      onCompaction: payloadHandler(handlers.onCompaction),
      onWarning: payloadHandler(handlers.onWarning),
      onInputDisposition: payloadHandler(handlers.onInputDisposition),
      onCronResult: payloadHandler(handlers.onCronResult),
      onSubagentCompletion: payloadHandler(handlers.onSubagentCompletion),
      onEpochChanged: payloadHandler(handlers.onEpochChanged),
      onSessionsChanged: payloadHandler(handlers.onSessionsChanged),
      onTaskQueued: payloadHandler(handlers.onTaskQueued),
      onTaskRunning: payloadHandler(handlers.onTaskRunning),
      onTaskGroupWaiting: payloadHandler(handlers.onTaskGroupWaiting),
      onTaskGroupSynthesizing: payloadHandler(handlers.onTaskGroupSynthesizing),
      onTaskGroupDone: payloadHandler(handlers.onTaskGroupDone),
      onTaskGroupFailed: payloadHandler(handlers.onTaskGroupFailed),
      onRouterDecision: payloadHandler(handlers.onRouterDecision),
      onEnsembleProgress: payloadHandler(handlers.onEnsembleProgress),
      onRouterControlReplay: payloadHandler(handlers.onRouterControlReplay),
      onAny: handlers.onAny,
      onConnectionState: handlers.onConnectionState,
    }
  }

  function subscribe(): () => void {
    unsubscribe()
    unsubscribeTransport = transport.subscribe(transportHandlers())
    return unsubscribe
  }

  function unsubscribe() {
    unsubscribeTransport?.()
    unsubscribeTransport = null
  }

  return {
    subscribe,
    unsubscribe,
  }
}
