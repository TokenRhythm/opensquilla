export interface AgentOption {
  id: string
  name: string
  model?: string
}

export interface AgentsListResponse {
  agents?: Array<{
    id?: string
    agentId?: string
    name?: string
    model?: string
  }>
}

export interface RawSessionThread {
  id?: string
  kind?: string
}

export interface RawSessionChannelContext {
  name?: string
  id?: string
  accountId?: string
  threadId?: string
}

export interface RawSessionTask {
  status?: string
  started_at?: number | string
  startedAt?: number | string
  finished_at?: number | string
  finishedAt?: number | string
}

export interface RawSessionCron {
  id?: string
  jobId?: string
  job_id?: string
  name?: string
}

export interface RawSessionItem {
  key?: string
  session?: string
  sessionKey?: string
  sessionId?: string
  agentId?: string
  agent_id?: string
  effectiveAgentId?: string
  sessionKind?: string
  surface?: string
  conversationKind?: string
  thread?: RawSessionThread | null
  channelContext?: RawSessionChannelContext | null
  title?: string
  subtitle?: string
  groupLabel?: string
  workspace?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
  updatedAt?: number | string
  updated_at?: number | string
  lastActivityAt?: number | string
  last_activity_at?: number | string
  messageCount?: number
  message_count?: number
  entry_count?: number
  status?: string
  runStatus?: string
  run_status?: string
  active_task?: RawSessionTask | null
  activeTask?: RawSessionTask | null
  last_task?: RawSessionTask | null
  lastTask?: RawSessionTask | null
  terminal_status?: string
  terminalStatus?: string
  display_name?: string
  displayName?: string
  subject?: string
  derived_title?: string
  derivedTitle?: string
  source_kind?: string
  sourceKind?: string
  channel_kind?: string
  channelKind?: string
  channel_id?: string
  channelId?: string
  chat_type?: string
  chatType?: string
  group_id?: string
  groupId?: string
  last_channel?: string
  lastChannel?: string
  last_to?: string
  lastTo?: string
  last_account_id?: string
  lastAccountId?: string
  last_thread_id?: string
  lastThreadId?: string
  delivery_context?: Record<string, unknown>
  deliveryContext?: Record<string, unknown>
  origin?: Record<string, unknown>
  interactive?: boolean
  model?: string
  channel?: Record<string, unknown>
  parent?: Record<string, unknown>
  forked_from_parent?: boolean
  forkedFromParent?: boolean
  cron?: RawSessionCron
}

export type RawSessionListEntry = RawSessionItem | string

export interface SessionsListResponse {
  sessions?: RawSessionListEntry[]
  keys?: RawSessionListEntry[]
}

/** One title/subject match from `sessions.search`. */
export interface SessionSearchHit {
  key: string
  title: string
  effectiveAgentId?: string | null
  surface?: string | null
  updatedAt?: number | null
}

/** One transcript (full-text) match from `sessions.search`. The snippet wraps
 *  matched terms in `>>>`/`<<<` delimiters (highlighted by the renderer). */
export interface MessageSearchHit {
  key: string
  title: string
  role?: string | null
  snippet: string
  createdAt?: number | null
  effectiveAgentId?: string | null
}

export interface SessionsSearchResponse {
  sessions?: SessionSearchHit[]
  messages?: MessageSearchHit[]
  query?: string
  ts?: number
}

export interface ArtifactPayload {
  id?: string
  key?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  stream_seq?: number
  name?: string
  mime?: string
  size?: number | string
  download_url?: string
  thumbnail_url?: string
  [key: string]: unknown
}

export interface StreamEventEnvelope {
  key?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  stream_seq?: number
  [key: string]: unknown
}

export interface SessionEventPayload extends StreamEventEnvelope {
  task_id?: string
  taskId?: string
  reason?: string
  status?: string
  run_status?: string
  runStatus?: string
  terminal_message?: string
  terminal_reason?: string
  message?: string
  code?: string
  group_id?: string
  to_state?: string
  toState?: string
  active_task?: RawSessionTask | null
  last_task?: RawSessionTask | null
  [key: string]: unknown
}

export interface WarningPayload extends SessionEventPayload {
  message?: string
  code?: string
}

export interface CronResultMessagePayload {
  role?: string
  text?: string
  timestamp?: string | number | null
  messageId?: string
  message_id?: string
  provenanceKind?: string
  provenanceSourceTool?: string
  provenanceSourceSessionKey?: string
}

export type CronResultPayload = StreamEventEnvelope & {
  message?: CronResultMessagePayload
}

