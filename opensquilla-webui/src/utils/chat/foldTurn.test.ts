import { describe, it, expect, vi } from 'vitest'
import { foldTurn, TurnAccumulator } from './foldTurn'
import type { ChatToolCall, ChatToolCallGroup } from '@/types/chat'
import type { ArtifactPayload } from '@/types/rpc'
import type { Frame } from '@/types/turnlog'
import type { InterruptViewState } from '@/types/parts'

// Pure stubs: the reducer's full-result / terminal-state / accumulation
// invariants are independent of markdown rendering and tool grouping, so the
// fold can be exercised with an identity renderer and an empty grouper.
const renderMarkdown = (text: string) => text
const toolCallGroups = (_calls: ChatToolCall[] | undefined, _baseKey: string): ChatToolCallGroup[] => []

function fold(events: Frame[]) {
  return foldTurn(events, renderMarkdown, toolCallGroups)
}

describe('foldTurn — tool result preservation', () => {
  it('keeps the FULL tool result while truncating only the preview', () => {
    const fullResult = 'A'.repeat(250) + '-TAIL-' + 'B'.repeat(250) // well over 200 chars
    const { toolCalls } = fold([
      { kind: 'tool-start', seq: 0, toolId: 't1', name: 'bash', input: '{"cmd":"ls"}', at: 1000 },
      { kind: 'tool-result', seq: 1, toolId: 't1', name: 'bash', result: fullResult, isError: false, input: '{"cmd":"ls"}', at: 2000 },
    ])
    expect(toolCalls).toHaveLength(1)
    // The full result is retained verbatim. This is the field the parity
    // comparator must compare; truncating it to a 200-char preview is exactly
    // the gap that let a divergent saved turn pass unnoticed.
    expect(toolCalls[0].result).toBe(fullResult)
    expect(toolCalls[0].result.length).toBe(fullResult.length)
    // The preview is a bounded truncation, strictly shorter than the result.
    expect(toolCalls[0].resultPreview.length).toBeLessThan(fullResult.length)
    expect(toolCalls[0].resultPreview).not.toBe(toolCalls[0].result)
  })

  it('marks terminal state from the result frame (success vs error)', () => {
    const ok = fold([
      { kind: 'tool-result', seq: 0, toolId: 'a', name: 'x', result: 'done', isError: false, input: '', at: 1 },
    ]).toolCalls[0]
    expect(ok.status).toBe('success')
    expect(ok.isError).toBe(false)
    expect(ok.isRunning).toBe(false)

    const bad = fold([
      { kind: 'tool-result', seq: 0, toolId: 'a', name: 'x', result: 'boom', isError: true, input: '', at: 1 },
    ]).toolCalls[0]
    expect(bad.status).toBe('error')
    expect(bad.isError).toBe(true)
  })

  it('creates a result-only call when no tool-start preceded it', () => {
    const { toolCalls } = fold([
      { kind: 'tool-result', seq: 0, toolId: 'orphan', name: 'grep', result: 'hit', isError: false, input: '{}', at: 5 },
    ])
    expect(toolCalls).toHaveLength(1)
    expect(toolCalls[0].toolId).toBe('orphan')
    expect(toolCalls[0].result).toBe('hit')
  })
})

