// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, shallowRef, type App } from 'vue'
import i18n from '@/i18n'
import { SESSION_DIRECTORY_KEY, type SessionDirectory } from '@/modules/sessionDirectory'
import {
  SESSION_DIRECTORY_CHANGES_KEY,
  type SessionDirectoryChange,
} from '@/modules/sessionDirectoryChanges'
import type { SessionReferenceV1 } from '@/types/references'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import SessionReferenceCard from './SessionReferenceCard.vue'

const key = 'agent:main:webchat:target'
const apps: App[] = []

async function mount(resolve = vi.fn().mockResolvedValue({
  key, id: 'target', title: 'Deployment review', runStatus: 'running',
}), overrides: Partial<SessionReferenceV1> = {}) {
  const reference = shallowRef<SessionReferenceV1>({
    version: 1, kind: 'session', id: key, label: key,
    scope: { sessionKey: key }, state: { available: true, runStatus: null },
    capabilities: { open: true },
    ...overrides,
  })
  const gateway = reactive({ endpoint: 'ws://gateway.example/ws', epoch: 1, available: true })
  const onOpen = vi.fn()
  const close = vi.fn()
  let listener: ((change: SessionDirectoryChange) => void) | undefined
  const el = document.createElement('div')
  document.body.append(el)
  const app = createApp({
    setup: () => () => h(SessionReferenceCard, { reference: reference.value, onOpen }),
  })
  apps.push(app)
  app.use(i18n)
  app.provide(SESSION_DIRECTORY_KEY, { resolve } as unknown as SessionDirectory)
  app.provide(GATEWAY_ACCESS_KEY, {
    get isAvailable() { return gateway.available },
    get subscriptionEpoch() { return gateway.epoch },
    loadConnectionEndpoint: () => gateway.endpoint,
  } as GatewayAccess)
  app.provide(SESSION_DIRECTORY_CHANGES_KEY, {
    subscribe: callback => { listener = callback; return { close } },
    resume: async () => {}, dispose: () => {},
  })
  app.mount(el)
  await nextTick()
  await nextTick()
  return { el, app, resolve, onOpen, close, reference, gateway, change: (value: SessionDirectoryChange) => listener?.(value) }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SessionReferenceCard', () => {
  it('hydrates historical labels, follows state changes, and resolves before navigation', async () => {
    i18n.global.locale.value = 'en'
    const ui = await mount()
    expect(ui.el.textContent).toContain('Deployment review')
    expect(ui.el.textContent).toContain('Running')
    ui.resolve.mockResolvedValue({ key, id: 'target', title: 'Renamed review', runStatus: 'idle' })
    ui.change({ key, reason: 'renamed', runStatus: 'idle' })
    await nextTick()
    await nextTick()
    expect(ui.el.textContent).toContain('Renamed review')
    ui.el.querySelector('button')?.click()
    await nextTick()
    await nextTick()
    expect(ui.onOpen).toHaveBeenCalledWith(key)
    expect(ui.resolve).toHaveBeenCalledTimes(3)
  })

  it('does not navigate after a resolution error or a fuzzy match', async () => {
    const ui = await mount()
    ui.resolve.mockRejectedValue(new Error('Forbidden'))
    ui.el.querySelector('button')?.click()
    await nextTick()
    await nextTick()
    expect(ui.onOpen).not.toHaveBeenCalled()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    ui.resolve.mockResolvedValue({ key: `${key}-other`, id: 'other', title: 'Wrong', runStatus: 'idle' })
    ui.change({ key, reason: 'updated' })
    await nextTick()
    await nextTick()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    expect(ui.el.textContent).not.toContain('Wrong')
  })

  it('does not re-resolve equivalent references recreated while an answer streams', async () => {
    const ui = await mount()
    for (let token = 0; token < 5; token += 1) {
      ui.reference.value = structuredClone(ui.reference.value)
      await nextTick()
      await nextTick()
    }
    expect(ui.resolve).toHaveBeenCalledOnce()
    expect(ui.el.textContent).toContain('Deployment review')
    expect(ui.el.textContent).toContain('Running')

    ui.reference.value = { ...ui.reference.value, label: 'Updated source label' }
    await nextTick()
    await nextTick()
    expect(ui.resolve).toHaveBeenCalledTimes(2)
  })

  it('fails closed for instance-scoped references without an instance attestation API', async () => {
    const resolve = vi.fn()
    const ui = await mount(resolve, { scope: { sessionKey: key, gatewayInstanceId: 'other-instance' } })
    expect(resolve).not.toHaveBeenCalled()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    ui.el.querySelector('button')?.click()
    expect(ui.onOpen).not.toHaveBeenCalled()
  })

  it('does not carry a card to another Gateway after a live connection switch', async () => {
    const ui = await mount()
    ui.gateway.endpoint = 'ws://other.example/ws'
    ui.gateway.epoch += 1
    await nextTick()
    await nextTick()
    expect(ui.resolve).toHaveBeenCalledOnce()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    expect(ui.el.textContent).not.toContain('Deployment review')
    ui.change({ key, reason: 'updated' })
    ui.el.querySelector('button')?.click()
    expect(ui.resolve).toHaveBeenCalledOnce()
    expect(ui.onOpen).not.toHaveBeenCalled()
  })

  it('ignores an old hydration response after the Gateway epoch changes', async () => {
    let finishOld!: (value: unknown) => void
    const resolve = vi.fn().mockReturnValueOnce(new Promise(done => { finishOld = done }))
      .mockResolvedValue({ key, id: 'fresh-id', title: 'Fresh Gateway result', runStatus: 'idle' })
    const ui = await mount(resolve)
    const signal = resolve.mock.calls[0]![0].signal as AbortSignal
    ui.gateway.epoch += 1
    await nextTick()
    await nextTick()
    finishOld({ key, id: 'old-id', title: 'Old Gateway result', runStatus: 'running' })
    await nextTick()
    await nextTick()
    expect(signal.aborted).toBe(true)
    expect(ui.el.textContent).toContain('Fresh Gateway result')
    expect(ui.el.textContent).not.toContain('Old Gateway result')
    expect(ui.resolve).toHaveBeenCalledTimes(2)
  })

  it('does not navigate if the Gateway changes while a click is resolving', async () => {
    const ui = await mount()
    let finishClick!: (value: unknown) => void
    ui.resolve.mockReturnValueOnce(new Promise(done => { finishClick = done }))
    ui.el.querySelector('button')?.click()
    const signal = ui.resolve.mock.calls[1]![0].signal as AbortSignal
    ui.gateway.endpoint = 'ws://other.example/ws'
    ui.gateway.epoch += 1
    finishClick({ key, id: 'old-id', title: 'Old Gateway result', runStatus: 'idle' })
    await nextTick()
    await nextTick()
    expect(signal.aborted).toBe(true)
    expect(ui.onOpen).not.toHaveBeenCalled()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
  })

  it('disables on disconnect and rechecks once when the same Gateway becomes available', async () => {
    const ui = await mount()
    ui.gateway.available = false
    ui.gateway.epoch += 1
    await nextTick()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    expect(ui.resolve).toHaveBeenCalledOnce()
    ui.gateway.available = true
    await nextTick()
    await nextTick()
    expect(ui.resolve).toHaveBeenCalledTimes(2)
    expect(ui.el.querySelector('button')?.disabled).toBe(false)
  })

  it('disables deleted references and removes its subscription on unmount', async () => {
    const ui = await mount()
    ui.change({ key, reason: 'deleted' })
    await nextTick()
    expect(ui.el.querySelector('button')?.disabled).toBe(true)
    ui.app.unmount()
    expect(ui.close).toHaveBeenCalledOnce()
    apps.splice(apps.indexOf(ui.app), 1)
  })
})
