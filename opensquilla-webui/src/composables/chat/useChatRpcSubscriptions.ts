import type {
  ArtifactPayload,
  CompactionPayload,
  CronResultPayload,
  EnsembleProgressPayload,
  RouterDecisionPayload,
  SessionEventPayload,
  SubagentCompletionPayload,
  TextDeltaPayload,
  ToolDeltaPayload,
  ToolResultPayload,
  ToolUsePayload,
  WarningPayload,
} from '@/types/rpc'
import type { RpcEventHandler } from '@/lib/rpc'

type RpcSubscriptionClient = {
  on(event: string, handler: RpcEventHandler): () => void
}

export type ChatRpcSubscriptionHandlers = {
  onTextDelta: (payload: TextDeltaPayload) => void
  onToolUseStart: (payload: ToolUsePayload) => void
  onToolUseDelta: (payload: ToolDeltaPayload) => void
  onToolResult: (payload: ToolResultPayload) => void
  onArtifact: (payload: ArtifactPayload) => void
  onStateChange: (payload: SessionEventPayload) => void
  onRunHeartbeat: (payload: SessionEventPayload) => void
  onCompaction: (payload: CompactionPayload, meta: unknown) => void
  onWarning: (payload: WarningPayload) => void
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
  let unsubs: Array<() => void> = []

  function subscribe(): () => void {
    unsubscribe()
    unsubs = [
      rpc.on('session.event.text_delta', handlers.onTextDelta),
      rpc.on('session.event.tool_use_start', handlers.onToolUseStart),
      rpc.on('session.event.tool_use_delta', handlers.onToolUseDelta),
      rpc.on('session.event.tool_result', handlers.onToolResult),
      rpc.on('session.event.artifact', handlers.onArtifact),
      rpc.on('session.event.state_change', handlers.onStateChange),
      rpc.on('session.event.run_heartbeat', handlers.onRunHeartbeat),
      rpc.on('session.event.compaction', handlers.onCompaction),
      rpc.on('session.event.warning', handlers.onWarning),
      rpc.on('session.event.cron_result', handlers.onCronResult),
      rpc.on('session.event.subagent_completion', handlers.onSubagentCompletion),
      rpc.on('session.epoch_changed', handlers.onEpochChanged),
      rpc.on('sessions.changed', handlers.onSessionsChanged),
      rpc.on('task.queued', handlers.onTaskQueued),
      rpc.on('task.running', handlers.onTaskRunning),
      rpc.on('session.event.task_group.waiting', handlers.onTaskGroupWaiting),
      rpc.on('session.event.task_group.synthesizing', handlers.onTaskGroupSynthesizing),
      rpc.on('session.event.task_group.done', handlers.onTaskGroupDone),
      rpc.on('session.event.task_group.failed', handlers.onTaskGroupFailed),
      rpc.on('session.event.router_decision', handlers.onRouterDecision),
      rpc.on('session.event.ensemble_progress', handlers.onEnsembleProgress),
      rpc.on('session.event.router_control_replay', handlers.onRouterControlReplay),
      rpc.on('*', handlers.onAny),
      rpc.on('_state', handlers.onConnectionState),
    ]
    return unsubscribe
  }

  function unsubscribe() {
    unsubs.forEach(fn => fn())
    unsubs = []
  }

  return {
    subscribe,
    unsubscribe,
  }
}
