import type {
  ChatModelCallSegment,
  ChatRunTask,
  ChatTurnOutcome,
  ChatSteerDisposition,
  ChatUsagePayload,
  ToolPresentation,
} from '@/types/chat'

/** Presentation facts accepted by the existing conversation reducer. Each fact
 * has one spelling; transport aliases and unknown envelope fields stop before
 * this interface. Open values below are explicitly tool/user-owned content. */
export type ConversationEventIdentity = {
  key?: string
  epoch?: number
  task_id?: string
  turn_id?: string
  stream_generation?: string
  stream_seq?: number
  current_stream_seq?: number
  replay_complete?: boolean
  replay_gap_reason?: string
  generation_epoch?: number
  assistant_message_id?: string
  started_at?: number
  emitted_at?: number
  /** Accepted display clock, independent of the strict producer tool clock. */
  activityStartedAt?: number
}

export type ConversationEventData = ConversationEventIdentity & {
  reason?: string
  status?: string
  run_status?: string
  terminal_message?: string
  terminal_reason?: string
  message?: string
  code?: string
  group_id?: string
  to_state?: string
  active_task?: ChatRunTask | null
  last_task?: ChatRunTask | null
  changed_task?: ChatRunTask | null
  turn_outcome?: ChatRunTask['turn_outcome']
  terminalOutcome?: ChatTurnOutcome
  document_mutation_outcome?: ChatRunTask['document_mutation_outcome']
  error_class?: string
  text?: string
  text_snapshot?: string | null
  reasoning_content?: string
  delivery?: 'visible' | 'suppressed'
  suppression_reason?: 'no_reply' | 'heartbeat_ack' | null
  input_mode?: string
  run_kind?: string
  input_tokens?: number
  output_tokens?: number
  cached_tokens?: number
  cache_write?: number
  cost_usd?: number
  model?: string
  usage?: ConversationUsage
  /** Adapter-resolved terminal text: null means no authoritative replacement. */
  finalText?: string | null
  completedTurnId?: string
  model_call_segments?: ChatModelCallSegment[]
  model_usage_breakdown?: ChatUsagePayload['model_usage_breakdown']
  ensemble_trace?: ChatUsagePayload['ensemble_trace']
  route_plan?: ChatUsagePayload['route_plan']
  coverage_status?: string
  usage_unknown?: boolean
  unknown_usage_events?: number
  old_generation_epoch?: number
  new_generation_epoch?: number
  preserve_completed_tools?: boolean
  authoritative_text_snapshot?: string
  authoritative_reasoning_snapshot?: string
  sequence?: number
  terminal?: boolean
  terminal_text_snapshot?: string | null
  activity_id?: string
  phase?: string
  retry_attempt?: number
  retry_limit?: number
  retry_after_ms?: number
  heartbeat?: boolean
  parent_session_key?: string
  child_session_key?: string
  message_id?: string
  finished_at?: number
  session_id?: string
  client_message_id?: string
  user_message_id?: string
  surface_id?: string
  approval_id?: string
  id?: string
  /** Watchdog ownership differs from the renderer's fallback tool identity. */
  watchdogToolId?: string
  name?: string
  input?: unknown
  input_delta?: string
  arguments?: Record<string, unknown>
  synthetic_from_text?: boolean
  result?: unknown
  /** Only a producer result can authorize a clarify lifecycle transition. */
  approvalResult?: unknown
  content?: unknown
  output?: unknown
  error?: unknown
  warningVisible?: boolean
  is_error?: boolean
  execution_status?: { status?: string }
  tool_presentation?: ToolPresentation
  presentation?: 'intermediate' | 'answer'
  model_call_id?: string
  iteration?: number
  block_id?: string
  block_index?: number
  content_kind?: string
  ended_at?: number
  target_turn_id?: string
  client_request_id?: string
  disposition?: ChatSteerDisposition
  promoted_from_turn_id?: string
  promoted_turn_id?: string
  applied_iteration?: number
  failure_code?: string
  error_code?: string
  retryable?: boolean
  recovery?: string
  fallback_safe?: boolean
  revision?: number
  tier?: string
  routed_tier?: string
  routed_model?: string
  baseline_model?: string
  decision_id?: string
  confidence?: number
  fallback?: boolean
  rollout_phase?: string
  accepted_routing_mode?: string
  source?: string
  routing_applied?: boolean
  decision?: unknown
  router_tier_snapshot?: unknown
  event_type?: 'proposer_start' | 'proposer_finish' | 'aggregator_start' | 'aggregator_finish'
  proposer_index?: number
  proposer_label?: string
  proposer_model?: string
  proposer_provider?: string
  /** Stable watchdog ownership before display-only numeric coercion. */
  watchdogMemberId?: string
  sample_index?: number
  elapsed_ms?: number
  compacted?: boolean
  detail?: string
  skip_reason?: string
  compaction_id?: string
  heartbeat_at?: number
  stage?: string
  refused?: boolean
  safe_to_send?: boolean
  applied?: boolean
  durability?: string
  user_visible?: boolean
  intent?: string
  kind?: string
  type?: 'subagent_completion'
  sha256?: string
  mime?: string
  size?: number | string
  created_at?: string
  store?: string
  download_url?: string
  thumbnail_url?: string
}

