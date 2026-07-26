// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import HistoryLoadSentinel from './HistoryLoadSentinel.vue'

const apps: App<Element>[] = []
let observerCallback: IntersectionObserverCallback | null = null
let observerOptions: IntersectionObserverInit | undefined

class MockIntersectionObserver {
  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    observerCallback = callback
    observerOptions = options
  }

  observe = vi.fn()
  disconnect = vi.fn()
  unobserve = vi.fn()
  takeRecords = vi.fn(() => [])
  root = null
  rootMargin = ''
  thresholds = []
}

async function mountSentinel(props: Record<string, unknown>, onLoadEarlier = vi.fn()) {
  const root = document.createElement('div')
  root.tabIndex = 0
  const host = document.createElement('div')
  document.body.append(root, host)
  const state = reactive({
    scrollContainer: root,
    hasMore: false,
    loading: false,
    blocked: false,
    error: false,
    canonicalAvailable: true,
    canonicalComplete: true,
    cursor: null,
    ...props,
  })
  const app = createApp({
    setup: () => () => h(HistoryLoadSentinel, {
      ...state,
      onLoadEarlier,
      onRetry: onLoadEarlier,
    }),
  })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  await nextTick()
  return { host, root, state, onLoadEarlier }
}

beforeEach(() => {
  observerCallback = null
  observerOptions = undefined
  vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('HistoryLoadSentinel', () => {
  it('preloads automatically within the 320px top margin and emits once per cursor', async () => {
    const { root, onLoadEarlier } = await mountSentinel({ hasMore: true, cursor: 'cursor-1' })

    expect(observerOptions).toMatchObject({ root, rootMargin: '320px 0px 0px 0px' })
    const entry = { isIntersecting: true } as IntersectionObserverEntry
    observerCallback?.([entry], {} as IntersectionObserver)
    observerCallback?.([entry], {} as IntersectionObserver)

    expect(onLoadEarlier).toHaveBeenCalledOnce()
  })

  it('does not consume a cursor while refresh is blocked and retries it after unblock', async () => {
    const { state, onLoadEarlier } = await mountSentinel({
      hasMore: true,
      cursor: 'cursor-1',
      blocked: true,
    })

    expect(observerCallback).toBeNull()
    state.blocked = false
    await nextTick()
    await nextTick()
    observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver)

    expect(onLoadEarlier).toHaveBeenCalledOnce()
  })

  it('renders the exact localized loading state', async () => {
    i18n.global.locale.value = 'zh-Hans'
    const { host } = await mountSentinel({ loading: true })

    expect(host.textContent).toContain('正在加载更早的消息…')
    expect(host.querySelector('.history-load-sentinel__feedback--loading')).toBeTruthy()
    expect(host.querySelector('[aria-atomic="true"]')).toBeTruthy()
  })

  it('only exposes a retry control after a load failure', async () => {
    const onLoadEarlier = vi.fn()
    const { host, root } = await mountSentinel({ error: true, hasMore: true, cursor: 'cursor-1' }, onLoadEarlier)

    expect(host.querySelector('[data-testid="history-load-retry"]')).toBeTruthy()
    expect(host.querySelector('[role="status"]')).toBeTruthy()
    ;(host.querySelector('[data-testid="history-load-retry"]') as HTMLButtonElement).click()
    await nextTick()
    expect(onLoadEarlier).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(root)
  })

  it('shows a truthful legacy notice without a summary or load button', async () => {
    const { host } = await mountSentinel({ canonicalAvailable: true, canonicalComplete: false })

    expect(host.textContent).toContain('Earlier original messages were not saved by the older version.')
    expect(host.querySelector('button')).toBeNull()
  })

  it('treats canonical-read unavailability as retryable instead of legacy loss', async () => {
    const { host } = await mountSentinel({ canonicalAvailable: false, canonicalComplete: false })

    expect(host.querySelector('[data-testid="history-load-retry"]')).toBeTruthy()
    expect(host.textContent).not.toContain('older version')
  })

  it('keeps a missing empty session quiet when its empty transcript is complete', async () => {
    const { host } = await mountSentinel({ canonicalAvailable: false, canonicalComplete: true })

    expect(host.querySelector('[data-testid="history-load-sentinel"]')).toBeNull()
    expect(host.querySelector('button')).toBeNull()
  })
})