export interface SubagentCompletionPayload extends SessionEventPayload {
  type?: 'subagent_completion'
  parent_session_key?: string
  child_session_key?: string
  status?: string
  terminal_reason?: string
  message_id?: string
  messageId?: string
  result?: { text?: string; [key: string]: unknown }
}

export interface ApprovalStatusPayload {
  found?: boolean
  id?: string
  namespace?: string
  pending?: boolean
  resolved?: boolean
  approved?: boolean
  resolution?: string
  resolutionInProgress?: boolean
  consumed?: boolean
  deadline?: number
}

export interface TextDeltaPayload extends SessionEventPayload {
  text?: string
}

export interface ToolUsePayload extends SessionEventPayload {
  id?: string
  toolId?: string
  tool_use_id?: string
  toolUseId?: string
  tool_id?: string
  name?: string
  tool_name?: string
  input?: unknown
  input_delta?: string
  inputDelta?: string
  json_fragment?: string
  jsonFragment?: string
  fragment?: string
  // Server wall-clock tool start time (epoch ms). Present on tool_use_start so a
  // running tool's elapsed timer survives page switches / stream replay instead of
  // restarting from a fresh local clock on remount (issue #329). 0/absent => use
  // the local clock.
  started_at?: number
}

export interface ToolDeltaPayload extends ToolUsePayload {
  delta?: string
  input_delta?: string
}

export interface ToolResultPayload extends ToolUsePayload {
  result?: unknown
  content?: unknown
  output?: unknown
  error?: unknown
  is_error?: boolean
  isError?: boolean
  execution_status?: { status?: string }
  executionStatus?: { status?: string }
}

export interface SessionMessagesSubscribeParams {
  key: string
  since_stream_seq?: number
  [key: string]: unknown
}

export interface SessionMessagesSubscribeResponse extends SessionEventPayload {
  subscribed?: boolean
  replay_complete?: boolean
  current_stream_seq?: number
  active_task_group_ids?: string[]
  activeTaskGroupIds?: string[]
}

export interface ChatSendAttachmentPayload {
  type: string
  mime: string
  name: string
  data?: string
  file_uuid?: string
}

export interface ChatSendParams {
  message: string
  sessionKey: string
  /** Stable idempotency key for one logical send attempt. */
  clientRequestId?: string
  /** Stable client identity for reconciling the optimistic user row. */
  clientMessageId?: string
  _source?: { elevated?: string; runMode?: 'standard' | 'trusted' | 'full' }
  intent?: string
  forkBeforeMessageId?: string
  displayText?: string
  attachments?: ChatSendAttachmentPayload[]
  [key: string]: unknown
}

export interface ChatSendResponse {
  sessionKey?: string
  message_id?: string
  user_message_id?: string
  client_message_id?: string
  clientMessageId?: string
  task_id?: string
  taskId?: string
  replayed?: boolean
  task_status?: string
  taskStatus?: string
  terminal_reason?: string
  terminalReason?: string
  terminal_message?: string
  terminalMessage?: string
  reason?: string
}

export interface ChatHistoryAttachmentPayload {
  type?: unknown
  mime?: unknown
  mime_type?: unknown
  media_type?: unknown
  name?: unknown
  filename?: unknown
  size?: unknown
  data?: unknown
  dataUrl?: unknown
  data_url?: unknown
  sha256_ref?: unknown
  download_url?: unknown
  kind?: unknown
  [key: string]: unknown
}

export interface ChatHistoryMessage {
  role?: string
  text?: string
  timestamp?: string | number | null
  ts?: string | number | null
  id?: string
  message_id?: string
  attachments?: ChatHistoryAttachmentPayload[]
  artifacts?: ArtifactPayload[]
  router_decision?: RouterDecisionPayload | null
  routerDecision?: RouterDecisionPayload | null
  tool_calls?: unknown[]
  timeline?: unknown[]
  provenance_kind?: string
  provenance_source_session_key?: string
  provenance_source_tool?: string
  turn_context?: Record<string, unknown>
  reasoning_content?: string
  usage?: unknown
  turn_usage?: unknown
  model?: string
  input?: number
  input_tokens?: number
  output?: number
  output_tokens?: number
}

export interface ChatHistoryResponse {
  messages?: ChatHistoryMessage[]
  has_more?: boolean
  hasMore?: boolean
  oldest_cursor?: string | number | null
  oldestCursor?: string | number | null
  newest_cursor?: string | number | null
  newestCursor?: string | number | null
  history_scope?: string
  historyScope?: string
  canonical_available?: boolean
  canonicalAvailable?: boolean
  canonical_complete?: boolean
  canonicalComplete?: boolean
  limit?: number
  returned?: number
}

