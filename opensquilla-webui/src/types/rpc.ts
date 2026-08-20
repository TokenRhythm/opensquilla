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
  task_id?: string
  taskId?: string
  turn_id?: string
  turnId?: string
  started_at?: number | string
  startedAt?: number | string
  finished_at?: number | string
  finishedAt?: number | string
  turn_outcome?: Record<string, unknown>
  turnOutcome?: Record<string, unknown>
  document_mutation_outcome?: Record<string, unknown>
  documentMutationOutcome?: Record<string, unknown>
  steer_capability?: import('./chat').ChatSteerCapability
  steerCapability?: import('./chat').ChatSteerCapability
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
  workspaceId?: string
  workspace_id?: string
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
  /** Number of rows returned in this page. */
  count?: number
  /** Exact number of sessions visible to the caller, independent of page size. */
  totalCount?: number
  total_count?: number
  /** Whether another stable session-list page is available. */
  hasMore?: boolean
  has_more?: boolean
  /** Opaque keyset cursor for the next session-list page. */
  nextCursor?: string | null
  next_cursor?: string | null
}

export interface ProjectWorkspaceItem {
  id: string
  name: string
  path: string
  taskCount: number
  pinned: boolean
  available: boolean
  availabilityReason?: string
}

export interface ProjectWorkspacesResponse {
  workspaces?: ProjectWorkspaceItem[]
}

export interface SandboxPathEntry {
  name: string
  path: string
  kind: 'directory' | 'file'
  selectable: boolean
  hidden?: boolean
}

export interface SandboxPathListResponse {
  currentPath: string
  path: string
  parentPath: string | null
  systemPickerAvailable: boolean
  entries: SandboxPathEntry[]
}

export interface ProjectWorkspaceHistoryDeleteResponse {
  workspaceId?: string
  deletedTaskCount?: number
  deletedSessionKeys?: string[]
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
  kind?: string
  sha256?: string
  session_id?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  generation_epoch?: number
  generationEpoch?: number
  stream_seq?: number
  name?: string
  mime?: string
  size?: number | string
  source?: string
  created_at?: string
  createdAt?: string
  store?: string
  download_url?: string
  thumbnail_url?: string
  [key: string]: unknown
}

export interface ArtifactsListResponse {
  artifacts?: ArtifactPayload[]
  has_more?: boolean
  hasMore?: boolean
  oldest_cursor?: string | null
  oldestCursor?: string | null
  newest_cursor?: string | null
  newestCursor?: string | null
  total_count?: number
  totalCount?: number
  page_size?: number
  pageSize?: number
}

export interface ArtifactsGetResponse {
  artifact?: ArtifactPayload | null
}

export interface StreamEventEnvelope {
  key?: string
  session_key?: string
  sessionKey?: string
  epoch?: number
  stream_generation?: string
  streamGeneration?: string
  generation_epoch?: number
  generationEpoch?: number
  assistant_message_id?: string
  assistantMessageId?: string
  stream_seq?: number
  [key: string]: unknown
}