describe('foldTurn — text, thinking, status, artifacts', () => {
  it('keeps a resolved approval in its true position between later timeline events', () => {
    const interruptState = new Map<string, InterruptViewState>([
      ['approval-1', { resolution: 'approved', busy: false, error: '' }],
    ])
    const f = foldTurn([
      { kind: 'text', seq: 0, text: 'before' },
      {
        kind: 'interrupt',
        seq: 1,
        interruptKind: 'approval',
        approvalId: 'approval-1',
        data: {
          approvalId: 'approval-1',
          namespace: 'exec',
          toolName: 'sandbox elevation',
          command: 'python -c pass',
          approvalKind: 'sandbox_elevation',
          args: null,
          warning: '',
          agent: 'main',
          sessionKey: 'agent:main:web',
          deadline: 0,
        },
        at: 1000,
      },
      { kind: 'text', seq: 2, text: 'after' },
    ], renderMarkdown, toolCallGroups, 'stream', interruptState)

    expect(f.timelineItems.map(item => item.type)).toEqual(['text', 'interrupt', 'text'])
    expect(f.timelineItems[1]).toMatchObject({
      type: 'interrupt',
      part: {
        interruptKind: 'approval',
        resolution: 'approved',
      },
    })
    expect(f.parts.map(part => part.type)).toEqual(['text', 'interrupt', 'text'])
  })

  it('accumulates streamed text and lets final-text override it', () => {
    expect(fold([
      { kind: 'text', seq: 0, text: 'Hello ' },
      { kind: 'text', seq: 1, text: 'world' },
    ]).rawText).toBe('Hello world')

    expect(fold([
      { kind: 'text', seq: 0, text: 'Hello ' },
      { kind: 'text', seq: 1, text: 'world' },
      { kind: 'final-text', seq: 2, text: 'Final answer' },
    ]).rawText).toBe('Final answer')
  })

  it('preserves semantic text boundaries for live answer streaming', () => {
    const f = fold([
      { kind: 'tool-start', seq: 0, toolId: 't', name: 'bash', input: '{}', at: 1 },
      { kind: 'tool-result', seq: 1, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 2 },
      { kind: 'text', seq: 2, text: 'Checking.', presentation: 'intermediate' },
      { kind: 'text', seq: 3, text: 'Answer', presentation: 'answer' },
    ])

    expect(f.timelineItems).toEqual([
      expect.objectContaining({ type: 'tool-group' }),
      expect.objectContaining({ type: 'text', rawText: 'Checking.', presentation: 'intermediate' }),
      expect.objectContaining({ type: 'text', rawText: 'Answer', presentation: 'answer' }),
    ])
    expect(f.timelineSegments).toEqual([
      expect.objectContaining({ type: 'tool-group' }),
      { type: 'text', raw: 'Checking.', presentation: 'intermediate' },
      { type: 'text', raw: 'Answer', presentation: 'answer' },
    ])
  })

  it('does not merge an authoritative answer suffix into intermediate commentary', () => {
    const f = fold([
      { kind: 'tool-result', seq: 0, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 1 },
      { kind: 'text', seq: 1, text: 'Working note.', presentation: 'intermediate' },
      { kind: 'final-text', seq: 2, text: 'Working note.Final answer.' },
    ])

    expect(f.rawText).toBe('Working note.Final answer.')
    expect(f.timelineItems).toHaveLength(3)
    expect(f.timelineItems[1]).toMatchObject({
      type: 'text',
      rawText: 'Working note.',
      presentation: 'intermediate',
    })
    expect(f.timelineItems[2]).toMatchObject({
      type: 'text',
      rawText: 'Final answer.',
      presentation: 'answer',
    })
  })

  it('replaces stale text around tools with one canonical terminal segment', () => {
    const f = fold([
      { kind: 'text', seq: 0, text: 'stale preface' },
      { kind: 'tool-start', seq: 1, toolId: 't', name: 'bash', input: '{}', at: 1 },
      { kind: 'tool-result', seq: 2, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 2 },
      { kind: 'text', seq: 3, text: 'stale retry' },
      { kind: 'final-text', seq: 4, text: 'Canonical answer' },
    ])

    expect(f.rawText).toBe('Canonical answer')
    expect(f.timelineItems.map(item => item.type)).toEqual(['tool-group', 'text'])
    expect(f.timelineItems[1]).toMatchObject({ type: 'text', html: 'Canonical answer' })
    expect(f.toolCalls[0]).toMatchObject({ toolId: 't', status: 'success', result: 'ok' })
  })

  it('treats an empty terminal snapshot as an authoritative text clear', () => {
    const f = fold([
      { kind: 'text', seq: 0, text: 'stale text' },
      { kind: 'tool-start', seq: 1, toolId: 't', name: 'bash', input: '{}', at: 1 },
      { kind: 'tool-result', seq: 2, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 2 },
      { kind: 'final-text', seq: 3, text: '' },
    ])

    expect(f.rawText).toBe('')
    expect(f.timelineItems.map(item => item.type)).toEqual(['tool-group'])
    expect(f.toolCalls[0]).toMatchObject({ toolId: 't', status: 'success' })
  })

  it('adds a strict terminal extension after the last tool group', () => {
    const f = fold([
      { kind: 'text', seq: 0, text: 'Canonical prefix' },
      { kind: 'tool-start', seq: 1, toolId: 't', name: 'bash', input: '{}', at: 1 },
      { kind: 'tool-result', seq: 2, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 2 },
      { kind: 'final-text', seq: 3, text: 'Canonical prefix and suffix' },
    ])

    expect(f.rawText).toBe('Canonical prefix and suffix')
    expect(f.timelineItems.map(item => item.type)).toEqual(['text', 'tool-group', 'text'])
    expect(f.timelineItems[2]).toMatchObject({ type: 'text', html: ' and suffix' })
  })

  it('accumulates thinking text separately from raw text', () => {
    const f = fold([
      { kind: 'thinking', seq: 0, text: 'pon', at: 1 },
      { kind: 'thinking', seq: 1, text: 'dering', at: 2 },
      { kind: 'text', seq: 2, text: 'answer' },
    ])
    expect(f.thinkingText).toBe('pondering')
    expect(f.reasoningBlocks).toMatchObject([{
      id: 'legacy-reasoning',
      index: 0,
      text: 'pondering',
      status: 'streaming',
      startedAt: 1,
    }])
    expect(f.rawText).toBe('answer')
  })

  it('preserves multiple reasoning blocks and their terminal states', () => {
    const f = fold([
      { kind: 'thinking-start', seq: 0, blockId: 'r1', blockIndex: 0, at: 100 },
      { kind: 'thinking', seq: 1, blockId: 'r1', blockIndex: 0, text: 'plan', at: 100 },
      { kind: 'thinking-end', seq: 2, blockId: 'r1', blockIndex: 0, status: 'completed', at: 200 },
      { kind: 'tool-start', seq: 3, toolId: 't', name: 'bash', input: '{}', at: 250 },
      { kind: 'tool-result', seq: 4, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 300 },
      { kind: 'thinking-start', seq: 5, blockId: 'r2', blockIndex: 1, at: 400 },
      { kind: 'thinking', seq: 6, blockId: 'r2', blockIndex: 1, text: 'review', at: 400 },
      { kind: 'thinking-end', seq: 7, blockId: 'r2', blockIndex: 1, status: 'interrupted', at: 500 },
    ])

    expect(f.thinkingText).toBe('planreview')
    expect(f.reasoningBlocks).toEqual([
      {
        id: 'r1',
        index: 0,
        text: 'plan',
        status: 'completed',
        startedAt: 100,
        endedAt: 200,
        contentKind: 'reasoning',
      },
      {
        id: 'r2',
        index: 1,
        text: 'review',
        status: 'interrupted',
        startedAt: 400,
        endedAt: 500,
        contentKind: 'reasoning',
      },
    ])
  })

  it('settles a prior block when a newer boundary arrives without an end event', () => {
    const f = fold([
      { kind: 'thinking', seq: 0, blockId: 'r1', blockIndex: 0, text: 'first', at: 100 },
      { kind: 'thinking', seq: 1, blockId: 'r2', blockIndex: 1, text: 'second', at: 200 },
    ])

    expect(f.reasoningBlocks.map(block => [block.id, block.status, block.endedAt])).toEqual([
      ['r1', 'completed', 200],
      ['r2', 'streaming', undefined],
    ])
  })

  it('records status transitions in arrival order with monotonic timestamps', () => {
    const f = fold([
      { kind: 'status', seq: 0, action: 'plan', label: 'Planning', at: 1000 },
      { kind: 'status', seq: 1, action: 'run', label: 'Running', at: 2000 },
    ])
    expect(f.statusHistory.map(s => s.action)).toEqual(['plan', 'run'])
    expect(f.statusHistory[0].at).toBeLessThanOrEqual(f.statusHistory[1].at)
  })

  it('merges maintenance completion into its original compaction row', () => {
    const f = fold([
      {
        kind: 'status',
        seq: 0,
        action: 'context_compaction',
        label: '',
        at: 1000,
        id: 'cmp-1',
        category: 'maintenance',
        state: 'running',
        source: 'automatic',
        durability: 'durable',
      },
      {
        kind: 'status',
        seq: 1,
        action: 'context_compaction',
        label: '',
        at: 2000,
        id: 'cmp-1',
        category: 'maintenance',
        state: 'completed',
        source: 'automatic',
        durability: 'durable',
        detail: 'Stable context compacted',
      },
    ])

    expect(f.statusHistory).toEqual([{
      action: 'context_compaction',
      label: '',
      at: 1000,
      id: 'cmp-1',
      category: 'maintenance',
      state: 'completed',
      source: 'automatic',
      durability: 'durable',
      detail: 'Stable context compacted',
    }])
  })

  it('preserves artifact arrival order', () => {
    const a1 = { id: 'a1', name: 'one.txt' } as unknown as ArtifactPayload
    const a2 = { id: 'a2', name: 'two.txt' } as unknown as ArtifactPayload
    const f = fold([
      { kind: 'artifact', seq: 0, artifact: a1 },
      { kind: 'artifact', seq: 1, artifact: a2 },
    ])
    expect(f.artifacts).toEqual([a1, a2])
  })
})

