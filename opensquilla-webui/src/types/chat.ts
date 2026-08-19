import type { ArtifactPayload } from './rpc'
import type { SessionSteerV2Params } from './rpc'
import type { IconName } from '@/utils/icons'

export interface Attachment {
  kind: 'inline' | 'staged' | 'inline_pending' | 'uploading' | 'failed'
  local_id: number
  name: string
  mime: string
  size?: number
  data?: string
  dataUrl?: string
  file_uuid?: string
  expires_at?: number
  ttl_seconds?: number
  error?: string
  file?: File
  /** Server-owned bytes restored from the durable pending-input queue. */
  durable_material?: true
}

export interface DisplayAttachment {
  kind: 'inline' | 'staged' | 'file'
  displayId: string
  renderKey: string
  name: string
  mime: string
  size?: number
  data?: string
  dataUrl?: string
  /** Base64 bytes retained in memory for downloads; never rendered into the DOM. */
  downloadData?: string
  /** Original optimistic upload retained in memory so a sent file stays downloadable. */
  localFile?: File
  download_url?: string
  sha256_ref?: string
}

/**
 * Local delivery state for a same-turn steer that has not yet been proven
 * durable. A pending attempt is deliberately not a transcript message: only
 * an accepted response, typed disposition event, or matching history row may
 * project it into `ChatMessage`.
 */
export type PendingSteerPhase =
  | 'submitting'
  | 'retryable_rejected'
  | 'acceptance_unknown'

export interface PendingSteerAttempt {
  phase: PendingSteerPhase
  /** Immutable idempotent request replayed byte-for-byte on manual retry. */
  request: Readonly<SessionSteerV2Params>
  errorCode?: string
  retryAfterMs?: number
  /** Stop raced admission; the authoritative disposition still decides. */
  stopRequested?: boolean
}

export interface ChatPendingItem {
  /** Stable local identity for keyed rendering and UI actions across peer edits. */
  pendingUiId: string
  text: string
  attachments: Attachment[]
  intent: string | null
  /** Slash-prefixed text that a complete command catalog classified as ordinary input. */
  confirmedPlainText?: boolean
  /** Generic non-v2 queue/hidden-control delivery lease. V2 Steer uses `steerAttempt`. */
  deliveryState?: 'steering' | 'retryable'
  /** Canonical transport identity/state for a not-yet-durable steer. */
  steerAttempt?: PendingSteerAttempt
  /** Session that owned this item when it entered the in-memory queue. */
  ownerSessionKey?: string
  /** chat.send request whose canonical response may carry this item to a child. */
  ownerRequestId?: string
  // Hidden control sends (e.g. meta-preflight confirmation) carry the provider
  // text in `text`, the visible bubble in `displayTextOverride`, and skip the
  // normal user-bubble push / composer consumption on drain.
  hiddenControl?: boolean
  displayTextOverride?: string
  // Stable ingress identity for a queued hidden control. Provider-setup
  // handoffs reuse it across remounts/tabs so Gateway idempotency can collapse
  // duplicate resumes of the same original intent.
  clientRequestId?: string
  /** Session that owns a durable hidden-control intent. */
  hiddenControlSessionKey?: string
  /** Stable transport identity for retrying a hidden control exactly once. */
  hiddenClientRequestId?: string
  hiddenClientMessageId?: string
  /** The visible confirmation bubble was already rendered optimistically. */
  hiddenVisibleCommitted?: boolean
  /** Stable identity shared by IndexedDB WAL and the Gateway staged queue. */
  pendingInputId?: string
  pendingClientRequestId?: string
  pendingClientMessageId?: string
  pendingRequestFingerprint?: string
  pendingServerRevision?: number
  pendingPosition?: number
  pendingWalRevision?: number
  pendingCreatedAt?: number
  /**
   * The stable identity may already exist in a Gateway even when its enqueue
   * acknowledgement was lost.  Keep this provenance across mixed-version or
   * disconnected periods so a local cancel cannot discard the only durable
   * delete intent.
   */
  pendingMayHaveServerCopy?: boolean
  /** A cancelling transport row must become a local editable draft after tombstoning. */
  pendingRetainAfterCancel?: boolean
  /** Browser/server staging lifecycle. Unknown enqueue results remain `saving`. */
  pendingPersistenceState?:
    | 'saving'
    | 'staged'
    | 'local_only'
    | 'retryable'
    | 'cancelling'
}