export interface SessionEventPayload extends StreamEventEnvelope {
  task_id?: string
  taskId?: string
  turn_id?: string
  turnId?: string
  started_at?: number
  emitted_at?: number
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

export interface AnswerGenerationResetPayload extends SessionEventPayload {
  kind?: 'answer_generation_reset'
  old_generation_epoch?: number
  oldGenerationEpoch?: number
  new_generation_epoch?: number
  newGenerationEpoch?: number
  preserve_completed_tools?: boolean
  preserveCompletedTools?: boolean
  authoritative_text_snapshot?: string
  authoritativeTextSnapshot?: string
  authoritative_reasoning_snapshot?: string
  authoritativeReasoningSnapshot?: string
  sequence?: number
  terminal?: boolean
  terminal_text_snapshot?: string | null
  terminalTextSnapshot?: string | null
}

export interface WarningPayload extends SessionEventPayload {
  message?: string
  code?: string
}

/** Content-free invalidation signal for one stable Artifact IDE document. */
export interface ArtifactStateEventPayload extends SessionEventPayload {
  artifactEventSeq?: number
  documentId?: string
  revisionId?: string | null
  changeSetId?: string | null
  action?: string
}

export type ProviderActivityPhase =
  | 'requesting'
  | 'reasoning'
  | 'retry_wait'
  | 'retrying'
  | 'fallback'

export type ProviderActivityReason =
  | 'initial'
  | 'rate_limited'
  | 'provider_overloaded'
  | 'transport_transient'
  | 'reasoning_only'
  | 'empty_response'
  | 'stream_incomplete'
  | 'invalid_response'
  | 'context_overflow'
  | 'unknown'

export interface ProviderActivityPayload extends SessionEventPayload {
  schema_version?: 1
  activity_id?: string
  phase?: ProviderActivityPhase
  reason?: ProviderActivityReason
  retry_attempt?: number
  retry_limit?: number
  retry_after_ms?: number
  started_at?: number
  heartbeat?: boolean
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
  /** Gateway-owned semantic role for this text span. */
  presentation?: 'intermediate' | 'answer'
  model_call_id?: string
  modelCallId?: string
  iteration?: number
}

export type AssistantDelivery = 'visible' | 'suppressed'
export type AssistantSuppressionReason = 'no_reply' | 'heartbeat_ack'

/**
 * Additive terminal-delivery contract. Older gateways omit these fields; the
 * client then retains the conservative presentation-only sentinel fallback.
 */
export interface SessionDonePayload extends SessionEventPayload {
  text?: string
  text_snapshot?: string | null
  textSnapshot?: string | null
  delivery?: AssistantDelivery
  suppression_reason?: AssistantSuppressionReason | null
  suppressionReason?: AssistantSuppressionReason | null
  /** Additive turn provenance; snake_case is the canonical gateway spelling. */
  input_mode?: string
  inputMode?: string
  run_kind?: string
  runKind?: string
}

/** Durable-success receipt emitted only after transcript and task commits. */
export interface TurnCommittedPayload extends SessionEventPayload {
  schema_version: 1
  session_key: string
  session_id?: string
  task_id: string
  turn_id: string
  status: 'succeeded'
  terminal_reason: 'completed'
  finished_at: number
  client_message_id?: string
  user_message_id?: string
  surface_id?: string
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

export interface ToolEndPayload extends ToolUsePayload {
  arguments?: Record<string, unknown>
  synthetic_from_text?: boolean
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
  since_stream_generation?: string
  since_stream_seq?: number
  [key: string]: unknown
}

export interface SessionLiveSnapshotEvent {
  event: string
  payload: SessionEventPayload
}

export interface SessionMessagesSnapshotResponse {
  key: string
  task_id?: string | null
  stream_generation?: string
  current_stream_seq?: number
  events?: SessionLiveSnapshotEvent[]
}

export interface SessionProjectWorkspaceSnapshot {
  id: string
  name: string
  path: string
  available: boolean
  removed: boolean
  availabilityReason?: string
}

/** Durable, session-owned model routing selection. */
export interface SessionRoutingSnapshot {
  key?: string
  sessionKey?: string
  session_key?: string
  mode?: import('./modelRouting').GatewayModelRoutingMode
  revision?: number
  source?: 'session' | 'legacy_initialized' | string
}

export interface SessionMessagesSubscribeResponse extends SessionEventPayload {
  subscribed?: boolean
  hydration_complete?: boolean
  hydrationComplete?: boolean
  deferred_fields?: string[]
  deferredFields?: string[]
  projectWorkspaceDeferred?: boolean
  project_workspace_deferred?: boolean
  replay_complete?: boolean
  current_stream_seq?: number
  active_task_group_ids?: string[]
  activeTaskGroupIds?: string[]
  run_mode_lock?: {
    locked?: boolean
    runMode?: 'safe' | 'full'
    source?: string
  }
  runModeLock?: {
    locked?: boolean
    runMode?: 'safe' | 'full'
    source?: string
  }
  workspaceId?: string
  projectWorkspace?: SessionProjectWorkspaceSnapshot | null
  routing?: SessionRoutingSnapshot
  modelRouting?: SessionRoutingSnapshot
  model_routing?: SessionRoutingSnapshot
  collaboration?: import('./plans').CollaborationSnapshot
  currentPlan?: import('./plans').PlanRevisionSnapshot | null
  current_plan?: unknown
  activePlanRun?: import('./plans').PlanRunSnapshot | null
  active_plan_run?: unknown
  goal?: unknown
  goalSnapshotStreamSeq?: number | null
  goal_snapshot_stream_seq?: number | null
  pendingUserInputs?: unknown[]
  pending_user_inputs?: unknown[]
}

export interface ChatSendAttachmentPayload {
  type: string
  mime: string
  name: string
  data?: string
  file_uuid?: string
}

/** Exact editable document head bound to one chat send attempt. */
export interface ChatDocumentContext {
  documentId: string
  headRevisionId: string
}

export interface ChatSendParams {
  message: string
  sessionKey: string
  /** Stable idempotency key for one logical send attempt. */
  clientRequestId?: string
  /** Stable client identity for reconciling the optimistic user row. */
  clientMessageId?: string
  /** Ordered durable drafts consumed atomically with this chat ingress. */
  promptAnnotationIds?: string[]
  /** Current editable document head made available only to this turn. */
  documentContext?: ChatDocumentContext
  _source?: { elevated?: string; runMode?: 'safe' | 'full' }
  intent?: string
  workspaceId?: string
  collaborationMode?: import('./plans').CollaborationMode
  /** Session routing mode atomically captured when a new chat is materialized. */
  initialRoutingMode?: import('./modelRouting').GatewayModelRoutingMode
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
  acceptedPromptAnnotationIds?: string[]
  accepted_prompt_annotation_ids?: string[]
}

/** Server-owned recovery record for one unaccepted manual MetaSkill launch. */
export interface MetaLaunchDraftPayload {
  sessionKey: string
  clientRequestId: string
  name: string
  launchText: string
  createdAt: number
  expiresAt: number
  sessionExists: boolean
}

export interface MetaDraftsListResponse {
  ok: boolean
  durable: boolean
  drafts: MetaLaunchDraftPayload[]
}

export interface MetaDraftDiscardResponse {
  ok: boolean
  discarded: boolean
  accepted?: boolean
}

export interface SessionSteerV2Params {
  key: string
  message: string
  expected_turn_id: string
  client_request_id: string
  client_message_id: string
  pendingInputId?: string
  requestFingerprint?: string
  expectedRevision?: number
  surface_id?: string
  _source?: { elevated?: string; runMode?: 'safe' | 'full' }
}

export interface SessionSteerV2Response {
  accepted?: boolean
  replayed?: boolean
  session_key?: string
  turn_id?: string
  user_message_id?: string
  client_request_id?: string
  client_message_id?: string
  disposition?: import('./chat').ChatSteerDisposition
  revision?: number
  promoted_turn_id?: string
  promoted_from_turn_id?: string
  applied_iteration?: number
  model_call_id?: string
  fallback_safe?: boolean
  failure_code?: string
  retryable?: boolean
  recovery?: string
  reason?: string
}

export interface InputDispositionPayload extends SessionEventPayload {
  target_turn_id?: string
  client_request_id?: string
  client_message_id?: string
  user_message_id?: string
  disposition?: import('./chat').ChatSteerDisposition
  promoted_from_turn_id?: string
  promoted_turn_id?: string
  applied_iteration?: number
  model_call_id?: string
  failure_code?: string
  retryable?: boolean
  recovery?: string
  fallback_safe?: boolean
  revision?: number
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
  attachmentId?: unknown
  attachment_id?: unknown
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
  promptAnnotations?: unknown[]
  prompt_annotations?: unknown[]
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

export interface ChatCompactionSummary {
  id?: string | number | null
  compaction_id?: string | null
  compaction_index?: number | null
  trigger_reason?: string | null
  summary_text?: string
  summary_format?: string
  coverage_status?: string
  removed_count?: number | null
  kept_count?: number | null
  covered_through_id?: number | null
  created_at?: string | number | null
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
  compaction_summaries?: ChatCompactionSummary[]
  compactionSummaries?: ChatCompactionSummary[]
  turn_outcomes?: ChatHistoryTurnOutcome[]
}

export interface ChatHistoryTurnOutcome {
  turn_id?: string
  task_id?: string
  status?: string
  started_at?: string | number
  finished_at?: string | number
  outcome?: Record<string, unknown>
  document_mutation_outcome?: Record<string, unknown>
  documentMutationOutcome?: Record<string, unknown>
  code?: string
  error_class?: string
  retryable?: boolean
  retry_after_ms?: number
  terminal_message?: string
  activity_snapshot?: Record<string, unknown>
  usage_call_index?: number
  no_prior_provider_dispatch?: boolean
  replay_safe?: boolean
  user_message_id?: string
  accepted_routing_mode?: 'direct' | 'router' | 'ensemble'
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
  status?:
    | 'started'
    | 'observed'
    | 'completed'
    | 'skipped'
    | 'stale'
    | 'failed'
    | 'error'
    | 'cancelled'
    | 'timed_out'
    | 'emergency_ephemeral'
    | (string & {})
  compacted?: boolean
  detail?: string
  reason?: string
  skip_reason?: string
  source?: string
  phase?: string
  compaction_id?: string
  compactionId?: string
  sequence?: number
  heartbeat?: boolean
  heartbeat_at?: number
  elapsed_ms?: number
  stage?: string
  refused?: boolean
  safe_to_send?: boolean
  safeToSend?: boolean
  applied?: boolean
  durability?: 'durable' | 'request_scoped' | (string & {})
  user_visible?: boolean
  userVisible?: boolean
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
  'session.event.answer_generation_reset': AnswerGenerationResetPayload
  'session.event.text_delta': TextDeltaPayload
  'session.event.tool_use_start': ToolUsePayload
  'session.event.tool_use_delta': ToolDeltaPayload
  'session.event.tool_use_end': ToolEndPayload
  'session.event.tool_result': ToolResultPayload
  'session.event.artifact': ArtifactPayload
  'session.event.artifact_state': ArtifactStateEventPayload
  'session.event.router_decision': RouterDecisionPayload
  'session.event.ensemble_progress': EnsembleProgressPayload
  'session.event.router_control_replay': SessionEventPayload
  'session.event.state_change': SessionEventPayload
  'session.event.run_heartbeat': SessionEventPayload
  'session.event.provider_activity': ProviderActivityPayload
  'session.event.compaction': CompactionPayload
  'session.event.goal': SessionEventPayload
  'session.event.warning': SessionEventPayload
  'session.event.input_disposition': InputDispositionPayload
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
  'session.event.done': SessionDonePayload
  'session.event.turn_committed': TurnCommittedPayload
}
