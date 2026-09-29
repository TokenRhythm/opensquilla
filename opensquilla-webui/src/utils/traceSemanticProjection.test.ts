import { describe, expect, it } from 'vitest'
import type { TraceSpan } from '@/types/traceView'
import { projectTraceSteps } from './traceProjection'

function event(id: string, kind: string, payload?: Record<string, unknown>): TraceSpan {
  return {
    id, kind, title: id, status: 'success', phase: 'unknown',
    recordedAt: 10, elapsedMs: 10,
    ...(kind === 'context_stage' || kind === 'prompt_report' ? { input: payload } : { output: payload }),
  }
}
function context(id: string, stage: string, messages: unknown[] | undefined, extra: Record<string, unknown> = {}): TraceSpan {
  return { ...event(id, 'context_stage', { stage, messages, ...extra }), phase: 'context' }
}
function model(id: string, callId: string, messages: unknown[] = []): TraceSpan {
  return { ...event(id, 'llm_response'), logicalCallId: callId, input: { messages }, durationMs: 25, startedAt: 12, endedAt: 37 }
}
function tool(id: string, toolId: string): TraceSpan {
  return { ...event(id, 'tool_response', { result: 'Original result' }), logicalCallId: toolId, durationMs: 2 }
}
const history = [{ role: 'user', content: 'A synthetic message' }]