export type HiddenControlDispatchStatus =
  | 'accepted'
  | 'queued'
  | 'rejected'
  | 'unknown'

export type HiddenControlDispatchReason =
  | 'accepted'
  | 'queued'
  | 'already_queued'
  | 'queue_full'
  | 'discarded'
  | 'invalid_request'
  | 'outbox_conflict'
  | 'outbox_persist_failed'
  | 'send_rejected'
  | 'response_unknown'

/**
 * Machine-owned result for a hidden control send. `accepted` is the only state
 * that proves the Gateway durably owns the request; `queued` is recoverable
 * local work and must keep its persisted source intent until a later accepted
 * result arrives.
 */
export interface HiddenControlDispatchResult {
  status: HiddenControlDispatchStatus
  reason: HiddenControlDispatchReason
  clientRequestId: string
  sessionKey: string
}

export interface ChatRouterCell {
  kind: 'real' | 'decoy'
  tier: string
  tiers: string[]
  displayName: string
  model?: string
  executionKind?: 'single_model' | 'ensemble'
}

export interface ChatRouterTierConfig {
  model: string
  supportsImage: boolean
  imageOnly: boolean
  ensembleEnabled?: boolean
}

export interface ChatToolCall {
  toolId: string
  name: string
  displayName: string
  groupId?: string
  inputRaw?: string
  inputPreview: string
  isRunning: boolean
  status: '' | 'success' | 'error'
  isError: boolean
  result: string
  resultPreview: string
  sources?: unknown
  isOpen: boolean
}

export type ChatToolCallRenderItem = ChatToolCall & {
  renderKey: string
}

// Context travels with an expanded tool payload so the full-result viewer can
// describe the content without trying to reverse-engineer it from its title.
// `inputRaw` is already part of the rendered tool trace; the viewer only uses
// it to extract safe display metadata such as a read_file path.
export interface ToolResultContext {
  toolName?: string
  inputRaw?: string
  section?: 'input' | 'result' | 'error'
}

export interface ChatToolCallGroup {
  groupId: string
  operationKey: string
  label: string
  iconName: IconName
  calls: ChatToolCallRenderItem[]
  secondary: string
  isRunning: boolean
  isError: boolean
  status: '' | 'success' | 'error'
}

export type ChatTextPresentation = 'intermediate' | 'answer'

export interface ChatStreamSegment {
  type: 'text' | 'tool-group' | 'interrupt'
  raw?: string
  html?: string
  dirty?: boolean
  presentation?: ChatTextPresentation
  groupId?: string
  operationKey?: string
  approvalId?: string
}

export type ChatStreamTimelineItem =
  | {
      type: 'text'
      key: string
      html: string
      rawText?: string
      presentation?: ChatTextPresentation
    }
  | { type: 'tool-group'; key: string; group: ChatToolCallGroup }
  | {
      type: 'interrupt'
      key: string
      approvalId: string
      part: Extract<import('./parts').ChatPart, { type: 'interrupt' }>
    }

export type ChatRole = 'user' | 'assistant' | 'system' | 'error' | 'router' | string

export type ChatRunStatusState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'approval_pending'
  | 'interrupted'
  | 'failed'
  | 'timeout'
  | 'cancelled'

export type ChatSteerDisposition =
  | 'steering'
  | 'applied'
  | 'promoted'
  | 'cancelled'
  | 'rejected'

export interface ChatSteerCapability {
  mode: 'same_turn' | 'queue_only' | 'disabled'
  expected_turn_id?: string
  input_kinds?: string[]
  reason?: string
}

export interface ChatTurnOutcome {
  turnId: string
  taskId?: string
  status: string
  kind?: string
  reason?: string
  cancellationSource?: string
  startedAt?: number | string
  finishedAt?: number | string
  retryable?: boolean
  errorClass?: string
  terminalMessage?: string
  retryAfterMs?: number
  statusHistory?: import('./parts').StatusPart[]
  usageCallIndex?: number
  noPriorProviderDispatch?: boolean
  replaySafe?: boolean
  userMessageId?: string
  acceptedRoutingMode?: 'direct' | 'router' | 'ensemble'
}

