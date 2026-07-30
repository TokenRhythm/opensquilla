// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App as VueApp } from 'vue'
import i18n from '@/i18n'
import UpdateBanner from './UpdateBanner.vue'

const platformMocks = vi.hoisted(() => ({
  desktopUpdateManaged: vi.fn(),
}))

vi.mock('@/platform', () => ({
  getPlatform: () => ({
    id: 'web',
    desktopUpdateManaged: platformMocks.desktopUpdateManaged,
  }),
}))

const POLL_INTERVAL_MS = 15 * 60 * 1000
const REQUEST_TIMEOUT_MS = 5 * 1000
const apps = new Set<VueApp>()
let fetchMock: ReturnType<typeof vi.fn>

interface UpdatePayload {
  current: string
  latest: string | null
  available: boolean
  url: string | null
  checkedAt: string | null
}

function payload(overrides: Partial<UpdatePayload> = {}): UpdatePayload {
  return {
    current: '0.5.0rc4',
    latest: null,
    available: false,
    url: null,
    checkedAt: '2026-07-13T08:00:00Z',
    ...overrides,
  }
}

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: vi.fn(async () => body),
  } as unknown as Response
}

function injectBootstrap(latest = '0.5.0rc5', url = 'https://example.test/rc5'): void {
  const data = document.createElement('div')
  data.id = 'opensquilla-data'
  data.dataset.update = JSON.stringify({
    current: '0.5.0rc4',
    latest,
    available: true,
    url,
  })
  document.body.appendChild(data)
}

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: state,
  })
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
  await nextTick()
}

async function mountBanner(): Promise<{ app: VueApp; el: HTMLDivElement }> {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(UpdateBanner)
  app.use(i18n)
  app.mount(el)
  apps.add(app)
  await flushAsync()
  return { app, el }
}

function unmount(app: VueApp): void {
  if (!apps.delete(app)) return
  app.unmount()
}

