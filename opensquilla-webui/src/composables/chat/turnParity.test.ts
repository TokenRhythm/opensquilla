import { describe, it, expect } from 'vitest'
import { diffFoldVsLegacy, type LegacyTurnSurface } from './turnParity'
import type { FoldedTurn } from '@/utils/chat/foldTurn'
import type { ChatToolCall } from '@/types/chat'

function makeCall(overrides: Partial<ChatToolCall> = {}): ChatToolCall {
  return {
    toolId: 't1',
    name: 'bash',
    displayName: 'bash',
    inputPreview: 'ls',
    isRunning: false,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
    isOpen: false,
    ...overrides,
  }
}

// A folded turn whose parts/timeline are empty-but-consistent, so the parts
// coverage check contributes nothing and the tool-call comparison is exercised
// in isolation.
function makeFold(toolCalls: ChatToolCall[], extra: Partial<FoldedTurn> = {}): FoldedTurn {
  return {
    timelineItems: [],
    toolCalls,
    artifacts: [],
    rawText: 'turn',
    thinkingText: '',
    toolTimes: new Map(),
    parts: [],
    sources: [],
    statusHistory: [],
    timelineSegments: [],
    ...extra,
    reasoningBlocks: extra.reasoningBlocks ?? [],
  }
}

function makeLegacy(toolCalls: ChatToolCall[], extra: Partial<LegacyTurnSurface> = {}): LegacyTurnSurface {
  return {
    timelineItems: [],
    rawText: 'turn',
    toolCalls,
    artifacts: [],
    thinkingText: '',
    ...extra,
  }
}

describe('diffFoldVsLegacy — equivalence', () => {
  it('returns no problems when fold and legacy match', () => {
    expect(diffFoldVsLegacy(makeFold([makeCall()]), makeLegacy([makeCall()]))).toEqual([])
  })
})

describe('diffFoldVsLegacy — the persist-by-fold guard', () => {
  it('flags a tool result that diverges PAST the 200-char preview boundary', () => {
    // Both results share an identical first 220 chars (so resultPreview, a
    // 200-char truncation, is identical) and differ only in their tail.
    // Comparing previews alone would call these equivalent — the exact false
    // negative that shipped a divergent saved turn. The full-result comparison
    // catches it.
    const head = 'X'.repeat(220)
    const foldCall = makeCall({ result: head + 'FOLD', resultPreview: 'same-preview' })
    const legacyCall = makeCall({ result: head + 'LEGACY', resultPreview: 'same-preview' })

    // Premise: previews are identical, full results are not.
    expect(foldCall.resultPreview).toBe(legacyCall.resultPreview)
    expect(foldCall.result).not.toBe(legacyCall.result)

    const problems = diffFoldVsLegacy(makeFold([foldCall]), makeLegacy([legacyCall]))
    expect(problems).toHaveLength(1)
    // The diverged-fields list is exactly `result` — pin the whole tail so the
    // substring overlap between 'result' and 'resultPreview' cannot let a
    // preview-only drift masquerade as the full-result catch.
    expect(problems[0]).toMatch(/fields diverge: result$/)
    expect(problems[0]).not.toContain('resultPreview')
  })
})

describe('diffFoldVsLegacy — terminal states and structure', () => {
  it('distinguishes success from error (silence is not a pass)', () => {
    const problems = diffFoldVsLegacy(
      makeFold([makeCall({ status: 'success', isError: false })]),
      makeLegacy([makeCall({ status: 'error', isError: true })]),
    )
    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('status')
    expect(problems[0]).toContain('isError')
  })

  it('flags a tool-call count mismatch', () => {
    const problems = diffFoldVsLegacy(
      makeFold([makeCall(), makeCall({ toolId: 't2' })]),
      makeLegacy([makeCall()]),
    )
    expect(problems.some(p => p.includes('tool call count'))).toBe(true)
  })

  it('flags diverging rawText', () => {
    const problems = diffFoldVsLegacy(
      makeFold([makeCall()], { rawText: 'A' }),
      makeLegacy([makeCall()], { rawText: 'B' }),
    )
    expect(problems.some(p => p.includes('rawText diverges'))).toBe(true)
  })

  it('flags a timeline length mismatch', () => {
    const problems = diffFoldVsLegacy(
      makeFold([makeCall()], { timelineItems: [{ type: 'text', key: 'k1' }] as unknown as FoldedTurn['timelineItems'] }),
      makeLegacy([makeCall()]),
    )
    expect(problems.some(p => p.includes('timeline length'))).toBe(true)
  })

  it('flags a semantic text-presentation mismatch', () => {
    const foldedTimeline = [{
      type: 'text' as const,
      key: 'text-0',
      html: '',
      rawText: 'Working note.',
      presentation: 'intermediate' as const,
    }]
    const legacyTimeline = [{
      ...foldedTimeline[0],
      presentation: 'answer' as const,
    }]
    const problems = diffFoldVsLegacy(
      makeFold([], { timelineItems: foldedTimeline }),
      makeLegacy([], { timelineItems: legacyTimeline }),
    )

    expect(problems).toContain('timeline text 0 presentation diverges')
  })
})
