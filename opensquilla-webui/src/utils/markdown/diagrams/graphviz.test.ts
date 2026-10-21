import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderGraphviz } from './graphviz'
import { GRAPHVIZ_TIMEOUT_MS } from './budget'
import type { DiagramPalette } from './types'

const palette: DiagramPalette = { background: 'white', text: 'black', muted: 'gray', border: 'gray', accent: 'blue', dark: false }
const workers: FakeWorker[] = []
class FakeWorker {
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  terminate = vi.fn()
  postMessage = vi.fn()
  constructor() { workers.push(this) }
}

describe('Graphviz worker lifecycle', () => {
  beforeEach(() => { workers.length = 0; vi.useFakeTimers(); vi.stubGlobal('Worker', FakeWorker) })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('terminates the worker when a request finishes', async () => {
    const request = renderGraphviz('digraph { a -> b }', palette)
    workers[0].onmessage?.({ data: { svg: '<svg />' } })
    await expect(request).resolves.toBe('<svg />')
    expect(workers[0].terminate).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('terminates timed-out native layout work and permits the next request', async () => {
    const request = renderGraphviz('digraph { a -> b }', palette)
    const rejected = expect(request).rejects.toMatchObject({ code: 'timeout' })
    await vi.advanceTimersByTimeAsync(GRAPHVIZ_TIMEOUT_MS)
    await rejected
    expect(workers[0].terminate).toHaveBeenCalledOnce()
    const retry = renderGraphviz('digraph { a -> b }', palette)
    workers[1].onmessage?.({ data: { svg: '<svg />' } })
    await expect(retry).resolves.toBe('<svg />')
  })

  it('aborts running workers and rejects aborted requests before allocation', async () => {
    const controller = new AbortController()
    const request = renderGraphviz('digraph { a -> b }', palette, controller.signal)
    controller.abort()
    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
    expect(workers[0].terminate).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
    expect(() => renderGraphviz('digraph {}', palette, controller.signal)).toThrow('cancelled')
    expect(workers).toHaveLength(1)
  })

  it('rejects invalid and oversized output without retaining the worker', async () => {
    const bad = renderGraphviz('bad', palette)
    workers[0].onmessage?.({ data: { error: true } })
    await expect(bad).rejects.toMatchObject({ code: 'render' })
    const huge = renderGraphviz('huge', palette)
    workers[1].onmessage?.({ data: { svg: 'x'.repeat(1_000_001) } })
    await expect(huge).rejects.toMatchObject({ code: 'budget' })
    expect(workers.every(worker => worker.terminate.mock.calls.length === 1)).toBe(true)
  })
})
