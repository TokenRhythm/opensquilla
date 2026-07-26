import type { ArtifactPayload } from './rpc'
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

export interface ChatPendingItem {
  text: string
  attachments: Attachment[]
  intent: string | null
  /** Session that owned this item when it entered the in-memory queue. */
  ownerSessionKey?: string
  /** chat.send request whose canonical response may carry this item to a child. */
  ownerRequestId?: string
  // Hidden control sends (e.g. meta-preflight confirmation) carry the provider
  // text in `text`, the visible bubble in `displayTextOverride`, and skip the
  // normal user-bubble push / composer consumption on drain.
  hiddenControl?: boolean
  displayTextOverride?: string
}

export interface ChatRouterCell {
  kind: 'real' | 'decoy'
  tier: string
  tiers: string[]
  displayName: string
  model?: string
}

export interface ChatRouterTierConfig {
  model: string
  supportsImage: boolean
  imageOnly: boolean
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

export interface ChatStreamSegment {
  type: 'text' | 'tool-group'
  raw?: string
  html?: string
  dirty?: boolean
  groupId?: string
  operationKey?: string
}

export type ChatStreamTimelineItem =
  | { type: 'text'; key: string; html: string; rawText?: string }
  | { type: 'tool-group'; key: string; group: ChatToolCallGroup }

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

export interface ChatRunTask {
  status?: string
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
  content?: unknown
  output?: unknown
  sources?: unknown
  is_error?: boolean
  isError?: boolean
  error?: unknown
  execution_status?: { status?: string }
  groupId?: string
  group_id?: string
}

export interface ChatTimelineSegment extends Record<string, unknown> {
  type?: string
  raw?: string
  text?: string
  groupId?: string
  group_id?: string
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

export interface ChatMessage {
  role: ChatRole
  text: string
  ts: string | number | null
  /** Stable client-only identity for optimistic rows before the backend assigns messageId. */
  clientId?: string
  reasoning?: ChatReasoning
  routerDecision?: import('./rpc').RouterDecisionPayload | null
  artifacts?: ArtifactPayload[]
  tool_calls?: RawToolCallPayload[]
  timeline?: ChatTimelineSegment[]
  attachments?: DisplayAttachment[]
  provenanceKind?: string
  provenanceSourceSessionKey?: string
  provenanceSourceTool?: string
  /** Durable causal turn identity restored from transcript turn_context. */
  turnId?: string
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
  statusHistory?: import('./parts').StatusPart[]
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
  /** Routing-decision id from turn usage; thumbs render only when present. */
  decisionId?: string
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
  /** Stable identity of the owning user turn for client-only UI continuity. */
  turnKey?: string
  hasAttachments?: boolean
  attachments?: DisplayAttachment[]
  toolCalls?: ChatToolCall[]
  timelineItems?: ChatStreamTimelineItem[]
  artifacts?: ArtifactPayload[]
  meta?: ChatMessageMeta
  reasoning?: ChatReasoning
  interrupted?: boolean
  /** The turn ended with a terminal error after this partial assistant output. */
  terminalFailure?: boolean
  provenanceKind?: string
  provenanceSourceSessionKey?: string
  provenanceSourceTool?: string
  daySeparator?: boolean
  dayLabel?: string
  isRouterStrip?: boolean
  /** Stable per-turn render identity. Unlike the router event message id, this
   *  does not change when a live strip is replaced by its settled trace. */
  routerTurnKey?: string
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
