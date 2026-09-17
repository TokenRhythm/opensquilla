// @vitest-environment happy-dom
import { createApp, defineComponent, nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProductActivityError, type ProductActivity } from '@/modules/productActivity'
import { useProductActivity } from './useProductActivity'
import { invalidateReadiness } from './setup/useReadinessSummary'

const platform = vi.hoisted(() => ({ capabilities: { isDesktop: false } }))
vi.mock('@/platform', () => ({ getPlatform: () => platform }))

let visible = true
let focused = true
const unmounts: Array<() => void> = []

function mount(options: { connected?: boolean; owner?: boolean; record?: () => Promise<boolean> } = {}) {
  const access = reactive({
    isAvailable: options.connected ?? true,
    isLocalOwner: options.owner ?? true,
    subscriptionEpoch: 1,
  })
  const recordActive = vi.fn<ProductActivity['recordActive']>(options.record ?? (async () => true))
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(defineComponent({
    setup() {
      useProductActivity(access, { recordActive })
      return () => null
    },
  }))
  app.mount(el)
  const unmount = () => { app.unmount(); el.remove() }
  unmounts.push(unmount)
  return { access, recordActive, unmount }
}

function interact(type = 'pointerdown', trusted = true) {
  const event = new Event(type)
  Object.defineProperty(event, 'isTrusted', { value: trusted })
  document.dispatchEvent(event)
}

async function settle() {
  await nextTick()
  await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-01-01T23:59:50Z'))
  visible = true
  focused = true
  platform.capabilities.isDesktop = false
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden')
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
})

afterEach(() => {
  for (const unmount of unmounts.splice(0)) unmount()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('foreground product activity', () => {
  it.each([false, true])('classifies the existing platform bridge, desktop=%s', async (isDesktop) => {
    platform.capabilities.isDesktop = isDesktop
    const { recordActive } = mount()
    await settle()
    expect(recordActive).toHaveBeenCalledExactlyOnceWith(
      isDesktop ? 'desktop' : 'web', { signal: expect.any(AbortSignal) },
    )
  })

  it('records once per UTC day and never advances activity on an idle timer', async () => {
    const { recordActive } = mount()
    await settle()
    interact()
    interact('keydown')
    window.dispatchEvent(new Event('focus'))
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
    vi.setSystemTime(new Date('2026-01-02T00:00:01Z'))
    await vi.runAllTimersAsync()
    expect(recordActive).toHaveBeenCalledTimes(1)
    interact('keydown')
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
  })

  it('ignores synthetic interactions', async () => {
    const { recordActive } = mount()
    await settle()
    vi.setSystemTime(new Date('2026-01-02T00:00:01Z'))
    interact('pointerdown', false)
    interact('keydown', false)
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
  })

  it('does not count hidden startup, background reconnection, or background input', async () => {
    visible = false
    const { access, recordActive } = mount()
    access.subscriptionEpoch += 1
    interact()
    window.dispatchEvent(new Event('focus'))
    await settle()
    expect(recordActive).not.toHaveBeenCalled()
    visible = true
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
  })

  it('waits for focus when a visible window is in the background', async () => {
    focused = false
    const { recordActive } = mount()
    await settle()
    expect(recordActive).not.toHaveBeenCalled()
    focused = true
    window.dispatchEvent(new Event('focus'))
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
  })

  it.each([{ connected: false }, { owner: false }])('does not count disconnected/guest access: %j', async (options) => {
    const { access, recordActive } = mount(options)
    interact()
    await settle()
    expect(recordActive).not.toHaveBeenCalled()
    access.isAvailable = true
    access.isLocalOwner = true
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
  })

  it.each(['error', 'not-recorded'])('retries %s only on later interaction at a limited frequency', async (result) => {
    const { recordActive } = mount({ record: async () => {
      if (result === 'error') throw new Error('transient')
      return false
    } })
    await settle()
    interact()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(5_000)
    interact()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(55_000)
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
    interact()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
  })

  it('quietly suppresses old Gateway incompatibility until connection changes', async () => {
    const { access, recordActive } = mount({ record: async () => {
      throw new ProductActivityError('unsupported')
    } })
    await settle()
    vi.setSystemTime(new Date('2026-01-02T00:00:01Z'))
    interact()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
    access.subscriptionEpoch += 1
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
  })

  it('invalidates its day cache on reconnection and settings changes', async () => {
    const { access, recordActive } = mount()
    await settle()
    access.subscriptionEpoch += 1
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
    invalidateReadiness()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
    interact()
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(3)
  })

  it('does not let an obsolete response populate a replacement connection cache', async () => {
    let finish!: (result: boolean) => void
    const { access, recordActive } = mount({ record: () => new Promise(resolve => { finish = resolve }) })
    const finishOld = finish
    access.isAvailable = false
    finishOld(true)
    await settle()
    access.isAvailable = true
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
  })

  it('preserves a real next-day interaction while the previous request is pending', async () => {
    let finish!: (result: boolean) => void
    const { recordActive } = mount({ record: () => new Promise(resolve => { finish = resolve }) })
    vi.setSystemTime(new Date('2026-01-02T00:00:01Z'))
    interact()
    expect(recordActive).toHaveBeenCalledTimes(1)
    finish(true)
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(2)
  })

  it('unsubscribes and aborts pending work on unmount', async () => {
    const removeDocumentListener = vi.spyOn(document, 'removeEventListener')
    const removeWindowListener = vi.spyOn(window, 'removeEventListener')
    const { access, recordActive, unmount } = mount({ record: () => new Promise(() => {}) })
    const signal = recordActive.mock.calls[0]?.[1]?.signal
    unmount()
    unmounts.pop()
    expect(signal?.aborted).toBe(true)
    expect(removeDocumentListener).toHaveBeenCalledWith('pointerdown', expect.any(Function), true)
    expect(removeDocumentListener).toHaveBeenCalledWith('keydown', expect.any(Function), true)
    expect(removeDocumentListener).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    expect(removeWindowListener).toHaveBeenCalledWith('focus', expect.any(Function))
    access.subscriptionEpoch += 1
    invalidateReadiness()
    interact()
    window.dispatchEvent(new Event('focus'))
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(recordActive).toHaveBeenCalledTimes(1)
  })
})