export interface ChatRunTask {
  status?: string
  cancel_requested?: boolean
  cancelRequested?: boolean
  task_id?: string
  taskId?: string
  started_at?: number | string
  startedAt?: number | string
  finished_at?: number | string
  finishedAt?: number | string
  terminal_reason?: string
  terminalReason?: string
  task_group_count?: number
  taskGroupCount?: number
  turn_id?: string
  turnId?: string
  steer_capability?: ChatSteerCapability
  steerCapability?: ChatSteerCapability
  turn_outcome?: Record<string, unknown>
  turnOutcome?: Record<string, unknown>
}

export interface ChatRunStatus {
  status: ChatRunStatusState
  label: string
  task: ChatRunTask | null
}

export interface ChatRunStatusSource {
  run_status?: string
  runStatus?: string
  active_task?: ChatRunTask | null
  activeTask?: ChatRunTask | null
  last_task?: ChatRunTask | null
  lastTask?: ChatRunTask | null
}

export interface RawToolCallPayload extends Record<string, unknown> {
  type?: string
  id?: string
  toolId?: string
  tool_use_id?: string
  name?: string
  tool_name?: string
  input?: unknown
  result?: unknown
  user_input_request?: unknown
  content?: unknown
  output?: unknown
  sources?: unknown
  is_error?: boolean
  isError?: boolean
  error?: unknown
  execution_status?: { status?: string }
  groupId?: string
  group_id?: string
  presentation?: ChatTextPresentation
}

export interface ChatTimelineSegment extends Record<string, unknown> {
  type?: string
  raw?: string
  text?: string
  presentation?: ChatTextPresentation
  groupId?: string
  group_id?: string
  approvalId?: string
  approval_id?: string
}

export interface ChatModelCallSegment {
  model_call_id?: string
  modelCallId?: string
  iteration?: number
  start_codepoint?: number
  startCodepoint?: number
  end_codepoint?: number
  endCodepoint?: number
}

export interface ChatUsagePayload {
  model?: string
  routed_model?: string
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  cached_tokens?: number
  reasoning_tokens?: number
  cost_usd?: number
  costUsd?: number
  routed_tier?: string
  routing_source?: string
  total_savings_pct?: number
  totalSavingsPct?: number
  total_savings_usd?: number
  totalSavingsUsd?: number
  savings_usd?: number
  savingsUsd?: number
  savings_pct?: number
  savingsPct?: number
  model_usage_breakdown?: ChatEnsembleUsageRow[]
  modelUsageBreakdown?: ChatEnsembleUsageRow[]
  ensemble_trace?: ChatEnsembleTrace
  ensembleTrace?: ChatEnsembleTrace
  route_plan?: Record<string, unknown>
  routePlan?: Record<string, unknown>
  model_call_segments?: ChatModelCallSegment[]
  modelCallSegments?: ChatModelCallSegment[]
  /** Physical provider call whose visible output owns the route card. */
  router_model_call_id?: string
  routerModelCallId?: string
  router_iteration?: number
  routerIteration?: number
  /** Per-turn ledger coverage. Older gateways omit these additive fields. */
  coverage_status?: string
  coverageStatus?: string
  usage_unknown?: boolean
  usageUnknown?: boolean
  unknown_usage_events?: number
  unknownUsageEvents?: number
  /** V017 routing-decision id — presence is what makes a turn rateable. */
  decision_id?: string
  __savings_ui_suppressed?: boolean
  [key: string]: unknown
}

export interface ChatEnsembleUsageRow {
  role?: string
  profile?: string
  label?: string
  provider?: string
  model?: string
  sample_index?: number
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  reasoning_tokens?: number
  reasoningTokens?: number
  cached_tokens?: number
  cachedTokens?: number
  cache_write_tokens?: number
  cacheWriteTokens?: number
  billed_cost?: number
  billedCost?: number
  cost_usd?: number
  costUsd?: number
  cost_source?: string
  costSource?: string
  elapsed_ms?: number
  elapsedMs?: number
  ok?: boolean
  error?: string
  error_code?: string
  errorCode?: string
  [key: string]: unknown
}

export interface ChatEnsembleTrace {
  mode?: string
  profile?: string
  successful_proposers?: number
  total_candidates?: number
  fallback_used?: boolean
  fallback_reason?: string
  final_request_role?: string
  llm_request_count?: number
  candidates?: ChatEnsembleUsageRow[]
  [key: string]: unknown
}

