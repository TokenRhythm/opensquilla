// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import { DURABLE_DELIVERY_KEY, type DeliverySnapshot, type DurableDelivery } from '@/modules/delivery'
import DeliveryRecoveryNotice from './DeliveryRecoveryNotice.vue'
import en from '@/locales/en.json'

describe('application delivery recovery notice', () => {
  let app: App | undefined
  afterEach(() => { app?.unmount(); app = undefined; document.body.replaceChildren() })

  function mount(initial: DeliverySnapshot[]) {
    let records = initial
    let notify = () => {}
    const unsubscribe = vi.fn()
    const retry = vi.fn().mockResolvedValue(undefined)
    const open = vi.fn()
    const owner = {
      snapshots: () => records,
      subscribe: (listener: () => void) => { notify = listener; return unsubscribe },
      retry,
    } as unknown as DurableDelivery
    const root = document.createElement('div')
    document.body.append(root)
    app = createApp(DeliveryRecoveryNotice, { onOpenSession: open })
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    app.provide(DURABLE_DELIVERY_KEY, owner)
    app.mount(root)
    return { root, retry, open, unsubscribe, async update(next: DeliverySnapshot[]) { records = next; notify(); await nextTick() } }
  }

  it('keeps a parked unknown delivery visible and lets a user recheck its exact identity', async () => {
    const { root, retry, open } = mount([{ id: 'original-request', sessionKey: 'source', phase: 'unknown', stopPending: true, waitReason: 'budget' }])
    expect(root.textContent).toContain('1 delivery results need confirmation')
    expect(retry).not.toHaveBeenCalled()
    root.querySelector('button')!.click()
    await nextTick()
    expect(root.textContent).toContain('Automatic checks have paused')
    expect(root.textContent).toContain('Stop will continue')
    const buttons = [...root.querySelectorAll('button')]
    buttons.find(button => button.textContent === 'Check again')!.click()
    await nextTick()
    expect(retry).toHaveBeenCalledExactlyOnceWith('original-request')
    buttons.find(button => button.textContent === 'Open conversation')!.click()
    expect(open).toHaveBeenCalledExactlyOnceWith('source')
  })

  it('shows storage failure even if the known task was already stopped, and releases its observer on unmount', async () => {
    const view = mount([])
    expect(view.root.querySelector('section')).toBeNull()
    await view.update([{ id: 'stop', sessionKey: 'source', phase: 'accepted', stopPending: false, waitReason: 'storage' }])
    view.root.querySelector('button')!.click()
    await nextTick()
    expect(view.root.textContent).toContain('could not be saved')
    expect(view.root.textContent).toContain('cannot be guaranteed after reopening')
    app!.unmount(); app = undefined
    expect(view.unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('keeps rendering bounded for hundreds of unresolved deliveries and clears completed rows', async () => {
    const view = mount(Array.from({ length: 500 }, (_, index) => ({ id: `${index}`, sessionKey: `session-${index}`, phase: 'unknown', stopPending: false, waitReason: 'receipt-missing' })))
    expect(view.root.querySelectorAll('li')).toHaveLength(0)
    view.root.querySelector('button')!.click()
    await nextTick()
    expect(view.root.querySelectorAll('li')).toHaveLength(11)
    expect(view.retry).not.toHaveBeenCalled()
    await view.update([{ id: '0', sessionKey: 'session-0', phase: 'accepted', stopPending: false }])
    expect(view.root.querySelector('section')).toBeNull()
  })
})