beforeEach(() => {
  vi.useFakeTimers()
  document.body.innerHTML = ''
  localStorage.clear()
  sessionStorage.clear()
  setVisibility('visible')
  i18n.global.locale.value = 'en'
  platformMocks.desktopUpdateManaged.mockReset().mockResolvedValue(false)
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  for (const app of apps) app.unmount()
  apps.clear()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('UpdateBanner live update polling', () => {
  it('shows a newly published release on the next poll without remounting', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(payload()))
      .mockResolvedValueOnce(jsonResponse(payload({
        latest: '0.5.0rc5',
        available: true,
        url: 'https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0rc5',
      })))

    const { el } = await mountBanner()
    expect(el.querySelector('[data-testid="update-banner"]')).toBeNull()

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    await flushAsync()

    const banner = el.querySelector('[data-testid="update-banner"]')
    expect(banner?.textContent).toContain('0.5.0rc5')
    expect(el.querySelector('.update-banner__link')?.getAttribute('href')).toBe(
      'https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0rc5',
    )
  })

  it('reads the current session token for every same-origin request', async () => {
    sessionStorage.setItem('opensquilla.wsToken', 'first-token')
    fetchMock.mockResolvedValue(jsonResponse(payload()))

    await mountBanner()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/system/update')
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      cache: 'no-store',
      headers: { Authorization: 'Bearer first-token' },
    })
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal)

    sessionStorage.setItem('opensquilla.wsToken', 'rotated-token')
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    await flushAsync()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({
      Authorization: 'Bearer rotated-token',
    })
  })

  it('clears stale bootstrap information after a valid no-update response', async () => {
    injectBootstrap()
    fetchMock.mockResolvedValue(jsonResponse(payload()))

    const { el } = await mountBanner()

    expect(el.querySelector('[data-testid="update-banner"]')).toBeNull()
  })

  it('preserves the last known update after an HTTP failure', async () => {
    injectBootstrap()
    fetchMock.mockResolvedValue(jsonResponse(null, false))

    const { el } = await mountBanner()

    expect(el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc5')
  })

  it('preserves the last known update after network or invalid-JSON failures', async () => {
    injectBootstrap()
    fetchMock.mockRejectedValueOnce(new TypeError('offline'))

    const first = await mountBanner()
    expect(first.el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc5')
    unmount(first.app)

    document.body.innerHTML = ''
    injectBootstrap('0.5.0rc6')
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: vi.fn(async () => { throw new SyntaxError('invalid JSON') }),
    } as unknown as Response)

    const second = await mountBanner()
    expect(second.el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc6')
  })

  it('preserves the last known update when the JSON schema is invalid', async () => {
    injectBootstrap()
    fetchMock.mockResolvedValue(jsonResponse({ available: false }))

    const { el } = await mountBanner()

    expect(el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc5')
  })

  it('pauses while hidden and requests immediately whenever the page becomes visible', async () => {
    setVisibility('hidden')
    fetchMock.mockResolvedValue(jsonResponse(payload()))
    await mountBanner()
    expect(fetchMock).not.toHaveBeenCalled()

    setVisibility('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(2)

    setVisibility('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('deduplicates interval and visibility triggers while a request is in flight', async () => {
    let resolveFirst!: (response: Response) => void
    fetchMock
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveFirst = resolve }))
      .mockResolvedValue(jsonResponse(payload()))

    await mountBanner()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)
    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    setVisibility('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    resolveFirst(jsonResponse(payload()))
    await flushAsync()
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('aborts a request after five seconds without erasing a known update', async () => {
    injectBootstrap()
    let requestSignal: AbortSignal | undefined
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      requestSignal = init?.signal as AbortSignal | undefined
      requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))

    const { el } = await mountBanner()
    expect(requestSignal?.aborted).toBe(false)

    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1)
    expect(requestSignal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await flushAsync()

    expect(requestSignal?.aborted).toBe(true)
    expect(el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc5')
  })

  it('aborts active work and removes timers/listeners when unmounted', async () => {
    let requestSignal: AbortSignal | undefined
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      requestSignal = init?.signal as AbortSignal | undefined
      requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))

    const { app } = await mountBanner()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    unmount(app)
    expect(requestSignal?.aborted).toBe(true)

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 4)
    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    setVisibility('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushAsync()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not start polling if unmounted before capability detection resolves', async () => {
    let resolveCapability!: (enabled: boolean) => void
    platformMocks.desktopUpdateManaged.mockImplementation(
      () => new Promise<boolean>((resolve) => { resolveCapability = resolve }),
    )

    const { app } = await mountBanner()
    unmount(app)
    resolveCapability(false)
    await flushAsync()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not poll and hides bootstrap information when the desktop owns updates', async () => {
    injectBootstrap()
    platformMocks.desktopUpdateManaged.mockResolvedValue(true)

    const { el } = await mountBanner()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="update-banner"]')).toBeNull()
  })

  it('does not poll when managed capability detection fails', async () => {
    injectBootstrap()
    platformMocks.desktopUpdateManaged.mockRejectedValue(new Error('bridge unavailable'))

    await mountBanner()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('uses the generic releases page when the API has no exact release URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse(payload({
      latest: '0.5.0rc5',
      available: true,
      url: null,
    })))

    const { el } = await mountBanner()

    expect(el.querySelector('.update-banner__link')?.getAttribute('href')).toBe(
      'https://github.com/opensquilla/opensquilla/releases',
    )
  })

  it('re-arms a dismissed notice when a newer version arrives', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(payload({
        latest: '0.5.0rc5',
        available: true,
        url: 'https://example.test/rc5',
      })))
      .mockResolvedValueOnce(jsonResponse(payload({
        latest: '0.5.0rc6',
        available: true,
        url: 'https://example.test/rc6',
      })))

    const { el } = await mountBanner()
    const dismiss = el.querySelector('.update-banner__dismiss') as HTMLButtonElement
    dismiss.click()
    await nextTick()
    expect(localStorage.getItem('opensquilla-update-dismissed')).toBe('0.5.0rc5')
    expect(el.querySelector('[data-testid="update-banner"]')).toBeNull()

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    await flushAsync()

    expect(el.querySelector('[data-testid="update-banner"]')?.textContent).toContain('0.5.0rc6')
  })
})