export interface ChatEnsembleMetaModel {
  role: string
  label: string
  provider: string
  model: string
  modelShort: string
  input: number
  output: number
  costUsd: number
  sampleIndex?: number
  // Per-member lifecycle from live progress or a settled ensemble trace.
  status?: 'running' | 'done' | 'failed' | 'skipped'
  elapsedMs?: number
  error?: string
  errorCode?: string
}

export interface ChatEnsembleMeta {
  profile: string
  modelCount: number
  totalCandidates: number
  requestCount: number
  fallbackUsed: boolean
  fallbackReason: string
  costUsd: number
  savedUsd: number
  savedPct: number
  models: ChatEnsembleMetaModel[]
}

/** Per-turn model reasoning captured from thinking deltas / done backfill. */
export interface ChatReasoning {
  text: string
  seconds: number
}

/** A non-conversational maintenance event rendered inside transcript chronology. */
export interface ChatMaintenanceEvent {
  kind: 'context_compaction'
  compactionId: string
  source: string
  state: 'running' | 'completed' | 'skipped' | 'stale' | 'cancelled' | 'failed'
  durability: string
  detail?: string
  reason?: string
  removedCount?: number
  keptCount?: number
  /** This event marks a durable summary/archive boundary in canonical history. */
  historyArchived?: boolean
  /** Whether every original row remains available across that boundary. */
  canonicalComplete?: boolean | null
}

export interface ChatMessage {
  role: ChatRole
  text: string
  ts: string | number | null
  /** Stable client-only identity for optimistic rows before the backend assigns messageId. */
  clientId?: string
  reasoning?: ChatReasoning
  /** Structured physical-call reasoning retained across live-to-history sync. */
  reasoningBlocks?: import('./turnlog').ReasoningBlock[]
  /** Ephemeral handoff when a coarse live burst still needs visual reveal. */
  reasoningPresentationPending?: boolean
  routerDecision?: import('./rpc').RouterDecisionPayload | null
  /** Routing-only usage projection for a split historical answer segment. */
  routerUsage?: ChatUsagePayload
  routerModelCallId?: string
  routerIteration?: number
  artifacts?: ArtifactPayload[]
  tool_calls?: RawToolCallPayload[]
  planRevisions?: import('./plans').PlanRevisionSnapshot[]
  timeline?: ChatTimelineSegment[]
  attachments?: DisplayAttachment[]
  provenanceKind?: string
  provenanceSourceSessionKey?: string
  provenanceSourceTool?: string
  /** Durable causal turn identity restored from transcript turn_context. */
  turnId?: string
  /** Internal-input provenance used by presentation-only compatibility rules. */
  turnInputMode?: string
  /** Runtime turn kind used by presentation-only compatibility rules. */
  turnRunKind?: string
  /** Same-turn input lifecycle, sourced only from durable context or typed events. */
  inputDisposition?: ChatSteerDisposition
  /** Monotonic server revision for the disposition state machine. */
  inputDispositionRevision?: number
  steerClientRequestId?: string
  steerClientMessageId?: string
  /** Physical model call that durably applied this same-turn adjustment. */
  steerModelCallId?: string
  steerAppliedIteration?: number
  steerRestored?: boolean
  /** Local Stop was requested; the server disposition remains authoritative. */
  steerStopRequested?: boolean
  /** Original turn when this accepted adjustment was promoted into a follow-up. */
  promotedFromTurnId?: string
  turnOutcome?: ChatTurnOutcome
  interrupted?: boolean
  routerState?: string
  routerSettled?: boolean
  // Live-accumulated ensemble members for the in-flight router strip, grown by
  // `session.event.ensemble_progress` deltas before the final `done` arrives.
  ensemble?: ChatEnsembleMeta
  messageId?: string
  usage?: ChatUsagePayload
  turn_usage?: ChatUsagePayload
  model?: string
  input?: number
  input_tokens?: number
  output?: number
  output_tokens?: number
  restoredFromHistory?: boolean
  /** Durable transcript maintenance restored from chat.history metadata. */
  maintenance?: ChatMaintenanceEvent
  statusHistory?: import('./parts').StatusPart[]
  /** Live approval/clarify snapshots referenced by interrupt timeline segments. */
  interrupts?: Extract<import('./parts').ChatPart, { type: 'interrupt' }>[]
  stopNotice?: boolean
  /** Client terminal error retained until history contains a durable error row. */
  terminalNotice?: boolean
  /** Typed terminal error code (e.g. 'sandbox_threshold_exceeded') carried on
   *  role:'error' messages so the renderer can offer a recovery action. */
  errorCode?: string
}

