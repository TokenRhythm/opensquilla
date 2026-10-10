// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import { MEMORY_PROFILE_IMPORT_KEY } from '@/modules/memoryProfileImport'
import { createV4MemoryProfileImport } from '@/adapters/gateway/memoryProfileImportV4'
import SettingsMemoryPanel from './SettingsMemoryPanel.vue'
import i18n from '@/i18n'

vi.mock('@/platform', () => ({ usePlatform: () => ({ capabilities: { isDesktop: true }, settings: {} }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

const apps: App[] = []
const activeJob = {
  schemaVersion: 1, jobId: 'job', batchId: 'batch', status: 'analyzing',
  stage: 'reading', provider: 'synthetic', model: 'synthetic', startedAt: '', preview: null,
}
const info = { schemaVersion: 1, available: true, draftJob: activeJob }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
async function settle() {
  for (let i = 0; i < 16; i++) await Promise.resolve()
  await nextTick()
}
async function mountPanel(request: (method: string, params?: Record<string, unknown>, options?: object) => Promise<unknown>) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp(SettingsMemoryPanel)
  app.use(i18n)
  app.provide(MEMORY_PROFILE_IMPORT_KEY, createV4MemoryProfileImport({
    ready: async () => {}, supports: () => true, markUnsupported: () => {},
    request: async <T>(...args: [string, Record<string, unknown>?, object?]) => await request(...args) as T,
  }))
  app.mount(element)
  apps.push(app)
  await settle()
  return { app, element }
}
function unmount(app: App) {
  app.unmount()
  apps.splice(apps.indexOf(app), 1)
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('memory import observation lifecycle', () => {
  it('clears settled polling on unmount without cancelling the backend job', async () => {
    vi.useFakeTimers()
    const request = vi.fn(async (method: string) => method === 'memory.import.info' ? info : activeJob)
    const { app } = await mountPanel(request)
    unmount(app)
    await vi.advanceTimersByTimeAsync(6000)
    expect(request.mock.calls.map(call => call[0])).toEqual(['memory.import.info'])
  })

  it('aborts an info read and ignores its late response after unmount', async () => {
    vi.useFakeTimers()
    const gate = deferred<typeof info>()
    const request = vi.fn(async (method: string) => method === 'memory.import.info' ? gate.promise : activeJob)
    const { app, element } = await mountPanel(request)
    const signal = (request.mock.calls[0] as unknown as [string, unknown, { signal?: AbortSignal }])[2].signal
    unmount(app)
    gate.resolve(info)
    await settle()
    await vi.advanceTimersByTimeAsync(6000)
    expect(element.childElementCount).toBe(0)
    expect(request.mock.calls.map(call => call[0])).toEqual(['memory.import.info'])
    expect(signal?.aborted).toBe(true)
  })

  it('does not resurrect polling from a late status response', async () => {
    vi.useFakeTimers()
    const gate = deferred<typeof activeJob>()
    const request = vi.fn(async (method: string) => method === 'memory.import.info' ? info : gate.promise)
    const { app } = await mountPanel(request)
    await vi.advanceTimersByTimeAsync(2000)
    expect(request).toHaveBeenCalledTimes(2)
    unmount(app)
    gate.resolve(activeJob)
    await settle()
    await vi.advanceTimersByTimeAsync(6000)
    expect(request.mock.calls.map(call => call[0])).toEqual(['memory.import.info', 'memory.import.status'])
  })

  it('coalesces visibility refreshes with an outstanding status read', async () => {
    vi.useFakeTimers()
    const gate = deferred<typeof activeJob>()
    const request = vi.fn(async (method: string) => method === 'memory.import.info' ? info : gate.promise)
    const { app } = await mountPanel(request)
    await vi.advanceTimersByTimeAsync(2000)
    for (let i = 0; i < 6; i++) document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(request).toHaveBeenCalledTimes(2)
    gate.resolve(activeJob)
    await settle()
    await vi.advanceTimersByTimeAsync(2000)
    expect(request).toHaveBeenCalledTimes(3)
    unmount(app)
  })

  it('only the reopened panel observes the background import', async () => {
    vi.useFakeTimers()
    const gate = deferred<typeof info>()
    let infos = 0
    const request = vi.fn(async (method: string) => {
      if (method === 'memory.import.info') return ++infos === 1 ? gate.promise : info
      return activeJob
    })
    const first = await mountPanel(request)
    unmount(first.app)
    const reopened = await mountPanel(request)
    gate.resolve(info)
    await settle()
    await vi.advanceTimersByTimeAsync(2000)
    expect(request.mock.calls.map(call => call[0])).toEqual([
      'memory.import.info', 'memory.import.info', 'memory.import.status',
    ])
    unmount(reopened.app)
  })
})
