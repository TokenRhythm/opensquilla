import { describe, it, expect } from 'vitest'
import { foldTurn } from './foldTurn'
import type { ChatToolCall, ChatToolCallGroup } from '@/types/chat'
import type { ArtifactPayload } from '@/types/rpc'
import type { Frame } from '@/types/turnlog'

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
    expect(f.rawText).toBe('answer')
  })

  it('records status transitions in arrival order with monotonic timestamps', () => {
    const f = fold([
      { kind: 'status', seq: 0, action: 'plan', label: 'Planning', at: 1000 },
      { kind: 'status', seq: 1, action: 'run', label: 'Running', at: 2000 },
    ])
    expect(f.statusHistory.map(s => s.action)).toEqual(['plan', 'run'])
    expect(f.statusHistory[0].at).toBeLessThanOrEqual(f.statusHistory[1].at)
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