export interface ChatMessageMeta {
  model: string
  modelShort: string
  input: number
  output: number
  hasTokens: boolean
  cachedTokens: number
  reasoningTokens: number
  costUsd: number
  hasSaved: boolean
  savedLabel: string
  turnSavedPct?: number
  ensemble?: ChatEnsembleMeta
  /** Normalized additive coverage metadata from the per-turn usage receipt. */
  coverageStatus?: string
  usageUnknown?: boolean
  unknownUsageEvents?: number
  /** True when at least one measured token/cost fact contributes to the receipt. */
  hasKnownUsage?: boolean
  /** Routing-decision id from turn usage; thumbs render only when present. */
  decisionId?: string
}

export interface ChatCreatedSessionLink {
  callId: string
  sessionKey: string
}

export interface ChatRenderedMessage {
  id?: string
  clientId?: string
  sourceIndex?: number
  role: string
  displayRole: string
  roleLabel: string
  text: string
  timeStr: string
  /** Raw message timestamp (epoch ms or ISO string) so components can derive a
   *  live relative + absolute label without re-running the renderedMessages map. */
  ts?: string | number | null
  showHeader: boolean
  isStreaming?: boolean
  messageId?: string
  restoredFromHistory?: boolean
  /** Stable identity of the owning user turn for client-only UI continuity. */
  turnKey?: string
  /** Durable server turn identity restored from transcript turn_context. */
  turnId?: string
  /** Internal-input provenance copied from the source ChatMessage. */
  turnInputMode?: string
  /** Runtime turn kind copied from the source ChatMessage. */
  turnRunKind?: string
  inputDisposition?: ChatSteerDisposition
  maintenance?: ChatMaintenanceEvent
  inputDispositionRevision?: number
  turnOutcome?: ChatTurnOutcome
  hasAttachments?: boolean
  attachments?: DisplayAttachment[]
  /** Explicit placement for successful sessions_spawn cards. An empty array
   *  suppresses the source card after it is rehomed below the parent reply. */
  createdSessionLinks?: ChatCreatedSessionLink[]
  toolCalls?: ChatToolCall[]
  planRevisions?: import('./plans').PlanRevisionSnapshot[]
  timelineItems?: ChatStreamTimelineItem[]
  artifacts?: ArtifactPayload[]
  meta?: ChatMessageMeta
  reasoning?: ChatReasoning
  /** Structured physical-call reasoning retained across live-to-history sync. */
  reasoningBlocks?: import('./turnlog').ReasoningBlock[]
  /** Ephemeral handoff when a coarse live burst still needs visual reveal. */
  reasoningPresentationPending?: boolean
  interrupted?: boolean
  /** The turn ended with a terminal error after this partial assistant output. */
  terminalFailure?: boolean
  provenanceKind?: string
  provenanceSourceSessionKey?: string
  provenanceSourceTool?: string
  daySeparator?: boolean
  dayLabel?: string
  isRouterStrip?: boolean
  /** Stable per-card render identity; preserved during terminal reconciliation. */
  routerTurnKey?: string
  routerModelCallId?: string
  routerIteration?: number
  routerState?: string
  routerSource?: string
  routerObserve?: boolean
  routerStatic?: boolean
  routerSettled?: boolean
  routerPanel?: string
  routerMode?: import('./modelRouting').ModelRoutingMode
  ensemble?: ChatEnsembleMeta
  gridCells?: ChatRouterCell[]
  winnerIdx?: number
  parts?: import('./parts').ChatPart[]
  sources?: import('./parts').SourcePart[]
  statusHistory?: import('./parts').StatusPart[]
  stopNotice?: boolean
  /** Typed terminal error code, propagated from the raw message so the error
   *  card can render a recovery action (e.g. resume after a sandbox pause). */
  errorCode?: string
}
