// @vitest-environment happy-dom

import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ToastHost from './ToastHost.vue'
import { useToasts } from '@/composables/useToasts'

const mountedApps: ReturnType<typeof createApp>[] = []

function clearToasts() {
  const { toasts, dismissToast } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  clearToasts()
  document.body.innerHTML = ''
})

describe('ToastHost actions', () => {
  it('runs the action and dismisses the toast', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({ setup: () => () => h(ToastHost) })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: { en: { shared: { toast: { dismiss: 'Dismiss' } } } },
    }))
    app.mount(host)
    mountedApps.push(app)

    const onClick = vi.fn()
    useToasts().pushToast('Drink water', {
      tone: 'ok',
      action: { label: 'View', onClick },
    })
    await nextTick()

    host.querySelector<HTMLButtonElement>('.toast__action')?.click()
    await nextTick()

    expect(onClick).toHaveBeenCalledOnce()
    expect(useToasts().toasts.value).toHaveLength(0)
  })
})
