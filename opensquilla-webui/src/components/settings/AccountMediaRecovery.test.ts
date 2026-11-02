// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import AccountMediaRecovery from './AccountMediaRecovery.vue'

const mock = vi.hoisted(() => ({ store: null as any }))
vi.mock('@/stores/account', () => ({ useAccountStore: () => mock.store }))
const apps: ReturnType<typeof createApp>[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

async function mount(image = 'inherit', video = 'inherit') {
  mock.store = reactive({
    state: { status: 'signed_out', mediaSources: { image, video } }, busy: false,
    selectMediaSource: vi.fn(async (kind: 'image' | 'video', source: string) => {
      mock.store.state.mediaSources[kind] = source
    }),
  })
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'zh-Hans'
  const el = document.createElement('div')
  document.body.append(el)
  const app = createApp({ render: () => h(AccountMediaRecovery) })
  app.use(i18n); app.mount(el); apps.push(app)
  await nextTick()
  return el
}

describe('legacy account media recovery', () => {
  it('is absent for default sources and never writes on mount', async () => {
    const el = await mount()
    expect(el.querySelector('button')).toBeNull()
    expect(mock.store.selectMediaSource).not.toHaveBeenCalled()
    mock.store.state.mediaSources.image = 'account'
    await nextTick()
    expect(el.querySelector('button')).not.toBeNull()
    expect(el.querySelector('select')).toBeNull()
    expect(mock.store.selectMediaSource).not.toHaveBeenCalled()
  })

  it('restores only non-default sources after an explicit click, including signed-out sessions', async () => {
    const el = await mount('account', 'inherit')
    el.querySelector('button')!.click()
    await vi.waitFor(() => expect(el.querySelector('button')).toBeNull())
    expect(mock.store.selectMediaSource).toHaveBeenCalledExactlyOnceWith('image', 'inherit')
  })

  it('blocks duplicate recovery while saving and respects account busy state', async () => {
    const el = await mount('manual', 'account')
    mock.store.busy = true
    await nextTick()
    expect(el.querySelector('button')!.disabled).toBe(true)
    expect(mock.store.selectMediaSource).not.toHaveBeenCalled()
    mock.store.busy = false
    let finish!: () => void
    mock.store.selectMediaSource.mockImplementationOnce(async () => {
      await new Promise<void>(resolve => { finish = resolve })
      mock.store.state.mediaSources.image = 'inherit'
    })
    await nextTick()
    el.querySelector('button')!.click()
    el.querySelector('button')!.click()
    await nextTick()
    expect(mock.store.selectMediaSource).toHaveBeenCalledTimes(1)
    expect(el.querySelector('button')!.disabled).toBe(true)
    finish()
    await vi.waitFor(() => expect(el.querySelector('button')).toBeNull())
    expect(mock.store.selectMediaSource.mock.calls).toEqual([['image', 'inherit'], ['video', 'inherit']])
  })

  it('shows a retryable error and retries only the remaining source after a partial failure', async () => {
    const el = await mount('account', 'manual')
    mock.store.selectMediaSource
      .mockImplementationOnce(async () => { mock.store.state.mediaSources.image = 'inherit' })
      .mockRejectedValueOnce(new Error('offline'))
    el.querySelector('button')!.click()
    await vi.waitFor(() => expect(el.querySelector('[role="alert"]')).not.toBeNull())
    expect(el.querySelector('button')!.disabled).toBe(false)
    expect(mock.store.state.mediaSources).toEqual({ image: 'inherit', video: 'manual' })
    el.querySelector('button')!.click()
    await vi.waitFor(() => expect(el.querySelector('button')).toBeNull())
    expect(mock.store.selectMediaSource.mock.calls).toEqual([
      ['image', 'inherit'], ['video', 'inherit'], ['video', 'inherit'],
    ])
  })
})
