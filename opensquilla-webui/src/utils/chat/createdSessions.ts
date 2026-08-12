import type {
  ChatCreatedSessionLink,
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCall,
} from '@/types/chat'

export type CreatedSessionLink = ChatCreatedSessionLink

function resultRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text.startsWith('{')) return null
    try {
      const parsed = JSON.parse(text)
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

function spawnedSessionKey(value: unknown): string | null {
  const record = resultRecord(value)
  const key = typeof record?.session_key === 'string'
    ? record.session_key.trim()
    : ''
  if (!/^agent:[^:\s]+:subagent:[^:\s]+$/.test(key)) return null
  return key
}

export function createdSessionFromToolCall(
  call: Pick<ChatToolCall, 'toolId' | 'name' | 'isRunning' | 'status' | 'isError' | 'result'>,
): CreatedSessionLink | null {
  if (
    call.name !== 'sessions_spawn'
    || call.isRunning
    || call.isError
    || call.status !== 'success'
  ) return null
  const sessionKey = spawnedSessionKey(call.result)
  if (!sessionKey) return null
  return {
    callId: call.toolId,
    sessionKey,
  }
}

function timelineCalls(items: ChatStreamTimelineItem[] | undefined): ChatToolCall[] {
  return (items ?? []).flatMap(item => (
    item.type === 'tool-group' ? item.group.calls : []
  ))
}

/**
 * Derive ordered, durable session links from the same normalized tool calls
 * used by both the live stream and restored history renderers.
 */
export function createdSessionsFromMessage(message: ChatRenderedMessage): CreatedSessionLink[] {
  const calls = [
    ...timelineCalls(message.timelineItems),
    ...(message.toolCalls ?? []),
  ]
  const seenCalls = new Set<string>()
  const links: CreatedSessionLink[] = []
  for (const call of calls) {
    const link = createdSessionFromToolCall(call)
    if (!link || seenCalls.has(link.callId)) continue
    seenCalls.add(link.callId)
    links.push(link)
  }
  return links
}