export type ConversationCronResult = ConversationEventIdentity & {
  message?: {
    role?: string
    text?: string
    timestamp?: string | number | null
    messageId?: string
    provenanceKind?: string
    provenanceSourceTool?: string
    provenanceSourceSessionKey?: string
  }
}

export type ConversationEventContext = {
  replayed?: boolean
  authoritativeLive?: boolean
}

type WithIdentity<K extends keyof ConversationEventData> = Pick<ConversationEventData, keyof ConversationEventIdentity | K>
export type ConversationTextContent = WithIdentity<'text' | 'presentation' | 'model_call_id' | 'iteration'>
export type ConversationToolContent = WithIdentity<'id' | 'watchdogToolId' | 'name' | 'input' | 'input_delta' | 'arguments' | 'synthetic_from_text' | 'result' | 'approvalResult' | 'error' | 'is_error' | 'tool_presentation' | 'model_call_id' | 'iteration'>
export type ConversationThinkingContent = WithIdentity<'text' | 'model_call_id' | 'iteration' | 'block_id' | 'block_index' | 'content_kind' | 'ended_at' | 'status'>
export type ConversationCompactionContent = WithIdentity<'status' | 'compacted' | 'detail' | 'reason' | 'skip_reason' | 'source' | 'phase' | 'compaction_id' | 'sequence' | 'heartbeat' | 'heartbeat_at' | 'elapsed_ms' | 'stage' | 'refused' | 'safe_to_send' | 'applied' | 'durability' | 'user_visible' | 'intent'>
export type ConversationProviderActivity = WithIdentity<'activity_id' | 'phase' | 'reason' | 'retry_attempt' | 'retry_limit' | 'retry_after_ms' | 'heartbeat'>
export type ConversationEnsembleProgress = WithIdentity<'event_type' | 'proposer_index' | 'proposer_label' | 'proposer_model' | 'proposer_provider' | 'watchdogMemberId' | 'sample_index' | 'elapsed_ms' | 'input_tokens' | 'output_tokens' | 'cost_usd' | 'error' | 'error_code'>
export type ConversationAnswerReset = WithIdentity<'old_generation_epoch' | 'new_generation_epoch' | 'preserve_completed_tools' | 'authoritative_text_snapshot' | 'authoritative_reasoning_snapshot' | 'sequence' | 'terminal' | 'terminal_text_snapshot'>
export type ConversationSubagentCompletion = WithIdentity<'type' | 'parent_session_key' | 'child_session_key' | 'status' | 'terminal_reason' | 'message_id'> & { result?: { text?: string; [key: string]: unknown } }
export type ConversationLifecycle = WithIdentity<'reason' | 'status' | 'run_status' | 'terminal_message' | 'terminal_reason' | 'message' | 'code' | 'error_class' | 'group_id' | 'to_state' | 'active_task' | 'last_task' | 'changed_task' | 'terminalOutcome'>
export type ConversationTurnCompletion = ConversationLifecycle & WithIdentity<'finalText' | 'completedTurnId' | 'usage' | 'text' | 'reasoning_content' | 'delivery' | 'suppression_reason' | 'input_mode' | 'run_kind' | 'model_call_segments'>
export type ConversationCommittedTurn = WithIdentity<'status' | 'terminal_reason' | 'finished_at' | 'session_id' | 'client_message_id' | 'user_message_id' | 'surface_id'>
export type ConversationInputDisposition = WithIdentity<'target_turn_id' | 'client_request_id' | 'client_message_id' | 'user_message_id' | 'disposition' | 'promoted_from_turn_id' | 'promoted_turn_id' | 'applied_iteration' | 'model_call_id' | 'failure_code' | 'retryable' | 'recovery' | 'fallback_safe' | 'revision' | 'intent'>
export type ConversationRoutingSnapshot = Pick<ConversationEventData, 'tier' | 'model' | 'routed_tier' | 'routed_model' | 'baseline_model' | 'decision_id' | 'confidence' | 'fallback' | 'rollout_phase' | 'accepted_routing_mode' | 'source' | 'routing_applied' | 'decision' | 'router_tier_snapshot'>
export type ConversationRoutingDecision = ConversationEventIdentity & ConversationRoutingSnapshot
export type ConversationWarning = WithIdentity<'message' | 'warningVisible'>
export type ConversationArtifact = WithIdentity<'id' | 'kind' | 'name' | 'sha256' | 'mime' | 'size' | 'created_at' | 'store' | 'download_url' | 'thumbnail_url'>
export type ConversationUsage = Pick<ChatUsagePayload,
  'model' | 'routed_model' | 'input_tokens' | 'output_tokens' | 'cached_tokens' | 'reasoning_tokens'
  | 'cost_usd' | 'routed_tier' | 'routing_source' | 'total_savings_pct' | 'total_savings_usd'
  | 'savings_usd' | 'savings_pct' | 'model_usage_breakdown' | 'ensemble_trace' | 'route_plan'
  | 'model_call_segments' | 'router_model_call_id' | 'router_iteration' | 'coverage_status'
  | 'usage_unknown' | 'unknown_usage_events' | 'decision_id' | '__savings_ui_suppressed'
> & { cache_write?: number; cache_write_tokens?: number; billed_cost?: number; total_tokens?: number; estimated_cost_component_usd?: number }
