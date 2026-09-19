import type { ChatRenderedMessage, ChatStreamTimelineItem, ChatToolCall } from '@/types/chat'
import {
  normalizeSessionReferenceV1,
  type SessionReferenceV1,
} from '@/types/references'

function resultRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed as Record<string, unknown>
        : null
    } catch {
      return null
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function isSessionSearchCall(call: Pick<ChatToolCall, 'name'>): boolean {
  return call.name === 'session_search' || call.name === ['sessions', 'search'].join('.')
}

function referencesFromList(value: unknown): SessionReferenceV1[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    const row = item && typeof item === 'object' && !Array.isArray(item)
      ? item as Record<string, unknown>
      : null
    // Compatibility is limited to the known tool's structured fields. Never
    // infer identity from snippets, Markdown, arbitrary URLs, or prose.
    const rawKey = row?.session_key ?? row?.key
    const legacy = row && !('reference' in row) && typeof rawKey === 'string' && rawKey.trim()
      ? {
          version: 1,
          kind: 'session',
          id: rawKey,
          label: typeof row.title === 'string' ? row.title : rawKey,
          scope: { sessionKey: rawKey },
          state: { available: true, runStatus: row.runStatus ?? row.run_status ?? null },
          capabilities: { open: true, copy: true },
        }
      : null
    const reference = normalizeSessionReferenceV1(row?.reference ?? legacy)
    return reference ? [reference] : []
  })
}

/**
 * Extract session references from the structured result of the session search
 * tools. Plain text is intentionally ignored; references are only rendered
 * when the Gateway supplied a versioned object or a known legacy result row.
 */
export function sessionReferencesFromToolCall(
  call: Pick<ChatToolCall, 'name' | 'isRunning' | 'isError' | 'status' | 'result'>,
): SessionReferenceV1[] {
  if (!isSessionSearchCall(call) || call.isRunning || call.isError || call.status !== 'success') {
    return []
  }
  const result = resultRecord(call.result)
  if (!result) return []
  const references = [
    ...referencesFromList(result.results),
    ...referencesFromList(result.sessions),
    ...referencesFromList(result.messages),
  ]
  const seen = new Set<string>()
  return references.filter(reference => {
    if (seen.has(reference.id)) return false
    seen.add(reference.id)
    return true
  }).slice(0, 20)
}

export function sessionReferencesFromCalls(calls: ChatToolCall[]): SessionReferenceV1[] {
  const seen = new Set<string>()
  return calls.flatMap(call => sessionReferencesFromToolCall(call)).filter(reference => {
    if (seen.has(reference.id)) return false
    seen.add(reference.id)
    return true
  })
}

export interface SessionReferenceLink {
  callId: string
  reference: SessionReferenceV1
}

function timelineCalls(items: ChatStreamTimelineItem[] | undefined): ChatToolCall[] {
  return (items ?? []).flatMap(item => item.type === 'tool-group' ? item.group.calls : [])
}

export function sessionReferencesFromMessage(message: ChatRenderedMessage): SessionReferenceLink[] {
  const calls = [
    ...timelineCalls(message.timelineItems),
    ...(message.toolCalls ?? []),
  ]
  const seen = new Set<string>()
  const direct = (message.sessionReferences ?? []).flatMap(reference => {
    const normalized = normalizeSessionReferenceV1(reference)
    return normalized ? [{ callId: 'message-reference', reference: normalized }] : []
  })
  return [...direct, ...calls.flatMap(call => sessionReferencesFromToolCall(call).map(reference => ({
    callId: call.toolId,
    reference,
  })))].filter(link => {
    if (seen.has(link.reference.id)) return false
    seen.add(link.reference.id)
    return true
  })
}
