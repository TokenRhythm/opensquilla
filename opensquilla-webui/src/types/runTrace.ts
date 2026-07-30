import type { ToolPartState } from '@/types/parts'

/**
 * One flat step in a run trace, as a backend would emit it. The frontend
 * composes parent/child trees from `parentId` (a null/undefined parentId is a
 * top-level step). `id` doubles as the row toggle key (== ChatToolCall.renderKey
 * on the chat path) so the open-state predicates address steps uniformly.
 */
export interface NodeStep {
  id: string
  parentId?: string | null
  title: string                 // displayName
  toolName?: string             // original tool identity; survives flat history traces
  operationKey: string          // e.g. 'web.search' — drives icon + grouping
  state: ToolPartState          // single status enum (see runTrace.ts map)
  tokens?: number | null        // per-step token cost when known
  elapsedMs?: number | null     // per-step duration when known
  input?: string                // raw input (full), for the INPUT panel
  inputPreview?: string         // truncated preview
  output?: string               // raw output (full), for the OUTPUT panel
  outputPreview?: string        // truncated preview
  isError?: boolean
}

export type RunTraceStatus =
  | 'idle' | 'queued' | 'running' | 'success' | 'error' | 'cancelled'

/**
 * summary strip: Status / Executor / Time / Tokens / Steps. Any field may be
 * undefined → renders as the em-dash placeholder. While `loading` is true the
 * cells show skeleton shimmers.
 */
export interface RunTraceSummary {
  status?: RunTraceStatus
  executor?: string             // agent / model name
  elapsedMs?: number | null
  tokens?: number | null
  steps?: number | null
  loading?: boolean
}
