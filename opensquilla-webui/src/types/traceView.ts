/** Canonical phase bands used by the web trace inspector. */
export type TracePhaseName =
  | 'intake'
  | 'context'
  | 'routing'
  | 'model_execution'
  | 'tool_execution'
  | 'approval_sandbox'
  | 'compaction_maintenance'
  | 'subagent'
  | 'finalize'
  | 'unknown'

export type TraceSpanStatus = 'queued' | 'running' | 'success' | 'error' | 'cancelled' | 'skipped' | 'unknown'

export interface TraceSpanLink {
  type: 'parent' | 'retry' | 'fallback' | 'candidate' | 'continuation' | 'related'
  spanId: string
  label?: string
}

export interface TraceSpan {
  id: string
  parentId?: string | null
  kind: string
  phase: TracePhaseName
  title: string
  role?: string
  provider?: string
  model?: string
  status: TraceSpanStatus
  startedAt?: number
  endedAt?: number
  durationMs?: number
  recordedAt?: number
  elapsedMs?: number
  startedElapsedMs?: number
  endedElapsedMs?: number
  seq?: number
  attemptIndex?: number
  logicalCallId?: string
  physicalAttemptId?: string
  links?: TraceSpanLink[]
  summary?: string
  attrs?: Record<string, unknown>
  input?: unknown
  output?: unknown
  inputSeq?: number
  inputChars?: number
  outputChars?: number
  inputTruncated?: boolean
  outputTruncated?: boolean
  payloadRef?: string
  usage?: Record<string, unknown>
  toolName?: string
}

export interface TracePhase {
  name: TracePhaseName
  label: string
  status: TraceSpanStatus
  spans: TraceSpan[]
  durationMs?: number
}

export interface TraceProjection {
  traceId: string
  runId?: string
  turnId?: string
  status: TraceSpanStatus
  requestedMode?: string
  effectiveMode?: string
  phases: TracePhase[]
  spans: TraceSpan[]
  currentSeq?: number
  complete: boolean
}

export interface TraceDetails {
  traceId: string
  available: boolean
  reason?: string
  rows: TraceSpan[]
  count: number
  total: number
  hasMore?: boolean
  clockOrigin?: 'turn_runner_start' | 'logger_start'
}
