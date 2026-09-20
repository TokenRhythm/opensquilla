import type { ChatToolCallRenderItem } from '@/types/chat'
import { redactActivityDetail } from './activityToolDetails'

export type ExecutionIoMode = 'pty' | 'pipe'

export interface ExecutionIoEntry {
  executionId: string
  requested?: ExecutionIoMode
  used?: ExecutionIoMode
  fallbackReason?: string
}

export type ExecutionIoSummary =
  | { kind: 'pty'; entries: ExecutionIoEntry[] }
  | { kind: 'pipe'; entries: ExecutionIoEntry[] }
  | { kind: 'fallback'; entries: ExecutionIoEntry[]; fallbackReason?: string }
  | { kind: 'mixed'; entries: ExecutionIoEntry[] }
  | { kind: 'unknown'; entries: ExecutionIoEntry[] }

const MAX_FALLBACK_REASON_LENGTH = 240

function sanitizeFallbackReason(value: unknown): string | undefined {
  const text = stringValue(value)
  if (!text) return undefined
  const singleLine = redactActivityDetail(text)
    .replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!singleLine) return undefined
  // A backend diagnostic can contain a local path or a shell fragment. Keep
  // the useful capability detail while avoiding raw multiline/path disclosure
  // in the compact WebUI trace.
  const redacted = singleLine
    .replace(/(?:\/Users|\/home|\/private|[A-Za-z]:\\)[^\s,)]+/g, '<path>')
  return redacted.length > MAX_FALLBACK_REASON_LENGTH
    ? `${redacted.slice(0, MAX_FALLBACK_REASON_LENGTH - 1)}…`
    : redacted
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function mode(value: unknown): ExecutionIoMode | undefined {
  return value === 'pty' || value === 'pipe' ? value : undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function addRecord(record: Record<string, unknown>, out: ExecutionIoEntry[]): void {
  const session = asRecord(record.session)
  const executionId = stringValue(record.execution_id)
    || stringValue(record.session_id)
    || stringValue(session?.execution_id)
    || stringValue(session?.session_id)
  const used = mode(record.io_mode_used) || mode(session?.io_mode_used)
  const requested = mode(record.io_mode_requested) || mode(session?.io_mode_requested)
  const fallbackReason = sanitizeFallbackReason(
    record.fallback_reason || session?.fallback_reason,
  )
  if (executionId) {
    out.push({ executionId, requested, used, fallbackReason })
  }
  if (session) addRecord(session, out)
  const sessions = Array.isArray(record.sessions) ? record.sessions : []
  for (const item of sessions) {
    const child = asRecord(item)
    if (child) addRecord(child, out)
  }
}

function parse(raw: unknown): Record<string, unknown> | null {
  if (typeof raw === 'string') {
    try {
      return asRecord(JSON.parse(raw))
    } catch {
      return null
    }
  }
  return asRecord(raw)
}

export function projectExecutionIo(raw: unknown): ExecutionIoSummary {
  const root = parse(raw)
  if (!root) return { kind: 'unknown', entries: [] }
  const collected: ExecutionIoEntry[] = []
  addRecord(root, collected)
  const byId = new Map<string, ExecutionIoEntry>()
  for (const entry of collected) {
    const previous = byId.get(entry.executionId)
    byId.set(entry.executionId, previous
      ? {
          executionId: entry.executionId,
          requested: entry.requested || previous.requested,
          used: entry.used || previous.used,
          fallbackReason: entry.fallbackReason || previous.fallbackReason,
        }
      : entry)
  }
  const entries = Array.from(byId.values())
  if (!entries.length) return { kind: 'unknown', entries: [] }
  // A fallback must remain visible even when the same group also contains
  // successful PTYs. This warning describes the affected entries, never
  // promotes the whole group to a successful TTY.
  const fallbacks = entries.filter(entry => entry.requested === 'pty' && entry.used === 'pipe')
  if (fallbacks.length) {
    return { kind: 'fallback', entries, fallbackReason: fallbacks.find(entry => entry.fallbackReason)?.fallbackReason }
  }
  const modes = new Set(entries.map(entry => entry.used).filter(Boolean))
  const hasUnknown = entries.some(entry => !entry.used)
  if (modes.size > 1 || (hasUnknown && modes.size > 0)) return { kind: 'mixed', entries }
  const only = entries[0]
  if (only.used === 'pty') return { kind: 'pty', entries }
  if (only.used === 'pipe') return { kind: 'pipe', entries }
  return { kind: 'unknown', entries }
}

export function projectExecutionIoForCall(call: Pick<ChatToolCallRenderItem, 'name' | 'result' | 'resultPreview'>): ExecutionIoSummary {
  if (!isManagedExecutionTool(call)) return { kind: 'unknown', entries: [] }
  return projectExecutionIo(call.result || call.resultPreview)
}

function isManagedExecutionTool(call: Pick<ChatToolCallRenderItem, 'name'>): boolean {
  return call.name === 'exec_command' || call.name === 'process' || call.name === 'background_process'
}

export function mergeExecutionIo(summaries: ExecutionIoSummary[]): ExecutionIoSummary {
  const entries = summaries.flatMap(summary => summary.entries)
  return projectExecutionIo(JSON.stringify({ sessions: entries.map(entry => ({
    execution_id: entry.executionId,
    io_mode_requested: entry.requested,
    io_mode_used: entry.used,
    fallback_reason: entry.fallbackReason,
  })) }))
}
