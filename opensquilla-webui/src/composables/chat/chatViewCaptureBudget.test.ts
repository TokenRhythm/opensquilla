import { describe, expect, it, vi } from 'vitest'
import { createChatViewCaptureBudget } from './chatViewCaptureBudget'

describe('chat view departure budget', () => {
  it('accounts for JSON escaping and UTF-8 at the exact byte boundary without a serialized payload', () => {
    const value = { text: '中文 😀 "\n\\\u0000\ud800', array: [null, 1, true, false, undefined, Infinity] }
    const bytes = new TextEncoder().encode(JSON.stringify({ messages: value })).byteLength
    const exact = createChatViewCaptureBudget(bytes)
    expect(exact.include('messages', value)).toBe(true)
    expect(exact.bytes).toBe(bytes)
    expect(createChatViewCaptureBudget(bytes - 1).include('messages', value)).toBe(false)
  })

  it('never executes accessors or serialization hooks during departure', () => {
    const accessor = vi.fn(() => 'unbounded computation')
    const getter = Object.defineProperty({}, 'text', { enumerable: true, get: accessor })
    const toJSON = vi.fn(() => ({ text: 'not the preflighted payload' }))
    expect(createChatViewCaptureBudget(1024).include('messages', getter)).toBe(false)
    expect(createChatViewCaptureBudget(1024).include('messages', { toJSON })).toBe(false)
    expect(accessor).not.toHaveBeenCalled()
    expect(toJSON).not.toHaveBeenCalled()
  })

  it('rejects excessive node/depth graphs while allowing shared acyclic references', () => {
    const shared = { text: 'shared text' }
    expect(createChatViewCaptureBudget(1024).include('messages', [shared, shared])).toBe(true)
    expect(createChatViewCaptureBudget(8 * 1024 * 1024).include('messages', Array(100_000).fill(null))).toBe(false)
    const omitted = Object.fromEntries(Array.from({ length: 20_001 }, (_, index) => [String(index), undefined]))
    expect(createChatViewCaptureBudget(1024).include('messages', omitted)).toBe(false)
    let deep: unknown = 'tail'
    for (let index = 0; index < 40; index++) deep = { next: deep }
    expect(createChatViewCaptureBudget(1024).include('messages', deep)).toBe(false)
  })

  it('reports bounded structural rejection separately from bytes without retaining input', () => {
    const nested = Array.from({ length: 5_001 }, () => ({ a: 1, b: 2, c: 3 }))
    const budget = createChatViewCaptureBudget(8 * 1024 * 1024)
    expect(budget.include('messages', nested)).toBe(false)
    expect(budget.rejectionReason).toBe('nodes')
    expect(budget.bytes).toBeLessThan(8 * 1024 * 1024)
    expect(budget.visitedNodes).toBe(20_001)
    const bytes = createChatViewCaptureBudget(32)
    expect(bytes.include('messages', 'x'.repeat(100))).toBe(false)
    expect(bytes.rejectionReason).toBe('bytes')
  })
})