describe('foldTurn — purity', () => {
  it('is deterministic: folding the same log twice yields equal results', () => {
    const events: Frame[] = [
      { kind: 'text', seq: 0, text: 'hi' },
      { kind: 'tool-start', seq: 1, toolId: 't', name: 'bash', input: '{}', at: 1 },
      { kind: 'tool-result', seq: 2, toolId: 't', name: 'bash', result: 'ok', isError: false, input: '{}', at: 2 },
    ]
    const a = fold(events)
    const b = fold(events)
    expect(a.rawText).toBe(b.rawText)
    expect(a.toolCalls).toEqual(b.toolCalls)
  })
})

describe('TurnAccumulator — incremental live projection', () => {
  it('matches the pure replay oracle across text, tools, status, and terminal reconcile', () => {
    const events: Frame[] = [
      { kind: 'status', seq: 0, action: 'requesting', label: 'Waiting', at: 1 },
      { kind: 'thinking', seq: 1, text: 'checking', at: 2 },
      { kind: 'text', seq: 2, text: 'stale', presentation: 'intermediate' },
      { kind: 'tool-start', seq: 3, toolId: 't', name: 'bash', input: '{', at: 3 },
      { kind: 'tool-delta', seq: 4, toolId: 't', fragment: '}' },
      { kind: 'tool-result', seq: 5, toolId: 't', name: 'bash', input: '{}', result: 'ok', isError: false, at: 4 },
      { kind: 'final-text', seq: 6, text: 'canonical' },
    ]
    const accumulator = new TurnAccumulator()
    events.forEach(event => accumulator.append(event))

    const incremental = accumulator.snapshot(renderMarkdown, toolCallGroups)
    const replayed = fold(events)
    expect(incremental.rawText).toBe(replayed.rawText)
    expect(incremental.thinkingText).toBe(replayed.thinkingText)
    expect(incremental.reasoningBlocks).toEqual(replayed.reasoningBlocks)
    expect(incremental.timelineSegments).toEqual(replayed.timelineSegments)
    expect(incremental.toolCalls).toEqual(replayed.toolCalls)
    expect(incremental.statusHistory).toEqual(replayed.statusHistory)
    expect(incremental.parts).toEqual(replayed.parts)
  })

  it('publishes reasoning blocks before answer text and settles the previous block', () => {
    const accumulator = new TurnAccumulator()
    accumulator.append({
      kind: 'thinking-start',
      seq: 0,
      blockId: 'reasoning-1',
      blockIndex: 0,
      at: 100,
    })
    accumulator.append({
      kind: 'thinking',
      seq: 1,
      blockId: 'reasoning-1',
      blockIndex: 0,
      text: 'first live delta',
      at: 110,
    })

    const first = accumulator.snapshot(renderMarkdown, toolCallGroups)
    expect(first.rawText).toBe('')
    expect(first.reasoningBlocks).toEqual([expect.objectContaining({
      id: 'reasoning-1',
      text: 'first live delta',
      status: 'streaming',
    })])

    accumulator.append({
      kind: 'thinking-start',
      seq: 2,
      blockId: 'reasoning-2',
      blockIndex: 1,
      at: 200,
    })
    accumulator.append({
      kind: 'thinking',
      seq: 3,
      blockId: 'reasoning-2',
      blockIndex: 1,
      text: 'second live delta',
      at: 210,
    })

    const second = accumulator.snapshot(renderMarkdown, toolCallGroups)
    expect(second.reasoningBlocks).toEqual([
      expect.objectContaining({
        id: 'reasoning-1',
        text: 'first live delta',
        status: 'completed',
        endedAt: 200,
      }),
      expect.objectContaining({
        id: 'reasoning-2',
        text: 'second live delta',
        status: 'streaming',
      }),
    ])
    expect(first.reasoningBlocks[0]?.status).toBe('streaming')
  })

  it('does not invoke Markdown for a tool-only burst', () => {
    const accumulator = new TurnAccumulator()
    accumulator.append({
      kind: 'tool-start',
      seq: 0,
      toolId: 'tool-1',
      name: 'bash',
      input: '',
      at: 1,
    })
    for (let index = 0; index < 10_000; index += 1) {
      accumulator.append({
        kind: 'tool-delta',
        seq: index + 1,
        toolId: 'tool-1',
        fragment: 'x',
      })
    }
    const renderer = vi.fn((text: string) => text)
    const snapshot = accumulator.snapshot(renderer, toolCallGroups)
    expect(renderer).not.toHaveBeenCalled()
    expect(snapshot.toolCalls[0]?.inputRaw).toHaveLength(10_000)
  })

  it('renders a provisional answer after a later tool moves it into activity', () => {
    const accumulator = new TurnAccumulator()
    accumulator.append({
      kind: 'tool-start',
      seq: 0,
      toolId: 'inspect',
      name: 'read_file',
      input: '{}',
      at: 1,
    })
    accumulator.append({
      kind: 'text',
      seq: 1,
      text: 'Draft candidate.',
      presentation: 'answer',
    })
    const initial = accumulator.snapshot(
      text => `<p>${text}</p>`,
      toolCallGroups,
      undefined,
      undefined,
      false,
    )
    expect(initial.timelineItems[1]).toMatchObject({ html: '' })
    accumulator.append({
      kind: 'tool-start',
      seq: 2,
      toolId: 'verify',
      name: 'bash_exec',
      input: '{}',
      at: 2,
    })

    const renderer = vi.fn((text: string) => `<p>${text}</p>`)
    const snapshot = accumulator.snapshot(
      renderer,
      toolCallGroups,
      undefined,
      undefined,
      false,
    )

    expect(renderer).toHaveBeenCalledWith('Draft candidate.')
    expect(snapshot.timelineItems[1]).toMatchObject({
      type: 'text',
      rawText: 'Draft candidate.',
      html: '<p>Draft candidate.</p>',
    })
  })
})
