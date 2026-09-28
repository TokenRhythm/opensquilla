// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, ref, type App } from 'vue'
import type { SetupStatusPort } from '@/modules/setupWorkflow'
import { invalidateReadiness } from './readinessInvalidation'
import { useSetupStatus, type SetupStatusOptions, type SetupStatusState } from './useSetupStatus'

const apps = new Set<App>()
type Status = { audioConfigured?: boolean; searchConfigured?: boolean }

function mountStatus(setup: SetupStatusPort, options?: SetupStatusOptions) {
  let state!: SetupStatusState<Status>
  const app = createApp({
    setup() {
      state = useSetupStatus<Status>(setup, options)
      return () => null
    },
  })
  app.mount(document.createElement('div'))
  apps.add(app)
  return { state, unmount: () => { app.unmount(); apps.delete(app) } }
}

function deferred() {
  let resolve!: (value: Status) => void
  const promise = new Promise<Status>((done) => { resolve = done })
  return { promise, resolve }
}

afterEach(() => {
  for (const app of apps) app.unmount()
  apps.clear()
})

describe('setup status after settings saves', () => {
  it('refreshes mounted voice and search consumers with one shared read', async () => {
    const setup = { status: vi.fn().mockResolvedValue({ audioConfigured: false, searchConfigured: false }) }
    const voice = mountStatus(setup)
    const search = mountStatus(setup)
    await vi.waitFor(() => expect(voice.state.loading.value).toBe(false))
    expect(setup.status).toHaveBeenCalledTimes(1)
    setup.status.mockResolvedValue({ audioConfigured: true, searchConfigured: true })

    invalidateReadiness()

    await vi.waitFor(() => expect(voice.state.data.value?.audioConfigured).toBe(true))
    expect(search.state.data.value?.searchConfigured).toBe(true)
    expect(setup.status).toHaveBeenCalledTimes(2)
  })

  it('invalidates the cache even with no mounted consumers', async () => {
    const setup = { status: vi.fn().mockResolvedValue({ audioConfigured: false }) }
    const first = mountStatus(setup)
    await vi.waitFor(() => expect(first.state.loading.value).toBe(false))
    first.unmount()
    setup.status.mockResolvedValue({ audioConfigured: true })

    invalidateReadiness()
    expect(setup.status).toHaveBeenCalledTimes(1)
    const reopened = mountStatus(setup)

    await vi.waitFor(() => expect(reopened.state.data.value?.audioConfigured).toBe(true))
    expect(setup.status).toHaveBeenCalledTimes(2)
  })

  it('defers invalidation reads until session recovery admits optional RPCs', async () => {
    const allowed = ref(true)
    const setup = { status: vi.fn().mockResolvedValue({ audioConfigured: false }) }
    const consumer = mountStatus(setup, { allowed })
    await vi.waitFor(() => expect(consumer.state.loading.value).toBe(false))
    allowed.value = false
    await nextTick()
    setup.status.mockResolvedValue({ audioConfigured: true })

    invalidateReadiness()
    expect(setup.status).toHaveBeenCalledTimes(1)
    allowed.value = true

    await vi.waitFor(() => expect(consumer.state.data.value?.audioConfigured).toBe(true))
    expect(setup.status).toHaveBeenCalledTimes(2)
  })

  it('ignores a pre-save response that arrives after the refreshed snapshot', async () => {
    const beforeSave = deferred()
    const afterSave = deferred()
    const setup = { status: vi.fn().mockReturnValueOnce(beforeSave.promise).mockReturnValueOnce(afterSave.promise) }
    const consumer = mountStatus(setup)
    invalidateReadiness()
    afterSave.resolve({ audioConfigured: true })
    await vi.waitFor(() => expect(consumer.state.data.value?.audioConfigured).toBe(true))

    beforeSave.resolve({ audioConfigured: false })
    await beforeSave.promise
    await nextTick()
    expect(consumer.state.data.value?.audioConfigured).toBe(true)
    const reopened = mountStatus(setup)
    expect(reopened.state.data.value?.audioConfigured).toBe(true)
    expect(setup.status).toHaveBeenCalledTimes(2)
  })

  it('retains a known snapshot on refresh failure and can recover on a later save', async () => {
    const setup = { status: vi.fn().mockResolvedValue({ audioConfigured: true }) }
    const consumer = mountStatus(setup)
    await vi.waitFor(() => expect(consumer.state.loading.value).toBe(false))
    setup.status.mockRejectedValue(new Error('connection closed'))
    invalidateReadiness()
    await vi.waitFor(() => expect(consumer.state.error.value).toBe('connection closed'))
    expect(consumer.state.data.value?.audioConfigured).toBe(true)

    setup.status.mockResolvedValue({ audioConfigured: false })
    invalidateReadiness()
    await vi.waitFor(() => expect(consumer.state.data.value?.audioConfigured).toBe(false))
    expect(consumer.state.error.value).toBeNull()
  })
})