describe('semantic trace display projection', () => {
  it('reduces an unchanged read flow to five operations and a result without changing milliseconds or raw rows', () => {
    const rows = [
      event('input', 'turn_start'), event('prompt', 'prompt_report'),
      event('route', 'router_decision'), event('budget', 'agent_runtime_budget'),
      context('loaded', 'session:loaded', history), context('sanitized', 'session:sanitized', history),
      context('limited', 'session:limited', history), context('prompt-before', 'prompt:before', history),
      context('prompt-images', 'prompt:images', history), context('stream-one', 'stream:context', history, { call_id: 'call-one' }),
      model('model-one', 'call-one'), tool('read', 'tool-one'),
      event('noop', 'tool_projection_noop', { tool_use_id: 'tool-one' }),
      context('stream-two', 'stream:context', history, { call_id: 'call-two' }), model('model-two', 'call-two'),
      context('after', 'session:after', history), event('output', 'turn_end'),
    ]
    const original = structuredClone(rows)
    const steps = projectTraceSteps(rows)
    expect(steps.map(step => step.id)).toEqual(['input', 'route', 'model-one', 'read', 'model-two', 'output'])
    expect(steps.map(step => step.lane)).toEqual(['intake', 'control', 'model', 'tool', 'model', 'result'])
    expect(steps[0].rawIds).toEqual(['input', 'prompt'])
    expect(steps[2].rawIds).toContain('budget')
    expect(steps[2].rawIds).toContain('sanitized')
    expect(steps[3].rawIds).toEqual(['read', 'noop'])
    expect(steps[5].rawIds).toEqual(['after', 'output'])
    expect(steps.map(step => step.row.durationMs)).toEqual([undefined, undefined, 25, 2, 25, undefined])
    expect(new Set(steps.flatMap(step => step.rawIds))).toEqual(new Set(rows.map(row => row.id)))
    expect(rows).toEqual(original)
  })

  it('attaches final context to the real result while preserving separate delivery work', () => {
    const rows = [
      model('response', 'call'), context('after', 'session:after', history),
      { ...event('delivery', 'response_delivery'), phase: 'finalize' as const, durationMs: 3, startedAt: 40, endedAt: 43 },
      { ...event('result', 'turn_end', { final_text: 'Final synthetic answer' }), seq: 9, recordedAt: 44 },
    ]
    const original = structuredClone(rows)
    const steps = projectTraceSteps(rows)
    expect(steps.map(step => [step.id, step.category, step.lane])).toEqual([
      ['response', 'model', 'model'], ['delivery', 'output', 'control'], ['result', 'result', 'result'],
    ])
    expect(steps[1].rawIds).toEqual(['delivery'])
    expect(steps[1].row.durationMs).toBe(3)
    expect(steps[2]).toMatchObject({ status: 'success', boundary: true, rawIds: ['after', 'result'] })
    expect(steps[2].row).toBe(rows[3])
    expect(steps[2].row.durationMs).toBeUndefined()
    expect(rows).toEqual(original)
  })

  it('preserves failure and cancellation results and their final context', () => {
    for (const result of [
      event('end-error', 'turn_end', { error: 'Synthetic failure' }),
      event('cancelled', 'turn_cancelled'),
      event('error', 'turn_error'),
    ]) {
      const steps = projectTraceSteps([context('after', 'session:after', history), result])
      expect(steps).toHaveLength(1)
      expect(steps[0]).toMatchObject({ lane: 'result', category: 'result', boundary: true, rawIds: ['after', result.id] })
      expect(steps[0].status).toBe(result.kind === 'turn_cancelled' ? 'cancelled' : 'error')
      expect(steps[0].row.status).toBe('success')
      expect(steps[0].row).toBe(result)
    }
  })

  it('does not absorb final context into an unfinished finalization step', () => {
    for (const kind of ['response_ready', 'finalize']) {
      const rows = [context('after', 'session:after', history), { ...event('work', kind), phase: 'finalize' as const }]
      const steps = projectTraceSteps(rows)
      expect(steps.map(step => step.id)).toEqual(['after', 'work'])
      expect(steps.every(step => step.category !== 'result')).toBe(true)
      expect(steps.every(step => step.label !== 'preparation')).toBe(true)
      expect(steps[1].rawIds).toEqual(['work'])
    }
  })

  it('compares complete message content even when counts and lengths match', () => {
    const steps = projectTraceSteps([
      context('loaded', 'session:loaded', [{ role: 'user', content: 'same' }]),
      context('changed', 'session:sanitized', [{ role: 'user', content: 'diff' }]),
      context('limited', 'session:limited', [{ role: 'user', content: 'diff' }]), model('model', 'call'),
    ])
    expect(steps.map(step => step.id)).toEqual(['changed', 'model'])
    expect(steps[0]).toMatchObject({ label: 'contextAdjusted', change: { beforeId: 'loaded', afterId: 'changed' } })
    expect(steps[0].change?.removedMessages).toBeUndefined()
  })

  it('does not claim unchanged content when payloads are missing, bounded, or redacted', () => {
    for (const loaded of [
      context('loaded', 'session:loaded', undefined),
      { ...context('loaded', 'session:loaded', history), inputTruncated: true },
      context('loaded', 'session:loaded', [{ role: 'user', content: '[REDACTED]' }]),
    ]) {
      const steps = projectTraceSteps([loaded, context('sanitized', 'session:sanitized', history), model('model', 'call')])
      expect(steps.find(step => step.id === 'sanitized')).toMatchObject({ label: 'contextUncertain', change: { uncertain: true } })
      expect(steps.flatMap(step => step.rawIds)).toContain('loaded')
    }
    const proven = projectTraceSteps([
      context('sanitized', 'session:sanitized', undefined),
      context('limited', 'session:limited', undefined, { removed_messages: 2 }),
    ])
    expect(proven.find(step => step.id === 'limited')).toMatchObject({ label: 'contextAdjusted', change: { removedMessages: 2, uncertain: true } })
  })

  it('keeps real image conversion and rejection while folding empty preflight into preparation', () => {
    const rejected = event('reject', 'image_input_preflight', { action: 'reject', image_count: 1 })
    const steps = projectTraceSteps([
      event('empty', 'image_input_preflight', { action: 'project', image_count: 0 }), model('first', 'one'),
      event('project', 'image_input_preflight', { action: 'project', image_count: 1 }), model('second', 'two'), rejected,
    ])
    expect(steps.map(step => step.id)).toEqual(['first', 'project', 'second', 'reject'])
    expect(steps[0].rawIds).toContain('empty')
    expect(steps[1].label).toBe('imageProcessed')
    expect(steps[3]).toMatchObject({ label: 'imageRejected', status: 'error', boundary: true })
    expect(rejected.status).toBe('success')
  })

  it('preserves boundaries and never associates preparation across routing or errors', () => {
    const rows = [
      event('input', 'turn_start'), context('before-route', 'stream:context', history, { call_id: 'same' }),
      event('route', 'router_decision'), model('later', 'same'),
      context('before-error', 'stream:context', history, { call_id: 'last' }),
      { ...event('error', 'turn_error'), status: 'error' as const }, model('last', 'last'), event('unknown', 'future_operation'),
    ]
    const steps = projectTraceSteps(rows)
    expect(steps.find(step => step.id === 'later')?.rawIds).toEqual(['later'])
    expect(steps.find(step => step.id === 'last')?.rawIds).toEqual(['last'])
    expect(steps.filter(step => step.label === 'preparation')).toHaveLength(0)
    expect(steps.find(step => step.id === 'before-route')).toBeDefined()
    expect(steps.find(step => step.id === 'before-error')).toBeDefined()
    expect(steps.find(step => step.id === 'unknown')).toBeDefined()
    expect(steps.filter(step => step.boundary).map(step => step.id)).toEqual(['route', 'error'])
  })

  it('keeps tool transformations and links original output and the actual next model input', () => {
    const toolResult = { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool-one', content: 'Reduced result' }] }
    const steps = projectTraceSteps([
      tool('read', 'tool-one'), event('provider-noop', 'tool_provider_projection_noop', { tool_use_id: 'tool-one' }),
      event('transform', 'tool_projection_applied', { tool_use_id: 'tool-one', original_chars: 100, projected_chars: 20 }),
      model('model', 'next', [toolResult]),
    ])
    expect(steps.map(step => step.id)).toEqual(['read', 'transform', 'model'])
    expect(steps[0].rawIds).toEqual(['read', 'provider-noop'])
    expect(steps[1]).toMatchObject({ label: 'toolResultTransformed', change: { beforeId: 'read', afterId: 'transform', afterInputId: 'model' } })
    expect(steps[1].row.output).not.toHaveProperty('content')
  })

  it('does not attribute tool diagnostics to another tool or cross a boundary', () => {
    const steps = projectTraceSteps([
      tool('other', 'other-tool'), event('noop', 'tool_projection_noop', { tool_use_id: 'missing' }),
      event('route', 'provider_fallback'), tool('later', 'missing'),
    ])
    expect(steps.find(step => step.id === 'other')?.rawIds).toEqual(['other'])
    expect(steps.find(step => step.id === 'later')?.rawIds).toEqual(['later'])
    expect(steps.find(step => step.id === 'noop')?.rawIds).toEqual(['noop'])
    expect(steps.find(step => step.id === 'noop')?.row.status).toBe('success')
  })

  it('retains raw selection anchors as preparation is absorbed by a streamed model call', () => {
    const preparation = [context('loaded', 'session:loaded', history), context('prompt', 'prompt:before', history)]
    const early = projectTraceSteps(preparation)
    expect(early).toHaveLength(1)
    expect(early[0]).toMatchObject({ id: 'preparation:loaded', label: 'preparation', rawIds: ['loaded', 'prompt'] })
    expect(early[0].row.status).toBe('queued')
    expect(early[0].row.durationMs).toBeUndefined()
    const live = { ...model('model', 'call'), kind: 'llm_progress', status: 'running' as const, durationMs: undefined, endedAt: undefined }
    const later = projectTraceSteps([...preparation, live])
    expect(later).toHaveLength(1)
    expect(later[0].rawIds).toEqual(['loaded', 'prompt', 'model'])
    expect(later[0].row).toBe(live)
    const complete = projectTraceSteps([...preparation, model('model', 'call')])
    expect(complete[0].id).toBe(later[0].id)
  })

  it('shows a single preparation item while a real context adjustment is streaming', () => {
    const steps = projectTraceSteps([
      context('loaded', 'session:loaded', history),
      context('changed', 'session:sanitized', []),
      context('limited', 'session:limited', []),
      context('prompt', 'prompt:before', history),
    ])
    expect(steps.filter(step => step.label === 'preparation')).toHaveLength(1)
    expect(steps.find(step => step.label === 'preparation')?.rawIds).toEqual(['loaded', 'limited', 'prompt'])
    expect(steps.find(step => step.id === 'changed')).toMatchObject({ label: 'contextAdjusted', change: { removedMessages: 1 } })
  })

  it('keeps message key ordering irrelevant while treating incomplete message arrays as uncertain', () => {
    const steps = projectTraceSteps([
      context('loaded', 'session:loaded', [{ role: 'user', content: 'hello' }]),
      context('sanitized', 'session:sanitized', [{ content: 'hello', role: 'user' }]),
      context('limited', 'session:limited', [{ role: 'user' }]),
      model('model', 'call'),
    ])
    expect(steps.find(step => step.id === 'sanitized')).toBeUndefined()
    expect(steps.find(step => step.id === 'limited')).toMatchObject({ label: 'contextUncertain' })
  })

  it('preserves producer-confirmed metadata changes even when visible message bodies match', () => {
    const steps = projectTraceSteps([
      context('loaded', 'session:loaded', history),
      context('sanitized', 'session:sanitized', history, { sanitize: { metadata_keys_removed: 2 } }),
      model('model', 'call'),
    ])
    expect(steps.find(step => step.id === 'sanitized')).toMatchObject({ label: 'contextAdjusted' })
  })

  it('never marks orphan output or completed preparation as a queued model call', () => {
    for (const rows of [
      [context('after', 'session:after', history)],
      [event('noop', 'tool_projection_noop', { tool_use_id: 'missing' })],
      [context('orphan', 'stream:context', history, { call_id: 'missing' }), event('end', 'turn_end')],
    ]) {
      const steps = projectTraceSteps(rows)
      expect(steps.every(step => step.label !== 'preparation')).toBe(true)
      expect(steps.every(step => step.row.status !== 'queued')).toBe(true)
      expect(steps.flatMap(step => step.rawIds)).toEqual(rows.map(row => row.id))
    }
  })

  it('attaches preparation to its failed operation without crossing that failure', () => {
    const failure = { ...model('failed', 'failed-call'), kind: 'llm_error', status: 'error' as const }
    const steps = projectTraceSteps([
      context('loaded', 'session:loaded', history),
      context('stream', 'stream:context', history, { call_id: 'failed-call' }),
      context('other-stream', 'stream:context', history, { call_id: 'later-call' }),
      failure,
      model('later', 'later-call'),
    ])
    expect(steps.find(step => step.id === 'failed')).toMatchObject({ boundary: true, rawIds: ['loaded', 'stream', 'failed'] })
    expect(steps.find(step => step.id === 'other-stream')).toBeDefined()
    expect(steps.find(step => step.id === 'later')?.rawIds).toEqual(['later'])
    expect(steps.find(step => step.id === 'failed')?.row).toBe(failure)
  })

  it('uses an exact call identity for snapshots sorted after the paired model start', () => {
    const steps = projectTraceSteps([
      model('first', 'one'), context('first-input', 'stream:context', history, { call_id: 'one' }),
      model('second', 'two'),
    ])
    expect(steps.map(step => step.id)).toEqual(['first', 'second'])
    expect(steps[0].rawIds).toEqual(['first', 'first-input'])
  })
})
