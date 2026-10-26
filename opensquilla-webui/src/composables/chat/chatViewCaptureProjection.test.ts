import { describe, expect, it, vi } from 'vitest'
import type { ChatMessage } from '@/types/chat'
import { compactChatViewCaptureMessages } from './chatViewCaptureProjection'

describe('bounded cache-only duplicate outcome projection', () => {
  it('retains all activity bodies, terminal semantics, routing and attachments in their rendered owners', () => {
    const statusHistory = [{ type: 'status', action: 'completed', at: 1 }]
    const activitySnapshot = { version: 2, turnId: 'turn', complete: true, entries: [] }
    const row = {
      role: 'assistant', messageId: 'answer', turnId: 'turn', text: 'exact answer', ts: 1,
      tool_calls: [{ name: 'exec', result: 'exact tool output', execution_status: 'completed' }],
      attachments: [{ name: 'artifact.svg', url: 'artifact.svg' }],
      routerSettled: true, statusHistory, activitySnapshot,
      turnOutcome: { turnId: 'turn', taskId: 'task', status: 'failed', statusSource: 'task',
        errorClass: 'provider_error', httpStatus: 429, replaySafe: false, statusHistory, activitySnapshot },
    } as unknown as ChatMessage
    const [projected] = compactChatViewCaptureMessages([row])!
    expect(projected).toEqual({ ...row, turnOutcome: {
      turnId: 'turn', taskId: 'task', status: 'failed', statusSource: 'task',
      errorClass: 'provider_error', httpStatus: 429, replaySafe: false,
    } })
    expect(projected?.statusHistory).toBe(row.statusHistory)
    expect(projected?.activitySnapshot).toBe(row.activitySnapshot)
    expect(projected?.tool_calls).toBe(row.tool_calls)
    expect(row.turnOutcome?.activitySnapshot).toBe(activitySnapshot)
  })

  it('does not discard an assistant outcome graph without its projected owner', () => {
    const row = { role: 'assistant', text: '', ts: null, turnOutcome: {
      turnId: 'turn', status: 'succeeded', statusHistory: [{ type: 'status', action: 'complete' }],
    } } as unknown as ChatMessage
    expect(compactChatViewCaptureMessages([row])).toBeUndefined()
  })

  it('never executes getters or serialization hooks in a fallback candidate', () => {
    const getter = vi.fn(() => 'unbounded')
    const row = Object.defineProperty({ role: 'user', text: 'query', ts: null }, 'turnOutcome', {
      enumerable: true, get: getter,
    }) as ChatMessage
    expect(compactChatViewCaptureMessages([row])).toBeUndefined()
    expect(getter).not.toHaveBeenCalled()
    const toJSON = vi.fn()
    expect(compactChatViewCaptureMessages([{ role: 'user', text: 'query', ts: null, toJSON } as ChatMessage])).toBeUndefined()
    expect(toJSON).not.toHaveBeenCalled()
  })

  it('bounds shallow construction before traversing a huge graph or property set', () => {
    const getter = vi.fn()
    const huge = Object.fromEntries(Array.from({ length: 20_001 }, (_, index) => [String(index), undefined]))
    Object.defineProperty(huge, 'late', { enumerable: true, get: getter })
    expect(compactChatViewCaptureMessages([huge as unknown as ChatMessage])).toBeUndefined()
    expect(getter).not.toHaveBeenCalled()
    expect(compactChatViewCaptureMessages(Array(201).fill({ role: 'user', text: 'query', ts: null }))).toBeUndefined()
  })
})
