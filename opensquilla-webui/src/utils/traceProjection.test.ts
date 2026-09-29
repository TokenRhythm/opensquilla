import { describe, expect, it } from 'vitest'
import { detailsFromApi, groupTraceRowsForTimeline, isTraceTerminal, layoutTraceTimeline, projectTraceEvents, projectionFromApi, traceDisplayItems, traceEventCategory, traceTerminalStatus, traceTimelineSelection } from './traceProjection'
import type { TraceSpan } from '@/types/traceView'

describe('projectTraceEvents', () => {
  it('groups structured events into phase bands and keeps routing modes', () => {
    const projection = projectTraceEvents([
      { kind: 'turn_start', seq: 1, ts: 1000, context: { trace_id: 'trace-1', run_id: 'run-1' } },
      { kind: 'route.resolved', seq: 2, ts: 1100, context: { trace_id: 'trace-1' }, attrs: { requested_mode: 'router', effective_mode: 'ensemble' } },
      { kind: 'model.start', seq: 3, ts: 1200, context: { trace_id: 'trace-1' }, attrs: { provider: 'demo', model: 'small', role: 'proposer' } },
      { kind: 'tool.end', seq: 4, ts: 1300, context: { trace_id: 'trace-1' }, attrs: { status: 'success', tool_name: 'search' } },
      { kind: 'turn_end', seq: 5, ts: 1400, context: { trace_id: 'trace-1', run_id: 'run-1' } },
    ])

    expect(projection?.traceId).toBe('trace-1')
    expect(projection?.runId).toBe('run-1')
    expect(projection?.requestedMode).toBe('router')
    expect(projection?.effectiveMode).toBe('ensemble')
    expect(projection?.complete).toBe(true)
    expect(projection?.phases.map(phase => phase.name)).toEqual([
      'intake', 'routing', 'model_execution', 'tool_execution', 'finalize',
    ])
  })

  it('ignores malformed and unrelated records while selecting the latest trace', () => {
    const projection = projectTraceEvents([
      'not json',
      { kind: 'turn_end', context: { trace_id: 'old' } },
      { kind: 'turn_start', context: { trace_id: 'new' }, seq: 8 },
    ])
    expect(projection?.traceId).toBe('new')
    expect(projection?.spans).toHaveLength(1)
  })

  it('waits for an explicit run result after successful finalization or response preparation', () => {
    for (const kind of ['response_ready', 'finalize', 'finalize_completed', 'llm_error']) {
      const unfinished = projectTraceEvents([
        { kind: 'turn_start', context: { trace_id: 'terminal-test' }, seq: 1 },
        { kind, context: { trace_id: 'terminal-test' }, seq: 2, attrs: { status: kind === 'llm_error' ? 'error' : 'success' } },
      ])!
      expect(unfinished.complete).toBe(false)
      expect(unfinished.status).not.toBe('success')
    }
    const recovered = projectTraceEvents([
      { kind: 'llm_error', context: { trace_id: 'recovered' }, seq: 1 },
      { kind: 'turn_end', context: { trace_id: 'recovered' }, seq: 2 },
    ])!
    expect(recovered).toMatchObject({ status: 'success', complete: true })
    const failed = projectTraceEvents([
      { kind: 'turn_end', context: { trace_id: 'failed-result' }, payload: { error: 'Synthetic failure' } },
    ])!
    expect(failed).toMatchObject({ status: 'error', complete: true })
    expect(failed.spans[0].status).toBe('error')
  })

  it('anchors completed response bars at their end timestamp', () => {
    const details = detailsFromApi({
      trace_id: 'trace-1',
      rows: [{
        id: 'step:1',
        kind: 'llm_response',
        phase: 'model_execution',
        status: 'success',
        ts: '2026-01-01T00:00:02.000Z',
        started_ts: '2026-01-01T00:00:00.900Z',
        duration_ms: 820,
      }],
    })
    expect(details?.rows[0].endedAt).toBe(Date.parse('2026-01-01T00:00:02.000Z'))
    expect(details?.rows[0].startedAt).toBe(Date.parse('2026-01-01T00:00:00.900Z'))
  })

  it('groups adjacent context checkpoints into one timeline stage', () => {
    const details = detailsFromApi({
      trace_id: 'trace-context',
      rows: [
        { id: 'input', kind: 'turn_start', phase: 'intake', status: 'running', seq: 1, ts: 1000 },
        { id: 'ctx-1', kind: 'context_stage', phase: 'context', status: 'running', seq: 2, ts: 1100, stage: 'session:loaded' },
        { id: 'ctx-2', kind: 'context_stage', phase: 'context', status: 'running', seq: 3, ts: 1110, stage: 'prompt:before' },
        { id: 'model', kind: 'llm_response', phase: 'model_execution', status: 'success', seq: 4, ts: 2000, duration_ms: 700 },
      ],
    })
    const rows = groupTraceRowsForTimeline(details?.rows || [])
    expect(rows.map(row => row.id)).toEqual(['input', 'context-group:ctx-1:ctx-2', 'model'])
    expect(rows[1].attrs?.context_count).toBe(2)
    expect(rows[1].summary).toBe('session:loaded → prompt:before')
    expect(rows[1].durationMs).toBeUndefined()
    expect(rows[1].startedAt).toBe(1100)
    expect(rows[1].endedAt).toBe(1110)
  })

  it('prefers monotonic elapsed timestamps when the backend provides them', () => {
    const details = detailsFromApi({
      trace_id: 'trace-2',
      rows: [{
        id: 'step:1',
        kind: 'llm_response',
        phase: 'model_execution',
        status: 'success',
        ts: '2026-01-01T00:00:02.000Z',
        elapsed_ms: 2200,
        started_elapsed_ms: 800,
        ended_elapsed_ms: 2200,
        duration_ms: 1400,
      }],
    })
    expect(details?.rows[0].startedAt).toBe(800)
    expect(details?.rows[0].endedAt).toBe(2200)
  })

  it('keeps legacy elapsed rows in the relative millisecond coordinate system', () => {
    const details = detailsFromApi({
      trace_id: 'trace-legacy-timing',
      rows: [{
        id: 'step:1',
        kind: 'llm_response',
        phase: 'model_execution',
        status: 'success',
        ts: '2026-01-01T00:00:02.000Z',
        elapsed_ms: 2200,
        duration_ms: 1400,
      }],
    })
    expect(details?.rows[0].startedAt).toBe(800)
    expect(details?.rows[0].endedAt).toBe(2200)
  })

  it('preserves an in-progress call start without inventing completion or measured duration', () => {
    const call = { id: 'step:live', span_id: 'step:live', kind: 'llm_progress', phase: 'model_execution', status: 'running', seq: 8, order_seq: 2, input_seq: 2, elapsed_ms: 450, started_elapsed_ms: 50, output: { text: 'Partial', partial: true } }
    const details = detailsFromApi({ trace_id: 'live', rows: [{ id: 'context', kind: 'context_stage', phase: 'context', seq: 5, elapsed_ms: 100 }, call] })!
    expect(details.rows.map(row => row.id)).toEqual(['step:live', 'context'])
    expect(details.rows[0]).toMatchObject({ startedAt: 50, recordedAt: 450, seq: 8, inputSeq: 2, output: { text: 'Partial', partial: true } })
    expect(details.rows[0].endedAt).toBeUndefined()
    expect(details.rows[0].durationMs).toBeUndefined()
    const projection = projectionFromApi({ trace_id: 'live', spans: [call] })!
    expect(projection.spans[0].startedAt).toBe(50)
    expect(projection.spans[0].endedAt).toBeUndefined()
    expect(projection.spans[0].durationMs).toBeUndefined()
  })

  it('preserves approval waiting and resolved timing without inventing a duration', () => {
    const waiting = { id: 'approval:a', kind: 'approval_wait', phase: 'approval_sandbox', status: 'running', elapsed_ms: 120, started_elapsed_ms: 100 }
    const completed = { ...waiting, kind: 'approval_resolved', status: 'success', elapsed_ms: 650, ended_elapsed_ms: 640 }
    for (const normalize of [
      (span: unknown) => detailsFromApi({ trace_id: 'approval-test', rows: [span] })!.rows[0],
      (span: unknown) => projectionFromApi({ trace_id: 'approval-test', spans: [span] })!.spans[0],
    ]) {
      expect(normalize(waiting)).toMatchObject({ startedAt: 100, status: 'running' })
      expect(normalize(waiting).endedAt).toBeUndefined()
      expect(normalize(completed)).toMatchObject({ startedAt: 100, endedAt: 640, recordedAt: 650 })
      expect(normalize(completed).durationMs).toBeUndefined()
    }
  })
})

