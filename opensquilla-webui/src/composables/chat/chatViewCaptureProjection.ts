import { toRaw } from 'vue'
import type { ChatMessage } from '@/types/chat'

/**
 * A cache-only fallback for duplicated outcome activity graphs. History mapping
 * already projects the assistant's activity/status onto the message; user rows
 * never render those outcome graphs. Keep every body, tool, identity and scalar
 * outcome fact. This is not a history projection and must never be installed.
 */
export function compactChatViewCaptureMessages(messages: readonly ChatMessage[]): ChatMessage[] | undefined {
  let properties = 0
  let changed = false
  const rejected = Symbol('capture-projection')
  function copy(value: object, omit: (key: string) => boolean = () => false): Record<string, unknown> {
    const raw = toRaw(value)
    const prototype = Object.getPrototypeOf(raw)
    if (prototype !== Object.prototype && prototype !== null) throw rejected
    const result: Record<string, unknown> = Object.create(null)
    // Bound construction itself; never enumerate a whole graph or invoke an
    // accessor/toJSON before the normal capture budget checks the candidate.
    for (const key in raw) {
      if (++properties > 20_000) throw rejected
      const descriptor = Object.getOwnPropertyDescriptor(raw, key)
      if (!descriptor) continue
      if (!('value' in descriptor) || key === 'toJSON') throw rejected
      if (!omit(key)) result[key] = descriptor.value
      else changed = true
    }
    return result
  }
  try {
    if (messages.length > 200) return undefined
    const projected: ChatMessage[] = []
    for (const message of messages) {
      const row = copy(message)
      const outcome = row.turnOutcome
      if (outcome && typeof outcome === 'object' && !Array.isArray(outcome)) {
        row.turnOutcome = copy(outcome, key => (
          key === 'activitySnapshot' && (row.role !== 'assistant' || row.activitySnapshot !== undefined)
        ) || (
          key === 'statusHistory' && (row.role !== 'assistant' || row.statusHistory !== undefined)
        ))
      }
      projected.push(row as unknown as ChatMessage)
    }
    return changed ? projected : undefined
  } catch {
    return undefined
  }
}
