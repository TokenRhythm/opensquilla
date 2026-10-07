import { describe, expect, it, vi } from 'vitest'

import {
  clampMermaidPreviewHeight,
  escapeSequenceTextSemicolons,
  estimateMermaidPreviewHeight,
  formatMermaidParseError,
  getMermaidDiagramKind,
  renderMermaidWithRetry,
} from './mermaidUtils'

describe('mermaid diagram kind', () => {
  it.each([
    ['flowchart TD\n  a --> b', 'flowchart'],
    ['graph LR\n  a --> b', 'graph'],
    ['sequenceDiagram\n  A->>B: hi', 'sequencediagram'],
    ['%% comment\nsequenceDiagram\n  A->>B: hi', 'sequencediagram'],
    ['gantt\n  title plan', 'gantt'],
  ])('detects %s', (code, expected) => {
    expect(getMermaidDiagramKind(code)).toBe(expected)
  })
})

describe('mermaid preview height estimation', () => {
  it('scales with line count per diagram kind', () => {
    const flowchart = 'flowchart TD\n' + Array.from({ length: 10 }, (_, i) => `  n${i}[x]`).join('\n')
    const gantt = 'gantt\n' + Array.from({ length: 10 }, (_, i) => `  task${i} :a1`).join('\n')
    expect(estimateMermaidPreviewHeight(flowchart)).toBeGreaterThan(estimateMermaidPreviewHeight('flowchart TD\n  a --> b'))
    expect(estimateMermaidPreviewHeight(gantt)).toBeGreaterThan(estimateMermaidPreviewHeight('flowchart TD\n  a --> b'))
  })

  it('clamps into the placeholder band', () => {
    expect(clampMermaidPreviewHeight(10)).toBeGreaterThanOrEqual(60)
    expect(clampMermaidPreviewHeight(100_000)).toBeLessThanOrEqual(500)
  })
})

describe('sequence-diagram semicolon escaping', () => {
  it('only rewrites sequence diagrams', () => {
    const code = 'flowchart TD\n  a["BEGIN; SELECT"] --> b'
    expect(escapeSequenceTextSemicolons(code)).toBe(code)
  })

  it('escapes semicolons inside message text but keeps statement separators', () => {
    const code = [
      'sequenceDiagram',
      '  A->>B: BEGIN; SELECT 1',
      '  Note over A: run; then wait',
    ].join('\n')
    const escaped = escapeSequenceTextSemicolons(code)
    expect(escaped).toContain('BEGIN#59; SELECT 1')
    expect(escaped).toContain('run#59; then wait')
    // Statement lines keep their terminator semantics.
    expect(escaped.split('\n')).toHaveLength(3)
  })

  it('leaves already-escaped entities untouched', () => {
    const code = 'sequenceDiagram\n  A->>B: price &#59; ok'
    expect(escapeSequenceTextSemicolons(code)).toBe(code)
  })
})

describe('render retry', () => {
  it('retries once with escaped semicolons after a failure', async () => {
    const render = vi.fn()
      .mockRejectedValueOnce(new Error('Parse error'))
      .mockResolvedValueOnce({ svg: '<svg/>' })
    const result = await renderMermaidWithRetry(
      render,
      'id-1',
      'sequenceDiagram\n  A->>B: BEGIN; SELECT',
    )
    expect(result.svg).toBe('<svg/>')
    expect(render).toHaveBeenCalledTimes(2)
    expect(render).toHaveBeenLastCalledWith('id-1', 'sequenceDiagram\n  A->>B: BEGIN#59; SELECT', undefined)
  })

  it('rethrows the original error when escaping cannot change the source', async () => {
    const failure = new Error('boom')
    const render = vi.fn().mockRejectedValue(failure)
    await expect(renderMermaidWithRetry(render, 'id-2', 'flowchart TD\n a-->b')).rejects.toBe(failure)
    expect(render).toHaveBeenCalledTimes(1)
  })
})

describe('parse error short message', () => {
  it('extracts line, excerpt and expectation from a mermaid parse error', () => {
    const message = [
      'Parse error on line 3:',
      'A->>B: begin; x',
      '--------------^',
      "Expecting 'SEMICOLON', 'NEWLINE', 'EOF', 'PLUS', 'MINUS', got 'UNTITLED'",
    ].join('\n')
    expect(formatMermaidParseError(message)).toEqual({
      line: 3,
      excerpt: 'A->>B: begin; x',
      expecting: "Expecting 'SEMICOLON', 'NEWLINE', 'EOF', 'PLUS', 'MINUS', got 'UNTITLED'",
    })
  })

  it('returns null for non-parse errors', () => {
    expect(formatMermaidParseError('mermaid module failed to load')).toBeNull()
  })
})