describe('operation lanes and run boundaries', () => {
  const row = (id: string, kind: string, phase: TraceSpan['phase'], summary?: string): TraceSpan => ({
    id, kind, phase, summary, title: id, status: 'success', recordedAt: 10, seq: Number(id) || 1,
  })

  it('keeps budget and image preparation before the first model call in input', () => {
    const items = traceDisplayItems([
      row('1', 'turn_start', 'intake'),
      row('2', 'router_decision', 'routing'),
      row('3', 'agent_runtime_budget', 'model_execution'),
      row('4', 'image_input_preflight', 'unknown'),
      row('5', 'context_stage', 'context', 'session:loaded'),
      row('6', 'llm_request', 'model_execution'),
      row('7', 'context_stage', 'context', 'stream:context'),
      row('8', 'tool_projection_noop', 'tool_execution'),
    ])
    expect(items.map(item => [item.lane, item.category])).toEqual([
      ['intake', 'input'], ['control', 'routing'], ['intake', 'context'],
      ['intake', 'context'], ['intake', 'context'], ['model', 'model'],
      ['intake', 'context'], ['intake', 'context'],
    ])
  })

  it('separates model responses from tool execution and preserves recorded rows', () => {
    const rows = [
      row('1', 'turn_start', 'intake'),
      row('2', 'routing_decision', 'routing'),
      row('3', 'context_stage', 'context', 'session:loaded'),
      row('4', 'context_stage', 'context', 'stream:context'),
      row('5', 'llm_response', 'model_execution'),
      row('6', 'tool_response', 'tool_execution'),
      row('7', 'context_stage', 'context', 'stream:context'),
      row('8', 'llm_response', 'model_execution'),
      row('9', 'context_stage', 'context', 'session:after'),
      row('10', 'turn_end', 'finalize'),
    ]
    const before = structuredClone(rows)
    const items = traceDisplayItems(rows)
    expect(items.map(item => item.lane)).toEqual([
      'intake', 'control', 'intake', 'intake', 'model', 'tool', 'intake', 'model', 'intake', 'result',
    ])
    expect(items.map(item => item.index)).toEqual(rows.map((_, index) => index))
    expect(items.every((item, index) => item.row === rows[index])).toBe(true)
    expect(rows).toEqual(before)
  })

  it('keeps boundary types distinct from model and tool work', () => {
    const rows = [
      row('route', 'routing_decision', 'routing'),
      row('retry', 'provider_retry_wait', 'model_execution'),
      row('fallback', 'provider_fallback', 'routing'),
      row('approval', 'tool_approval', 'tool_execution'),
      row('compact', 'context_compaction', 'context'),
      row('child', 'subagent_dispatch', 'subagent'),
      row('unknown', 'future_event', 'unknown'),
      { ...row('model', 'llm_response', 'model_execution'), attrs: { retry_of: 'earlier', summary: 'fallback' } },
      { ...row('tool', 'tool_response', 'tool_execution'), toolName: 'approval_inspector' },
    ]
    const items = traceDisplayItems(rows)
    expect(items.map(item => item.category)).toEqual([
      'routing', 'retry', 'fallback', 'approval', 'maintenance', 'subagent', 'unknown', 'model', 'tool',
    ])
    expect(items.map(item => item.boundary)).toEqual([true, true, true, true, true, true, false, false, false])
    expect(items.map(item => item.lane)).toEqual(['control', 'control', 'control', 'control', 'control', 'control', 'control', 'model', 'tool'])
    expect(traceEventCategory(row('error', 'llm_error', 'finalize'))).toBe('model')
  })

  it('never groups checkpoints across boundaries or display lanes', () => {
    const rows = [
      row('a', 'context_stage', 'context', 'session:loaded'),
      row('b', 'context_stage', 'context', 'prompt:before'),
      row('route', 'routing_decision', 'routing'),
      row('first-stream', 'context_stage', 'context', 'stream:context'),
      row('model', 'llm_response', 'model_execution'),
      row('c', 'context_stage', 'context', 'stream:context'),
      row('d', 'context_stage', 'context', 'session:after'),
    ]
    const grouped = groupTraceRowsForTimeline(rows)
    expect(grouped.map(item => item.id)).toEqual(['context-group:a:b', 'route', 'first-stream', 'model', 'c', 'd'])
    expect(traceDisplayItems(grouped).map(item => item.lane)).toEqual(['intake', 'control', 'intake', 'model', 'intake', 'intake'])
  })

  it('keeps actual finalization work distinct from terminal result markers', () => {
    const rows = [
      row('ready', 'response_ready', 'finalize'),
      { ...row('delivery', 'response_delivery', 'finalize'), startedAt: 110, endedAt: 135, durationMs: 25 },
      row('finalize', 'finalize', 'finalize'),
      row('end', 'turn_end', 'finalize'),
    ]
    const original = structuredClone(rows)
    expect(traceDisplayItems(rows).map(item => [item.category, item.lane, item.boundary])).toEqual([
      ['output', 'control', false], ['output', 'control', false], ['output', 'control', false], ['result', 'result', true],
    ])
    expect(rows.map(isTraceTerminal)).toEqual([false, false, false, true])
    expect(rows).toEqual(original)
  })

  it('uses terminal evidence without altering the original payload or recorded status', () => {
    const results = [
      row('end', 'turn_end', 'finalize'),
      { ...row('error-payload', 'turn_end', 'finalize'), output: { error: 'Synthetic error' } },
      { ...row('error-state', 'turn_end', 'finalize'), status: 'error' as const },
      row('error-kind', 'turn_error', 'unknown'),
      { ...row('cancelled', 'turn_cancelled', 'finalize'), output: { error: 'Interrupted' } },
      { ...row('empty-error', 'turn_end', 'finalize'), output: { error: null } },
      row('not-end', 'provider_turn_end_notice', 'unknown'),
    ]
    const original = structuredClone(results)
    expect(results.map(traceTerminalStatus)).toEqual(['success', 'error', 'error', 'error', 'cancelled', 'success', 'success'])
    expect(results.map(isTraceTerminal)).toEqual([true, true, true, true, true, true, false])
    expect(traceEventCategory(results[6])).not.toBe('result')
    expect(results).toEqual(original)
  })

  it('does not restore obsolete display lanes from grouped checkpoint metadata', () => {
    const rows = ['execution', 'output', 'control', 'result'].map((lane, index) => ({
      ...row(`legacy-${index}`, 'context_stage_group', 'context'), attrs: { grouped: true, display_lane: lane },
    }))
    expect(traceDisplayItems(rows).map(item => item.lane)).toEqual(['intake', 'intake', 'intake', 'intake'])
  })

  it('does not invent a routing event from routing modes or attempt links', () => {
    const projection = projectionFromApi({
      trace_id: 'synthetic-mode', requested_mode: 'router', effective_mode: 'single',
      spans: [{ span_id: 'model', kind: 'llm_response', phase: 'model_execution', attrs: { fallback_of: 'earlier' } }],
    })!
    expect(projection.requestedMode).toBe('router')
    expect(projection.effectiveMode).toBe('single')
    expect(traceDisplayItems(projection.spans)).toMatchObject([{ category: 'model', boundary: false }])
  })
})

