// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import { useChatTrace } from './useChatTrace'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { createV4Observability } from '@/adapters/gateway/observabilityV4'

const rpc = vi.hoisted(() => ({ call: vi.fn(), waitForConnection: vi.fn() }))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
async function settle() { for (let i = 0; i < 12; i += 1) await Promise.resolve(); await nextTick() }
let complete = false
function response(method: string, params: Record<string, unknown>) {
  const traceId = String(params.trace_id || `trace-${params.turn_id}`)
  if (method === 'logs.turn_traces') return { raw_enabled: true, traces: [{ trace_id: traceId, status: complete ? 'success' : 'running', complete, raw_available: true }] }
  if (method === 'logs.trace_projection') return { trace_id: traceId, status: complete ? 'success' : 'running', complete, spans: [{ kind: 'turn_start', phase: 'intake', seq: 1, elapsed_ms: 0 }] }
  if (method === 'logs.trace_details') return { trace_id: traceId, available: true, rows: [{ id: 'step', kind: 'llm_response', phase: 'model_execution', seq: 3, input_seq: 2, elapsed_ms: 100, duration_ms: 90, input: 'preview request', output: 'preview response' }] }
  if (method === 'logs.trace_payload') return { trace_id: traceId, available: true, payload: { seq: params.seq, content: `full-${params.seq}` } }
  throw new Error(`Unexpected RPC ${method}`)
}
const apps: Array<ReturnType<typeof createApp>> = []
function mount(initial = { sessionKey: 'session-a', turnId: 'turn-a', running: false }) {
  const state = reactive(initial)
  let result!: ReturnType<typeof useChatTrace>
  const app = createApp({ setup() {
    result = useChatTrace({ sessionKey: () => state.sessionKey, turnId: () => state.turnId, running: () => state.running })
    return () => null
  } })
  app.provide(OBSERVABILITY_KEY, createV4Observability({
    request: rpc.call, ready: rpc.waitForConnection, supports: () => true, markUnsupported: () => {},
  }, { requestJson: vi.fn(), requestBinary: vi.fn() }))
  app.mount(document.createElement('div'))
  apps.push(app)
  return { state, result, app }
}
beforeEach(() => {
  vi.useFakeTimers()
  rpc.call.mockReset()
  rpc.waitForConnection.mockReset().mockResolvedValue(undefined)
  rpc.call.mockImplementation(async (method, params) => response(method, params))
  complete = false
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })

