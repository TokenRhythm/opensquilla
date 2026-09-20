// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import { detailsFromApi } from '@/utils/traceProjection'
import type { TraceProjection, TraceSpan } from '@/types/traceView'
import TraceTimeline from './TraceTimeline.vue'

describe('TraceTimeline sequence display', () => {
  it('shows a safe timeline and a permission notice without implying raw diagnostics is disabled', async () => {
    const projection: TraceProjection = {
      traceId: 'read-only', status: 'running', complete: false, phases: [],
      spans: [{ id: 'model', kind: 'llm_response', phase: 'model_execution', title: 'Model', status: 'running', durationMs: 25 }],
    }
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const host = document.createElement('div')
    const app = createApp(TraceTimeline, { projection, detailsAvailable: false, detailsReason: 'access_denied', compact: true })
    app.use(i18n)
    app.mount(host)
    try {
      expect(host.querySelector('[data-event-id="model"]')).not.toBeNull()
      expect(host.querySelector('.trace-details__empty')?.textContent).toContain('管理员权限')
      expect(host.querySelector('.trace-details__empty')?.textContent).not.toContain('原始诊断')
      expect(host.querySelector('.trace-inspector__raw')).toBeNull()
    } finally { app.unmount() }
  })

  it('shows asynchronous details in equal-width slots independent of duration and timestamps', async () => {
    const details = detailsFromApi({ trace_id: 'deferred', rows: [
      { id: 'input', kind: 'input_received', phase: 'intake', elapsed_ms: 0 },
      { id: 'tool', kind: 'tool_response', phase: 'tool_execution', started_elapsed_ms: 1, ended_elapsed_ms: 2, elapsed_ms: 2, duration_ms: 1 },
      { id: 'model', kind: 'llm_response', phase: 'model_execution', started_elapsed_ms: 5, ended_elapsed_ms: 600005, elapsed_ms: 600005, duration_ms: 600000 },
      { id: 'untimed', kind: 'event', phase: 'unknown' },
      { id: 'output', kind: 'turn_end', phase: 'finalize', elapsed_ms: 600006 },
    ] })!
    const state = reactive<{ details?: TraceSpan[] }>({})
    const projection: TraceProjection = { traceId: 'deferred', status: 'success', complete: true, spans: [], phases: [] }
    const host = document.createElement('div')
    const app = createApp({ setup: () => () => h(TraceTimeline, { projection, details: state.details }) })
    app.use(i18n)
    app.mount(host)
    try {
      expect(host.querySelector('[data-event-id]')).toBeNull()
      state.details = details.rows
      await nextTick()
      const event = (id: string) => host.querySelector<HTMLButtonElement>(`[data-event-id="${id}"]`)!
      expect([...host.querySelectorAll('[data-lane]')].map(lane => lane.getAttribute('data-lane'))).toEqual(['intake', 'model', 'tool'])
      expect(host.querySelectorAll('[data-event-id]')).toHaveLength(5)
      expect(event('tool').closest('[data-lane]')?.getAttribute('data-lane')).toBe('tool')
      expect(event('model').closest('[data-lane]')?.getAttribute('data-lane')).toBe('model')
      expect(event('tool').dataset.category).toBe('tool')
      expect(event('model').dataset.category).toBe('model')
      expect(event('untimed').closest('.trace-control-track')).not.toBeNull()
      expect(event('output').closest('[data-lane]')).toBeNull()
      expect(event('output').dataset.terminal).toBe('true')
      expect(host.querySelector('[data-result-id="output"]')).not.toBeNull()
      expect([...host.querySelectorAll('[data-event-id]')].every(block => !block.textContent?.trim())).toBe(true)
      expect(parseFloat(event('tool').style.width)).toBeCloseTo(17.6)
      expect(event('model').style.width).toBe(event('tool').style.width)
      expect(event('untimed')).not.toBeNull()
      expect(parseFloat(event('model').style.left) - parseFloat(event('tool').style.left)).toBeCloseTo(20)
      expect(parseFloat(event('untimed').style.left) - parseFloat(event('model').style.left)).toBeCloseTo(20)
      expect(event('output').style.left).toContain('%')
      expect(parseFloat(event('output').style.left) + parseFloat(event('output').style.width)).toBeLessThan(100)
      const positions = ['input', 'tool', 'model', 'untimed', 'output'].map(id => event(id).style.left)
      expect(event('tool').dataset.timeStart).toBe('1')
      expect(event('tool').dataset.timeEnd).toBe('2')
      expect(event('model').title).toContain('600000ms')

      state.details = details.rows.map(row => row.id === 'tool'
        ? { ...row, startedAt: 40000, endedAt: 70000, recordedAt: 70000, durationMs: 30000 }
        : row.id === 'model'
          ? { ...row, startedAt: 3, endedAt: 4, recordedAt: 4, durationMs: 1 }
          : row)
      await nextTick()
      expect(['input', 'tool', 'model', 'untimed', 'output'].map(id => event(id).style.left)).toEqual(positions)
      expect(event('tool').style.width).toBe(event('model').style.width)
      state.details = undefined
      await nextTick()
      expect(host.querySelector('[data-event-id]')).toBeNull()
    } finally {
      app.unmount()
    }
  })

  it('keeps the active step selected while output grows and new steps fit the same viewport', async () => {
    const request: TraceSpan = { id: 'step:active', kind: 'llm_request', phase: 'model_execution', title: 'Model', status: 'running', startedAt: 10, elapsedMs: 10, input: 'request' }
    const projection: TraceProjection = { traceId: 'live', status: 'running', complete: false, spans: [], phases: [] }
    const state = reactive({ rows: [request] })
    const host = document.createElement('div')
    const app = createApp({ setup: () => () => h(TraceTimeline, { projection, details: state.rows }) })
    app.use(i18n)
    app.mount(host)
    try {
      const active = () => host.querySelector<HTMLButtonElement>('[data-event-id="step:active"]')!
      active().click()
      await nextTick()
      expect(active().dataset.timeEnd).toBeUndefined()
      state.rows = [{ ...request, kind: 'llm_progress', recordedAt: 500, elapsedMs: 500, output: { text: 'Partial answer' } }]
      await nextTick()
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Partial answer')
      expect(active().classList.contains('trace-event--running')).toBe(true)
      const firstWidth = parseFloat(active().style.width)
      state.rows = [
        { ...request, kind: 'llm_response', status: 'success', endedAt: 900, durationMs: 890, output: { text: 'Final answer' } },
        ...Array.from({ length: 49 }, (_, index): TraceSpan => ({ id: `tool-${index}`, kind: 'tool_response', phase: 'tool_execution', title: 'Tool', status: 'success', durationMs: 1 })),
      ]
      await nextTick()
      expect(host.querySelectorAll('[data-event-id]')).toHaveLength(50)
      expect(parseFloat(active().style.width)).toBeLessThan(firstWidth)
      expect(active().classList.contains('trace-event--selected')).toBe(true)
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Final answer')
      for (const block of host.querySelectorAll<HTMLButtonElement>('[data-event-id]')) {
        expect(block.style.width).toContain('%')
        expect(parseFloat(block.style.left) + parseFloat(block.style.width)).toBeLessThanOrEqual(100)
      }
    } finally {
      app.unmount()
    }
  })

  it('uses one concise projection for blocks and ledger while keeping original payload selection', async () => {
    const messages = [{ role: 'user', content: [{ type: 'text', text: 'Sample input' }] }]
    const records = [
      { id: 'input', kind: 'turn_start', phase: 'intake', input: { message: 'Sample input' } },
      { id: 'report', kind: 'prompt_report', phase: 'context', input: { message_count: 1 } },
      { id: 'route', kind: 'router_decision', phase: 'routing' },
      { id: 'budget', kind: 'agent_runtime_budget', phase: 'context' },
      { id: 'image', kind: 'image_input_preflight', phase: 'context', output: { action: 'project', image_count: 0 } },
      ...['session:loaded', 'session:sanitized', 'session:limited', 'prompt:before', 'prompt:images', 'stream:context'].map((stage, index) => ({ id: `prep-${index}`, kind: 'context_stage', phase: 'context', stage, input: { stage, messages, ...(stage === 'stream:context' ? { call_id: 'model-1' } : {}) } })),
      { id: 'model-1', kind: 'llm_response', call_id: 'model-1', phase: 'model_execution', input: { messages }, output: { text: 'Reading' }, duration_ms: 101 },
      { id: 'tool', kind: 'tool_response', call_id: 'tool-1', phase: 'tool_execution', tool_name: 'read_file', output: { result: 'File contents' }, duration_ms: 2 },
      { id: 'noop', kind: 'tool_projection_noop', call_id: 'tool-1', phase: 'context', output: { tool_use_id: 'tool-1', original_chars: 13 } },
      { id: 'next-context', kind: 'context_stage', phase: 'context', stage: 'stream:context', input: { messages, call_id: 'model-2' } },
      { id: 'model-2', kind: 'llm_response', call_id: 'model-2', phase: 'model_execution', input: { messages }, output: { text: 'Done' }, duration_ms: 202 },
      { id: 'after', kind: 'context_stage', phase: 'context', stage: 'session:after', input: { messages } },
      { id: 'output', kind: 'turn_end', phase: 'finalize', output: { final_text: 'Done' } },
    ]
    const details = detailsFromApi({ trace_id: 'semantic', rows: records.map((row, index) => ({ ...row, status: 'success', seq: index + 1, elapsed_ms: index * 100 })) })!
    const projection: TraceProjection = { traceId: 'semantic', status: 'success', complete: true, spans: details.rows, phases: [] }
    const selected: TraceSpan[] = []
    const state = reactive<{ payload?: { rowId: string; input?: unknown } }>({})
    const host = document.createElement('div')
    document.body.appendChild(host)
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const app = createApp({ setup: () => () => h(TraceTimeline, { projection, details: details.rows, allowFullPayload: true, fullPayload: state.payload, onSelect: row => selected.push(row) }) })
    app.use(i18n)
    app.mount(host)
    try {
      const ids = ['input', 'route', 'model-1', 'tool', 'model-2']
      expect([...host.querySelectorAll('[data-step-id]')].map(row => row.getAttribute('data-step-id'))).toEqual(ids)
      expect(host.querySelectorAll('[data-event-id]')).toHaveLength(6)
      expect(host.querySelector('.trace-visual__count')?.textContent).toContain('5 个步骤 · 1 个结束标记 · 18 条关联记录')
      host.querySelector<HTMLButtonElement>('[data-step-id="model-1"]')!.click()
      await nextTick()
      expect(selected[selected.length - 1]?.id).toBe('model-1')
      expect(host.querySelector('.trace-inspector__records')).not.toBeNull()
      host.querySelector<HTMLButtonElement>('[data-record-id="prep-4"]')!.click()
      await nextTick()
      expect(selected[selected.length - 1]?.id).toBe('prep-4')
      expect(selected[selected.length - 1]?.seq).toBe(10)
      expect(host.querySelector('[data-step-id="model-1"]')?.getAttribute('aria-pressed')).toBe('true')
      state.payload = { rowId: 'prep-4', input: { messages: [{ role: 'user', content: 'Complete source payload' }] } }
      await nextTick()
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Complete source payload')
      host.querySelector<HTMLButtonElement>('[data-step-id="tool"]')!.click()
      await nextTick()
      expect(host.querySelector('[data-record-id="noop"]')).not.toBeNull()
      expect(host.querySelector('.trace-inspector__timing')?.textContent).toContain('2ms')
      host.querySelector<HTMLButtonElement>('[data-result-id="output"]')!.click()
      await nextTick()
      expect(host.querySelector('[data-record-id="after"]')).not.toBeNull()
      expect(host.querySelector<HTMLButtonElement>('[data-event-id="model-1"]')?.title).toContain('101ms')
      expect(host.querySelector<HTMLButtonElement>('[data-event-id="model-2"]')?.title).toContain('202ms')
    } finally {
      app.unmount()
      host.remove()
    }
  })

  it('keeps a routing boundary selectable without adding its cost to a model call', async () => {
    const rows: TraceSpan[] = [
      { id: 'input', kind: 'turn_start', phase: 'intake', title: 'Input', status: 'success', elapsedMs: 0 },
      { id: 'route', kind: 'routing_decision', phase: 'routing', title: 'Route', status: 'success', durationMs: 5, startedAt: 1, endedAt: 6, output: { selected_model: 'model-a', reason: 'capability', requested_mode: 'squilla_router', effective_mode: 'single' } },
      { id: 'model', kind: 'llm_response', phase: 'model_execution', title: 'Model', status: 'success', durationMs: 100, startedAt: 6, endedAt: 106 },
      { id: 'fallback', kind: 'provider_fallback', phase: 'routing', title: 'Fallback', status: 'success', elapsedMs: 107, output: { previous_model: 'model-a', next_model: 'model-b', reason: 'timeout' } },
    ]
    const projection: TraceProjection = { traceId: 'boundary', status: 'running', complete: false, spans: rows, phases: [] }
    const host = document.createElement('div')
    const app = createApp(TraceTimeline, { projection, details: rows, compact: true })
    app.use(i18n)
    app.mount(host)
    try {
      const route = host.querySelector<HTMLButtonElement>('[data-event-id="route"]')!
      expect(route.closest('[data-lane]')).toBeNull()
      expect(route.closest('.trace-control-track')).not.toBeNull()
      expect(route.classList.contains('trace-event--measured-control')).toBe(true)
      expect(route.dataset.boundary).toBe('true')
      expect(route.textContent?.trim()).toBe('')
      expect(host.querySelectorAll('.trace-boundary-guides i')).toHaveLength(2)
      expect(host.querySelector<HTMLButtonElement>('[data-event-id="model"]')?.title).toContain('100ms')
      route.click()
      await nextTick()
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('model-a')
      expect(host.querySelector('.trace-inspector [data-fact="reason"]')?.textContent).toContain('capability')
      host.querySelector<HTMLButtonElement>('[data-event-id="fallback"]')!.click()
      await nextTick()
      expect(host.querySelector('.trace-inspector [data-fact="previous_model"]')?.textContent).toContain('model-a')
      expect(host.querySelector('.trace-inspector [data-fact="next_model"]')?.textContent).toContain('model-b')
      expect(host.querySelector('.trace-inspector [data-fact="reason"]')?.textContent).toContain('timeout')
    } finally {
      app.unmount()
    }
  })

  it('keeps an inspected preparation record selected when a streamed model call takes ownership', async () => {
    const preparation: TraceSpan = { id: 'prep', kind: 'context_stage', phase: 'context', title: 'Context', status: 'success', seq: 2, summary: 'stream:context', input: { call_id: 'call-a', messages: [] }, recordedAt: 10 }
    const state = reactive({ rows: [preparation] })
    const projection: TraceProjection = { traceId: 'preparing', status: 'running', complete: false, spans: [], phases: [] }
    const selected: TraceSpan[] = []
    const host = document.createElement('div')
    const app = createApp({ setup: () => () => h(TraceTimeline, { projection, details: state.rows, onSelect: row => selected.push(row) }) })
    app.use(i18n)
    app.mount(host)
    try {
      expect(host.querySelectorAll('[data-step-id]')).toHaveLength(1)
      const preparing = host.querySelector<HTMLButtonElement>('[data-event-id]')!
      preparing.click()
      await nextTick()
      expect(preparing.title).toContain('准备模型输入')
      expect(preparing.classList.contains('trace-event--running')).toBe(false)
      expect(selected[selected.length - 1].id).toBe('prep')
      state.rows.push({ id: 'model', kind: 'llm_request', phase: 'model_execution', title: 'Model', status: 'running', seq: 3, logicalCallId: 'call-a', startedAt: 12, input: { messages: [] } })
      await nextTick()
      expect(host.querySelectorAll('[data-step-id]')).toHaveLength(1)
      expect(host.querySelector('[data-step-id="model"]')?.getAttribute('aria-pressed')).toBe('true')
      expect(selected[selected.length - 1].id).toBe('prep')
      expect(host.querySelector('[data-record-id="prep"]')).not.toBeNull()
      expect(host.querySelector('.trace-inspector__timing')?.textContent).not.toContain('实测耗时')
    } finally { app.unmount() }
  })

  it('shows image rejection even when a legacy checkpoint was marked successful', async () => {
    const image: TraceSpan = { id: 'image-rejection', kind: 'image_input_preflight', phase: 'context', title: 'Image', status: 'success', seq: 1, recordedAt: 8, output: { action: 'reject', image_count: 1, reason: 'unsupported_image' } }
    const projection: TraceProjection = { traceId: 'rejected', status: 'success', complete: true, spans: [], phases: [] }
    const host = document.createElement('div')
    const app = createApp(TraceTimeline, { projection, details: [image] })
    app.use(i18n)
    app.mount(host)
    try {
      expect(host.querySelector('[data-event-id="image-rejection"]')?.getAttribute('data-boundary')).toBe('true')
      expect(host.querySelector('[data-event-id="image-rejection"]')?.classList.contains('trace-event--error')).toBe(true)
      expect(host.querySelector('.trace-ledger-row__title')?.textContent).toContain('图片输入被拒绝')
      expect(host.querySelector('.trace-detail-facts__status--error')?.textContent).toContain('错误')
      expect(image.status).toBe('success')
    } finally { app.unmount() }
  })

  it('appends a result marker without changing the inspected model or counting output twice', async () => {
    const model: TraceSpan = { id: 'model', kind: 'llm_progress', phase: 'model_execution', title: 'Model', status: 'running', startedAt: 10, seq: 1, output: { text: 'Draft answer' } }
    const final: TraceSpan = { id: 'end', kind: 'turn_end', phase: 'finalize', title: 'End', status: 'success', seq: 4, startedAt: 0, endedAt: 120, recordedAt: 120, elapsedMs: 120, durationMs: 120, output: { final_text: 'Final delivered answer', segments: [{ type: 'text', text: 'Final delivered answer' }], error: null } }
    const state = reactive({ rows: [model], payload: undefined as { rowId: string; output?: unknown } | undefined })
    const projection: TraceProjection = { traceId: 'result-live', status: 'running', complete: false, spans: [], phases: [] }
    const selections: TraceSpan[] = []
    const host = document.createElement('div')
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const app = createApp({ setup: () => () => h(TraceTimeline, { projection, details: state.rows, fullPayload: state.payload, allowFullPayload: true, onSelect: row => selections.push(row) }) })
    app.use(i18n)
    app.mount(host)
    try {
      expect(host.querySelector('[data-terminal]')).toBeNull()
      host.querySelector<HTMLButtonElement>('[data-event-id="model"]')!.click()
      await nextTick()
      state.rows = [
        { ...model, kind: 'llm_response', status: 'success', endedAt: 110, recordedAt: 110, durationMs: 100 },
        { id: 'after', kind: 'context_stage', phase: 'context', title: 'After', status: 'success', seq: 3, recordedAt: 115, input: { stage: 'session:after', messages: [] } },
        final,
      ]
      await nextTick()
      expect(host.querySelector('[data-step-id="model"]')?.getAttribute('aria-pressed')).toBe('true')
      expect(host.querySelector('[data-result-id="end"]')?.getAttribute('aria-pressed')).toBe('false')
      expect(host.querySelectorAll('[data-step-id]')).toHaveLength(1)
      const marker = host.querySelector<HTMLButtonElement>('[data-event-id="end"]')!
      expect(marker.dataset.terminal).toBe('true')
      expect(marker.dataset.timeStart).toBe('120')
      expect(marker.dataset.timeEnd).toBe('120')
      expect(marker.textContent?.trim()).toBe('')
      expect(marker.title).not.toContain('实测耗时')
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Draft answer')
      marker.click()
      await nextTick()
      expect(selections[selections.length - 1].id).toBe('end')
      expect(selections[selections.length - 1].seq).toBe(4)
      expect(host.querySelector('.trace-inspector h3')?.textContent).toContain('本轮结果 · 已完成')
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Final delivered answer')
      expect(host.querySelector('.trace-inspector__timing')?.textContent).not.toContain('实测耗时')
      expect(host.querySelector('[data-record-id="after"]')).not.toBeNull()
      state.payload = { rowId: 'end', output: { final_text: 'Complete final payload', error: null } }
      await nextTick()
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Complete final payload')
      expect(final.durationMs).toBe(120)
      expect(final.output).toEqual({ final_text: 'Final delivered answer', segments: [{ type: 'text', text: 'Final delivered answer' }], error: null })
    } finally { app.unmount() }
  })

  it('preserves real result processing while rendering a failed terminal as a result', async () => {
    const processing: TraceSpan = { id: 'delivery', kind: 'response_delivery', phase: 'finalize', title: 'Deliver response', status: 'success', startedAt: 10, endedAt: 13, recordedAt: 13, durationMs: 3 }
    const end: TraceSpan = { id: 'failed', kind: 'turn_end', phase: 'finalize', title: 'End', status: 'success', recordedAt: 14, output: { final_text: '', error: 'Delivery failed' } }
    const projection: TraceProjection = { traceId: 'failed-result', status: 'success', complete: true, spans: [], phases: [] }
    const host = document.createElement('div')
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const app = createApp(TraceTimeline, { projection, details: [processing, end] })
    app.use(i18n)
    app.mount(host)
    try {
      const delivery = host.querySelector<HTMLButtonElement>('[data-event-id="delivery"]')!
      expect(delivery.dataset.terminal).toBeUndefined()
      expect(delivery.classList.contains('trace-event--measured-control')).toBe(true)
      expect(delivery.title).toContain('3ms')
      expect(host.querySelectorAll('[data-step-id]')).toHaveLength(1)
      const result = host.querySelector<HTMLButtonElement>('[data-result-id="failed"]')!
      expect(result.textContent).toContain('运行失败')
      expect(host.querySelector('.trace-timeline')?.getAttribute('data-status')).toBe('error')
      result.click()
      await nextTick()
      expect(host.querySelector('.trace-inspector')?.textContent).toContain('Delivery failed')
      expect(host.querySelector('.trace-detail-facts__status--error')).not.toBeNull()
      expect(end.status).toBe('success')
    } finally { app.unmount() }
  })

  it('keeps cancelled results and controls inspectable when only summary records exist', async () => {
    const rows: TraceSpan[] = [
      { id: 'input', kind: 'turn_start', phase: 'intake', title: 'Input', status: 'success', recordedAt: 0 },
      { id: 'approval', kind: 'approval_wait', phase: 'approval_sandbox', title: 'Approval', status: 'cancelled', recordedAt: 10 },
      { id: 'cancelled', kind: 'turn_cancelled', phase: 'finalize', title: 'Cancelled', status: 'cancelled', recordedAt: 20 },
    ]
    const projection: TraceProjection = { traceId: 'summary-only', status: 'cancelled', complete: true, spans: rows, phases: [] }
    const host = document.createElement('div')
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const app = createApp(TraceTimeline, { projection, allowFullPayload: true, detailsReason: 'disabled' })
    app.use(i18n)
    app.mount(host)
    try {
      expect([...host.querySelectorAll('[data-lane]')].map(lane => lane.getAttribute('data-lane'))).toEqual(['intake', 'model', 'tool'])
      expect(host.querySelector('[data-event-id="approval"]')).not.toBeNull()
      expect(host.querySelectorAll('[data-terminal]')).toHaveLength(1)
      host.querySelector<HTMLButtonElement>('[data-result-id="cancelled"]')!.click()
      await nextTick()
      expect(host.querySelector('.trace-inspector h3')?.textContent).toContain('本轮结果 · 已取消')
      expect(host.querySelector('.trace-inspector__raw')).toBeNull()
      expect(host.querySelector('.trace-details__empty')?.textContent).toContain('请启用原始诊断')
    } finally { app.unmount() }
  })
})
