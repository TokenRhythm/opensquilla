import { describe, expect, it } from 'vitest'
import { createConversationRuntime, type ConversationCursor } from './conversationRuntime'

function cursor(runtime: ReturnType<typeof createConversationRuntime>, seed: Partial<ConversationCursor> = {}) {
  return runtime.createCursor('session-a', seed)
}

describe('ConversationRuntime', () => {
  const runtime = createConversationRuntime()

  it('accepts increasing event sequences and rejects duplicates', () => {
    const first = runtime.acceptEvent(cursor(runtime), {
      sessionKey: 'session-a',
      streamSeq: 4,
    })
    expect(first.accepted).toBe(true)
    expect(first.cursor.streamSeq).toBe(4)

    const duplicate = runtime.acceptEvent(first.cursor, {
      sessionKey: 'session-a',
      streamSeq: 4,
    })
    expect(duplicate.accepted).toBe(false)
    expect(duplicate.reason).toBe('duplicate-sequence')
  })

  it('rejects events from another session without changing the cursor', () => {
    const initial = cursor(runtime, { streamSeq: 9 })
    const decision = runtime.acceptEvent(initial, {
      sessionKey: 'session-b',
      streamGeneration: 'foreign-generation',
      streamSeq: 10,
    })
    expect(decision.accepted).toBe(false)
    expect(decision.reason).toBe('session-mismatch')
    expect(decision.cursor).toEqual(initial)
  })

  it('adopts the first generation unless a visible restart makes the old cursor unsafe', () => {
    const normal = runtime.observeGeneration(cursor(runtime, { streamSeq: 3 }), {
      streamGeneration: 'g1',
      streamSeq: 8,
    })
    expect(normal.changed).toBe(true)
    expect(normal.reset).toBe(false)
    expect(normal.cursor.streamSeq).toBe(3)

    const restarted = runtime.observeGeneration(cursor(runtime, { streamSeq: 8 }), {
      streamGeneration: 'g1',
      streamSeq: 2,
    })
    expect(restarted.reset).toBe(true)
    expect(restarted.cursor.streamSeq).toBe(0)
  })

  it('resets sequence when an established generation changes', () => {
    const initial = cursor(runtime, { streamGeneration: 'g1', streamSeq: 19 })
    const transition = runtime.acceptEvent(initial, {
      streamGeneration: 'g2',
      streamSeq: 1,
    })
    expect(transition.accepted).toBe(true)
    expect(transition.changed).toBe(true)
    expect(transition.reset).toBe(true)
    expect(transition.cursor.streamGeneration).toBe('g2')
    expect(transition.cursor.streamSeq).toBe(1)
  })

  it('recognizes a replay gap even when the first sequence is not lower', () => {
    const transition = runtime.observeGeneration(cursor(runtime, { streamSeq: 3 }), {
      streamGeneration: 'g1',
      streamSeq: 3,
      replayGapReason: 'stream_generation_changed',
    })
    expect(transition.reset).toBe(true)
  })

  it('keeps stale session epochs out of the live stream', () => {
    const initial = cursor(runtime, { sessionEpoch: 5, streamSeq: 2 })
    const decision = runtime.acceptEvent(initial, {
      sessionEpoch: 4,
      streamSeq: 3,
    })
    expect(decision.accepted).toBe(false)
    expect(decision.reason).toBe('stale-epoch')
    expect(runtime.isStaleEpoch(initial, 4)).toBe(true)
  })

  it('advances the epoch monotonically', () => {
    const initial = cursor(runtime, { sessionEpoch: 2 })
    const advanced = runtime.advanceEpoch(initial, 7)
    expect(advanced.changed).toBe(true)
    expect(advanced.cursor.sessionEpoch).toBe(7)
    expect(runtime.advanceEpoch(advanced.cursor, 6).changed).toBe(false)
  })

  it('accepts a snapshot only when it cannot overwrite newer live events', () => {
    const initial = cursor(runtime, { streamGeneration: 'g1', streamSeq: 12 })
    const behind = runtime.acceptSnapshot(initial, {
      sessionKey: 'session-a',
      streamGeneration: 'g1',
      currentStreamSeq: 11,
    })
    expect(behind.accepted).toBe(false)
    expect(behind.reason).toBe('snapshot-behind')

    const current = runtime.acceptSnapshot(initial, {
      sessionKey: 'session-a',
      streamGeneration: 'g1',
      currentStreamSeq: 15,
    })
    expect(current.accepted).toBe(true)
    expect(current.cursor.streamSeq).toBe(15)
  })

  it('rejects snapshots from a different generation or without a cursor', () => {
    const initial = cursor(runtime, { streamGeneration: 'g1' })
    expect(runtime.acceptSnapshot(initial, {
      streamGeneration: 'g2',
      currentStreamSeq: 2,
    }).reason).toBe('generation-mismatch')
    expect(runtime.acceptSnapshot(initial, {}).reason).toBe('invalid-snapshot')
  })

  it('applies replay cursors and signals history recovery', () => {
    const initial = cursor(runtime, { streamSeq: 5 })
    const replay = runtime.applyReplayCursor(initial, {
      currentStreamSeq: 3,
      replayComplete: false,
    })
    expect(replay.requiresHistory).toBe(true)
    expect(replay.cursor.streamSeq).toBe(5)

    const reset = runtime.applyReplayCursor(cursor(runtime), {
      currentStreamSeq: 2,
    }, true)
    expect(reset.requiresHistory).toBe(true)
    expect(reset.cursor.streamSeq).toBe(2)
  })

  it('resets all session-local cursor state for a route handoff', () => {
    const initial = cursor(runtime, {
      sessionEpoch: 9,
      streamGeneration: 'g7',
      streamSeq: 42,
    })
    const reset = runtime.reset(initial, 'session-b')
    expect(reset).toEqual({
      sessionKey: 'session-b',
      sessionEpoch: 0,
      streamGeneration: null,
      streamSeq: 0,
    })
  })
})