describe('useChatTrace', () => {
  it('queries the exact session and turn and rejects a late result after switching turns', async () => {
    const oldLookup = deferred<unknown>()
    rpc.call.mockImplementation((method, params) => method === 'logs.turn_traces' && params.turn_id === 'turn-a' ? oldLookup.promise : Promise.resolve(response(method, params)))
    const { state, result } = mount()
    await settle()
    const oldOptions = rpc.call.mock.calls[0][2]
    state.turnId = 'turn-b'
    await settle()
    expect(oldOptions.signal.aborted).toBe(true)
    expect(result.activeTraceId.value).toBe('trace-turn-b')
    expect(rpc.call).toHaveBeenCalledWith('logs.turn_traces', { session_key: 'session-a', turn_id: 'turn-b' }, expect.objectContaining({ abortAction: 'reject' }))
    oldLookup.resolve(response('logs.turn_traces', { turn_id: 'turn-a' }))
    await settle()
    expect(result.projection.value?.traceId).toBe('trace-turn-b')
    expect(rpc.call.mock.calls.some(([method, params]) => method === 'logs.trace_projection' && params.trace_id === 'trace-turn-a')).toBe(false)
  })

  it('polls while running, loads the terminal snapshot and stops after unmount', async () => {
    const { state, result, app } = mount({ sessionKey: 'session-a', turnId: 'turn-a', running: true })
    await settle()
    await vi.advanceTimersByTimeAsync(1500)
    expect(rpc.call.mock.calls.filter(([method]) => method === 'logs.turn_traces')).toHaveLength(2)
    complete = true
    state.running = false
    await settle()
    expect(result.projection.value?.complete).toBe(true)
    const count = rpc.call.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(rpc.call).toHaveBeenCalledTimes(count)
    state.running = true
    await settle()
    app.unmount()
    apps.splice(apps.indexOf(app), 1)
    const afterUnmount = rpc.call.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(rpc.call).toHaveBeenCalledTimes(afterUnmount)
    expect(rpc.call.mock.calls[rpc.call.mock.calls.length - 1][2].signal.aborted).toBe(true)
  })

  it('loads matched original request and response only on demand and clears them on turn changes', async () => {
    const { state, result } = mount()
    await settle()
    expect(rpc.call.mock.calls.some(([method]) => method === 'logs.trace_payload')).toBe(false)
    result.selectRow(result.details.value!.rows[0])
    await result.loadPayload()
    expect(result.payload.value).toEqual({ input: { seq: 2, content: 'full-2' }, output: { seq: 3, content: 'full-3' } })
    const delayed = deferred<unknown>()
    rpc.call.mockImplementation((method, params) => method === 'logs.trace_payload' ? delayed.promise : Promise.resolve(response(method, params)))
    const pending = result.loadPayload()
    state.turnId = 'turn-b'
    await settle()
    expect(result.payload.value).toBeNull()
    delayed.resolve({ available: true, payload: 'old turn content' })
    await pending
    expect(result.payload.value).toBeNull()
    expect(result.selectedRow.value).toBeNull()
  })

  it('keeps all attempts selectable and respects the raw capture gate', async () => {
    rpc.call.mockImplementation(async (method, params) => {
      if (method === 'logs.turn_traces') return { raw_enabled: false, traces: ['first', 'second'].map(trace_id => ({ trace_id, complete: true, status: 'success', raw_available: false })) }
      if (method === 'logs.trace_details') return { trace_id: params.trace_id, available: false, reason: 'raw_diagnostics_disabled', rows: [] }
      return response(method, params)
    })
    const { result } = mount()
    await settle()
    expect(result.traces.value).toHaveLength(2)
    expect(result.activeTraceId.value).toBe('second')
    result.selectTrace('first')
    await settle()
    expect(result.activeTraceId.value).toBe('first')
    expect(result.details.value?.reason).toBe('raw_diagnostics_disabled')
    expect(result.rawEnabled.value).toBe(false)
  })

  it('keeps the safe projection visible when detailed records require admin access', async () => {
    rpc.call.mockImplementation(async (method, params) => {
      if (method === 'logs.trace_details') throw Object.assign(new Error('admin required'), { code: 'UNAUTHORIZED' })
      return response(method, params)
    })
    const { result } = mount()
    await settle()
    expect(result.projection.value?.traceId).toBe('trace-turn-a')
    expect(result.projection.value?.spans).toHaveLength(1)
    expect(result.error.value).toBe('')
    expect(result.details.value).toMatchObject({ available: false, reason: 'access_denied', rows: [] })
    expect(result.rawEnabled.value).toBe(true)
    expect(rpc.call.mock.calls.some(([method]) => method === 'logs.trace_payload')).toBe(false)
  })

  it('clears previously loaded details on access revocation and distinguishes a denied full record', async () => {
    let denyDetails = false
    rpc.call.mockImplementation(async (method, params) => {
      if (method === 'logs.trace_details' && denyDetails) throw Object.assign(new Error('admin required'), { code: 'FORBIDDEN' })
      if (method === 'logs.trace_payload') throw Object.assign(new Error('admin required'), { code: 'PERMISSION_DENIED' })
      return response(method, params)
    })
    const { result } = mount()
    await settle()
    result.selectRow(result.details.value!.rows[0])
    await result.loadPayload()
    expect(result.payloadError.value).toBe(false)
    expect(result.payloadRestricted.value).toBe(true)
    denyDetails = true
    await result.refresh()
    expect(result.projection.value?.traceId).toBe('trace-turn-a')
    expect(result.error.value).toBe('')
    expect(result.details.value?.reason).toBe('access_denied')
    expect(result.selectedRow.value).toBeNull()
    expect(result.payload.value).toBeNull()
  })

  it('updates a selected call incrementally without discarding unrelated loaded records', async () => {
    let revision = 0
    let additionalCheckpoint = false
    rpc.call.mockImplementation(async (method, params) => {
      if (method === 'logs.trace_projection' && revision === 1) throw new Error('Temporary projection read failure')
      if (method === 'logs.trace_details') {
        const call = {
          id: 'step:call-a', order_seq: 2, seq: revision === 0 ? 2 : revision === 1 ? 3 : 7, input_seq: 2,
          kind: revision === 0 ? 'llm_request' : revision === 1 ? 'llm_progress' : 'llm_response',
          status: revision === 2 ? 'success' : 'running', phase: 'model_execution',
          started_elapsed_ms: 10, elapsed_ms: revision === 0 ? 10 : revision === 1 ? 50 : 100,
          input: 'request', output: revision === 0 ? null : { text: revision === 1 ? 'Partial' : 'Partial complete', partial: revision === 1 },
        }
        return { trace_id: params.trace_id, available: true, rows: additionalCheckpoint ? [{ id: 'context', kind: 'context_stage', phase: 'context', seq: 6, elapsed_ms: 90 }, call] : [call] }
      }
      return response(method, params)
    })
    const { result } = mount({ sessionKey: 'session-a', turnId: 'turn-a', running: true })
    await settle()
    result.selectRow(result.details.value!.rows[0])
    await result.loadPayload()
    expect(result.payload.value?.input).toBeDefined()
    revision = 1
    await result.refresh()
    expect(result.projection.value?.traceId).toBe('trace-turn-a')
    expect(result.selectedRow.value?.id).toBe('step:call-a')
    expect(result.selectedRow.value?.output).toEqual({ text: 'Partial', partial: true })
    expect(result.payload.value).toBeNull()
    await result.loadPayload()
    const loaded = result.payload.value
    additionalCheckpoint = true
    await result.refresh()
    expect(result.payload.value).toBe(loaded)
    expect(result.details.value?.rows.map(row => row.id)).toEqual(['step:call-a', 'context'])
    revision = 2
    complete = true
    await result.refresh()
    expect(result.details.value?.rows.map(row => row.id)).toEqual(['step:call-a', 'context'])
    expect(result.selectedRow.value?.output).toEqual({ text: 'Partial complete', partial: false })
    expect(result.payload.value).toBeNull()
  })

  it('keeps an observed live turn polling through a temporary chat-status gap', async () => {
    const { state, result } = mount({ sessionKey: 'session-a', turnId: 'turn-a', running: true })
    await settle()
    state.running = false
    await settle()
    expect(result.live.value).toBe(true)
    await vi.advanceTimersByTimeAsync(6000)
    const count = rpc.call.mock.calls.filter(([method]) => method === 'logs.turn_traces').length
    expect(count).toBeGreaterThan(4)
    complete = true
    await vi.advanceTimersByTimeAsync(1500)
    expect(result.live.value).toBe(false)
    const terminalCount = rpc.call.mock.calls.length
    await vi.advanceTimersByTimeAsync(6000)
    expect(rpc.call).toHaveBeenCalledTimes(terminalCount)
  })

  it('does not poll historical incomplete traces that were never observed live in this binding', async () => {
    const { result } = mount()
    await settle()
    expect(result.live.value).toBe(false)
    const count = rpc.call.mock.calls.length
    await vi.advanceTimersByTimeAsync(6000)
    expect(rpc.call).toHaveBeenCalledTimes(count)
  })

  it('clears the old session immediately and ignores its delayed detail snapshot', async () => {
    const delayed = deferred<unknown>()
    let firstDetail = true
    rpc.call.mockImplementation((method, params) => {
      if (method === 'logs.trace_details' && firstDetail) { firstDetail = false; return delayed.promise }
      return Promise.resolve(response(method, params))
    })
    const { state, result } = mount({ sessionKey: 'session-a', turnId: 'same-turn', running: true })
    await settle()
    state.sessionKey = 'session-b'
    await settle()
    expect(rpc.call).toHaveBeenCalledWith('logs.turn_traces', { session_key: 'session-b', turn_id: 'same-turn' }, expect.anything())
    delayed.resolve({ trace_id: 'trace-same-turn', available: true, rows: [{ id: 'old-session', kind: 'context_stage', phase: 'context', input: 'old session data' }] })
    await settle()
    expect(result.details.value?.rows.map(row => row.id)).toEqual(['step'])
    expect(result.selectedRow.value).toBeNull()
  })
})