describe('recorded timeline layout', () => {
  const point = (id: string, time: number): TraceSpan => ({ id, kind: 'input_received', phase: 'intake', title: id, status: 'success', startedAt: time, recordedAt: time, elapsedMs: time })

  it('stacks same-millisecond points vertically without moving their timestamp', () => {
    const rows = [point('first', 0), point('second', 0), point('nearby', 1), point('last', 1000)]
    const layout = layoutTraceTimeline(rows, 500)
    expect(layout.items.map(item => [item.start, item.end])).toEqual([[0, 0], [0, 0], [1, 1], [1000, 1000]])
    expect(layout.items.slice(0, 3).map(item => item.stack)).toEqual([0, 1, 2])
    expect(rows.map(row => row.recordedAt)).toEqual([0, 0, 1, 1000])
  })

  it('preserves real parallel intervals and same-time final checkpoints', () => {
    const rows: TraceSpan[] = [
      { ...point('tool', 200), kind: 'tool_response', phase: 'tool_execution', startedAt: 100, endedAt: 200, durationMs: 100 },
      { ...point('model', 250), kind: 'llm_response', phase: 'model_execution', startedAt: 150, endedAt: 250, durationMs: 100 },
      { ...point('context', 250), kind: 'context_stage', phase: 'context' },
      { ...point('output', 250), kind: 'turn_end', phase: 'finalize' },
    ]
    const layout = layoutTraceTimeline(rows)
    expect(layout.items.map(item => [item.start, item.end])).toEqual([[100, 200], [150, 250], [250, 250], [250, 250]])
    expect(layout.items[1].start).toBeLessThan(layout.items[0].end)
    expect(layout.items.map(item => item.lane)).toEqual(['tool', 'model', 'intake', 'result'])
  })

  it('renders a terminal record as a marker even if it reports total run duration', () => {
    const result = { ...point('result', 1000), kind: 'turn_end', phase: 'finalize' as const, startedAt: 0, endedAt: 1000, durationMs: 1000 }
    expect(layoutTraceTimeline([result]).items).toMatchObject([{ lane: 'result', kind: 'point', start: 1000, end: 1000 }])
    expect(result.durationMs).toBe(1000)
  })

  it('keeps sampling ranges distinct from measured duration and selects the clicked group', () => {
    const rows: TraceSpan[] = [
      { ...point('a', 10), kind: 'context_stage', phase: 'context' },
      { ...point('b', 11), kind: 'context_stage', phase: 'context' },
      { ...point('model', 100), kind: 'llm_response', phase: 'model_execution', startedAt: 12, endedAt: 100, durationMs: 88 },
      { ...point('c', 100), kind: 'context_stage', phase: 'context' },
      { ...point('d', 100), kind: 'context_stage', phase: 'context' },
    ]
    const grouped = groupTraceRowsForTimeline(rows)
    const layout = layoutTraceTimeline(grouped)
    expect(grouped[0].durationMs).toBeUndefined()
    expect(grouped[2].durationMs).toBeUndefined()
    expect(layout.items.map(item => [item.kind, item.start, item.end])).toEqual([['checkpoints', 10, 11], ['span', 12, 100], ['checkpoints', 100, 100]])
    expect(traceTimelineSelection(grouped[2], rows)).toBe('c')
    expect(traceTimelineSelection(rows[4], rows)).toBe('d')
  })

  it('does not invent a timestamp for unrecorded events', () => {
    expect(layoutTraceTimeline([{ id: 'unknown', kind: 'event', phase: 'unknown', title: 'unknown', status: 'unknown' }]).items).toEqual([])
  })
})