export interface RouterDecisionPayload extends SessionEventPayload {
  tier?: string
  model?: string
  routed_model?: string
  source?: string
  routing_applied?: boolean
  decision?: unknown
}

/* ── LLM ensemble progress ─────────────────────────────────────────────
 * Mid-turn `session.event.ensemble_progress` frames announce each ensemble
 * proposer/aggregator starting and finishing, so the router strip can reveal
 * members incrementally before the terminal `done` event lands.
 */
export interface EnsembleProgressPayload extends SessionEventPayload {
  event_type?: 'proposer_start' | 'proposer_finish' | 'aggregator_start' | 'aggregator_finish'
  proposer_index?: number
  proposer_label?: string
  proposer_model?: string
  proposer_provider?: string
  sample_index?: number
  elapsed_ms?: number
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  error?: string
}

export interface CompactionPayload extends SessionEventPayload {
  compacted?: boolean
  detail?: string
}

/* ── MetaSkill run events ──────────────────────────────────────────────
 * Four `session.event.meta_*` frames drive the run-progress ribbon and the
 * preflight checkpoint card. They are delivered through the `*` wildcard
 * handler (handleRpcAny) rather than an explicit rpc.on, so the composable
 * casts the raw payload to these shapes. snake_case keys mirror the gateway. */

export interface MetaPreflightFieldSpec {
  name?: string
  label?: string
  title?: string
  type?: string
  kind?: string
  multiline?: boolean
  required?: boolean
  default?: unknown
  description?: string
  help?: string
  hint?: string
  options?: unknown[]
  choices?: unknown[]
  [key: string]: unknown
}

export interface MetaPreflightRequestTemplate {
  language?: string
  outcome?: string
  deliverable?: string
  fields?: MetaPreflightFieldSpec[]
  [key: string]: unknown
}

export interface MetaPreflightPayload extends SessionEventPayload {
  run_id?: string
  meta_skill_name?: string
  language?: string
  interpreted_request?: string
  missing_fields?: string[]
  assumptions?: string[]
  request_template?: MetaPreflightRequestTemplate
  can_skip?: boolean
  requires_confirmation?: boolean
}

export interface MetaRunStepSpec {
  id?: string
  label?: string
  kind?: string
  depends_on?: string[]
}

export interface MetaRunAnnouncedPayload extends SessionEventPayload {
  run_id?: string
  meta_skill_name?: string
  language?: string
  user_language?: string
  meta_language?: string
  steps?: MetaRunStepSpec[]
  total?: number
}

export interface MetaStepRescueAction {
  id?: string
  label?: string
  [key: string]: unknown
}

export interface MetaStepRescue {
  actions?: MetaStepRescueAction[]
  [key: string]: unknown
}

export interface MetaStepStatePayload extends SessionEventPayload {
  run_id?: string
  step_id?: string
  state?: string
  status_text?: string | null
  error?: string
  substitute_for?: string | null
  rescue?: MetaStepRescue
}

export interface MetaRunCompletedPayload extends SessionEventPayload {
  run_id?: string
  outcome?: string
  completed_steps?: string[]
  failed_steps?: string[]
  recovered_steps?: string[]
  skipped_steps?: string[]
}

export interface RpcEventMap {
  'session.event.text_delta': TextDeltaPayload
  'session.event.tool_use_start': ToolUsePayload
  'session.event.tool_use_delta': ToolDeltaPayload
  'session.event.tool_result': ToolResultPayload
  'session.event.artifact': ArtifactPayload
  'session.event.router_decision': RouterDecisionPayload
  'session.event.ensemble_progress': EnsembleProgressPayload
  'session.event.router_control_replay': SessionEventPayload
  'session.event.state_change': SessionEventPayload
  'session.event.run_heartbeat': SessionEventPayload
  'session.event.compaction': CompactionPayload
  'session.event.warning': SessionEventPayload
  'session.epoch_changed': SessionEventPayload
  'sessions.changed': SessionEventPayload
  'task.queued': SessionEventPayload
  'task.running': SessionEventPayload
  'session.event.task_group.waiting': SessionEventPayload
  'session.event.task_group.synthesizing': SessionEventPayload
  'session.event.task_group.done': SessionEventPayload
  'session.event.task_group.failed': SessionEventPayload
  'session.event.meta_preflight': MetaPreflightPayload
  'session.event.meta_run_announced': MetaRunAnnouncedPayload
  'session.event.meta_step_state': MetaStepStatePayload
  'session.event.meta_run_completed': MetaRunCompletedPayload
}
