// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import type { TraceProjection } from '@/types/traceView'
import TraceInspectorDialog from './TraceInspectorDialog.vue'

vi.mock('@/components/trace/TraceTimeline.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({
    props: ['projection'],
    setup: props => () => h('div', { 'data-testid': 'trace-timeline' }, props.projection.traceId),
  }) }
})

const traceProjection = vi.fn()
const traceDetails = vi.fn()
const mounted: App[] = []

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
}

function mountDialog(initialTraceId?: string) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(TraceInspectorDialog, { initialTraceId })
  const gatewayAccess = reactive({ subscriptionEpoch: 1, availability: 'available' })
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, gatewayAccess as GatewayAccess)
  app.provide(OBSERVABILITY_KEY, { traceProjection, traceDetails } as never)
  app.mount(host)
  mounted.push(app)
  return { app, host, gatewayAccess }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  traceProjection.mockReset()
  traceDetails.mockReset()
})

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('TraceInspectorDialog', () => {
  it('opens a valid linked Trace automatically when the gated entry mounts', async () => {
    traceProjection.mockResolvedValue({
      traceId: 'linked-123', status: 'success', complete: true, phases: [],
      spans: [{ id: 'step-1', kind: 'llm_response', phase: 'model_execution', title: 'Model', status: 'success' }],
    })
    traceDetails.mockResolvedValue({ traceId: 'linked-123', available: true, rows: [], count: 0, total: 0 })
    mountDialog('linked-123')
    await flush()
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
    expect(traceProjection).toHaveBeenCalledExactlyOnceWith('linked-123', { signal: expect.any(AbortSignal) })
  })

  it('ignores an invalid linked Trace ID', async () => {
    mountDialog('../trace')
    await flush()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(traceProjection).not.toHaveBeenCalled()
  })

  it('discards a previous Gateway result when the connection changes', async () => {
    traceProjection.mockResolvedValue({
      traceId: 'trace-123', status: 'success', complete: true, phases: [],
      spans: [{ id: 'step-1', kind: 'llm_response', phase: 'model_execution', title: 'Model', status: 'success' }],
    })
    traceDetails.mockResolvedValue({ traceId: 'trace-123', available: true, rows: [], count: 0, total: 0 })
    const { gatewayAccess } = mountDialog('trace-123')
    await flush()
    expect(document.querySelector('[data-testid="trace-timeline"]')).not.toBeNull()
    gatewayAccess.subscriptionEpoch += 1
    await flush()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.querySelector('[data-testid="trace-timeline"]')).toBeNull()
  })

  it('reads a Trace only after explicit lookup and shows the returned timeline', async () => {
    const projection: TraceProjection = {
      traceId: 'trace-123', status: 'success', complete: true, phases: [],
      spans: [{ id: 'step-1', kind: 'llm_response', phase: 'model_execution', title: 'Model', status: 'success' }],
    }
    traceProjection.mockResolvedValue(projection)
    traceDetails.mockResolvedValue({ traceId: 'trace-123', available: true, rows: [], count: 0, total: 0 })
    const { host } = mountDialog()
    expect(traceProjection).not.toHaveBeenCalled()
    host.querySelector<HTMLButtonElement>('button')!.click()
    await flush()
    expect(traceProjection).not.toHaveBeenCalled()

    const input = document.querySelector<HTMLInputElement>('[role="dialog"] input')!
    input.value = ' trace-123 '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()
    document.querySelector<HTMLFormElement>('[role="dialog"] form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()

    expect(traceProjection).toHaveBeenCalledExactlyOnceWith('trace-123', { signal: expect.any(AbortSignal) })
    expect(traceDetails).toHaveBeenCalledExactlyOnceWith('trace-123', { signal: expect.any(AbortSignal), limit: 1000 })
    expect(document.querySelector('[data-testid="trace-timeline"]')?.textContent).toBe('trace-123')
  })

  it('aborts an in-flight lookup when the entry is removed', async () => {
    let signal: AbortSignal | undefined
    traceProjection.mockImplementation((_id, options) => {
      signal = options.signal
      return new Promise(() => {})
    })
    const { app, host } = mountDialog()
    host.querySelector<HTMLButtonElement>('button')!.click()
    await flush()
    const input = document.querySelector<HTMLInputElement>('[role="dialog"] input')!
    input.value = 'trace-123'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()
    document.querySelector<HTMLFormElement>('[role="dialog"] form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(signal?.aborted).toBe(false)
    app.unmount()
    expect(signal?.aborted).toBe(true)
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })
})
