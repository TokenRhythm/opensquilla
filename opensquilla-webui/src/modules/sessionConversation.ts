import type { InjectionKey } from 'vue'
import type {
  ChatHistoryResponse,
  SessionEventPayload,
} from '@/types/chat'
import type { ConversationSemanticEventKind } from './conversationEvents'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'

export interface SessionMessagesSubscribeParams {
  key: string
  since_stream_generation?: string
  since_stream_seq?: number
  [key: string]: unknown
}

export interface SessionLiveSnapshotEvent {
  event: ConversationSemanticEventKind
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
  mode?: import('@/types/modelRouting').GatewayModelRoutingMode
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
  collaboration?: import('@/types/plans').CollaborationSnapshot
  currentPlan?: import('@/types/plans').PlanRevisionSnapshot | null
  current_plan?: unknown
  activePlanRun?: import('@/types/plans').PlanRunSnapshot | null
  active_plan_run?: unknown
  goal?: unknown
  goalSnapshotStreamSeq?: number | null
  goal_snapshot_stream_seq?: number | null
  pendingUserInputs?: unknown[]
  pending_user_inputs?: unknown[]
}

/** Application request policy; the Adapter maps it to v4 transport options. */
export interface SessionConversationRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
  timeoutAction?: 'reject' | 'reconnect'
  abortAction?: 'reject' | 'reconnect'
  expectedGeneration?: number
  onSent?: (transportGeneration: number) => void
}

export interface SessionHistoryRequest {
  sessionKey: string
  limit?: number
  before?: string | number | null
  after?: string | number | null
  includeCanonical?: boolean
  includeSummaries?: boolean
}

export interface SessionPreview {
  key?: string
  title?: string
  lastMessage?: string
  updatedAt?: number
  updated_at?: number
  [key: string]: unknown
}

export interface SessionPreviewResult {
  previews?: SessionPreview[]
  ts?: number
}

export interface SessionAbortResult {
  aborted?: boolean
  key?: string
  status?: string
  [key: string]: unknown
}

export interface SessionForkRequest {
  key: string
  beforeMessageId?: string
  throughTurnId?: string
}

export interface SessionForkResult {
  key: string
  parentKey?: string
  forkMode?: string
  throughTurnId?: string
  [key: string]: unknown
}

export interface SessionCompactResult {
  status?: string
  compactionId?: string
  compaction_id?: string
  [key: string]: unknown
}

export interface UsageStatusSession {
  session?: string
  sessionKey?: string
  key?: string
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  cache_read_tokens?: number
  cacheReadTokens?: number
  cache_write_tokens?: number
  cacheWriteTokens?: number
  cost_usd?: number
  costUsd?: number
  model?: string
  contextStatus?: Record<string, unknown> | null
  context_status?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface UsageStatusResult {
  sessions?: UsageStatusSession[]
  totals?: { tokens?: number; [key: string]: unknown }
  totalTokens?: number
  total_tokens?: number
  [key: string]: unknown
}

export interface SlashCommandCatalogItem {
  name?: string
  cmd?: string
  label?: string
  description?: string
  desc?: string
  aliases?: unknown
  execution?: { action?: string }
  [key: string]: unknown
}

export interface SlashCommandCatalogResult {
  commands?: SlashCommandCatalogItem[]
  surface?: string
}

export type RouteFeedbackRating = 'up' | 'down' | 'neutral'
export interface RouteFeedbackResult {
  accepted?: boolean
  recorded?: string
  reason?: string
}

export interface PromptCacheKeepaliveUpdate {
  key: string
  enabled: boolean
  ttlSeconds: number
  idleTimeoutSeconds: number
}

export type SessionConversationCapability =
  | 'messages'
  | 'history'
  | 'preview'
  | 'abort'
  | 'fork'
  | 'reset'
  | 'compact'
  | 'usage'
  | 'slash-catalog'
  | 'route-feedback'
  | 'prompt-cache-keepalive'
  | 'turn-committed'

export interface SessionConversationSubscription {
  close(): void
}

/**
 * Application-facing conversation seam. Wire method names, aliases and
 * connection state are owned by the Gateway Adapter. Existing conversation
 * runtime/cursor modules remain responsible for ordering and replay policy.
 */
export interface SessionConversation {
  ready(options?: SessionConversationRequestOptions): Promise<void>
  subscribe(
    params: SessionMessagesSubscribeParams,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionMessagesSubscribeResponse>
  hydrate(
    key: string,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionMessagesSubscribeResponse>
  snapshot(
    key: string,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionMessagesSnapshotResponse>
  unsubscribe(key: string, options?: SessionConversationRequestOptions): Promise<void>
  history(
    request: SessionHistoryRequest,
    options?: SessionConversationRequestOptions,
  ): Promise<ChatHistoryResponse>
  preview(
    keys: readonly string[],
    options?: SessionConversationRequestOptions,
  ): Promise<SessionPreviewResult>
  abort(
    key: string,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionAbortResult>
  fork(
    request: SessionForkRequest,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionForkResult>
  reset(key: string, options?: SessionConversationRequestOptions): Promise<Record<string, unknown>>
  compact(
    key: string,
    wait?: boolean,
    options?: SessionConversationRequestOptions,
  ): Promise<SessionCompactResult>
  usage(
    sessionKey?: string,
    options?: SessionConversationRequestOptions,
  ): Promise<UsageStatusResult>
  listCommands(
    surface: string,
    options?: SessionConversationRequestOptions,
  ): Promise<SlashCommandCatalogResult>
  submitRouteFeedback(
    decisionId: string,
    rating: RouteFeedbackRating,
    options?: SessionConversationRequestOptions,
  ): Promise<RouteFeedbackResult>
  promptCacheStatus(
    key: string,
    options?: SessionConversationRequestOptions,
  ): Promise<PromptCacheKeepaliveStatus>
  setPromptCacheStatus(
    update: PromptCacheKeepaliveUpdate,
    options?: SessionConversationRequestOptions,
  ): Promise<PromptCacheKeepaliveStatus>
  submitClarify(
    params: Record<string, unknown>,
    options?: SessionConversationRequestOptions,
  ): Promise<Record<string, unknown>>
  subscribeToolResults(listener: (...args: unknown[]) => void): SessionConversationSubscription
  subscribeRoutingChanged(listener: (snapshot: unknown) => void): SessionConversationSubscription
  supports(capability: SessionConversationCapability): boolean
}

export const SESSION_CONVERSATION_KEY: InjectionKey<SessionConversation> =
  Symbol('SessionConversation')

// Keep the imported snapshot type available to consumers that currently use
// it as a callback payload while the legacy rpc type aliases are retired.
export type SessionWorkspaceSnapshot = SessionProjectWorkspaceSnapshot
