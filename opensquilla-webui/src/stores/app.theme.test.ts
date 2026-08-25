// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAppStore } from './app'

const THEME_KEY = 'opensquilla-theme'
const DARK_SCHEME_QUERY = '(prefers-color-scheme: dark)'

function stubMatchMedia(initialMatches = false) {
  type ChangeListener = (event: MediaQueryListEvent) => void

  let matches = initialMatches
  const modernListeners = new Set<ChangeListener>()
  const legacyListeners = new Set<ChangeListener>()
  const addEventListener = vi.fn((type: string, listener: EventListener) => {
    if (type === 'change') modernListeners.add(listener as ChangeListener)
  })
  const removeEventListener = vi.fn((type: string, listener: EventListener) => {
    if (type === 'change') modernListeners.delete(listener as ChangeListener)
  })
  const addListener = vi.fn((listener: ChangeListener) => legacyListeners.add(listener))
  const removeListener = vi.fn((listener: ChangeListener) => legacyListeners.delete(listener))
  const mediaQuery = {
    get matches() { return matches },
    media: DARK_SCHEME_QUERY,
    onchange: null,
    addEventListener,
    removeEventListener,
    addListener,
    removeListener,
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList

  // Return one stable MediaQueryList object, like a browser-owned subscription,
  // so tests can drive the exact object the store listened to.
  window.matchMedia = vi.fn(() => mediaQuery) as unknown as typeof window.matchMedia

  return {
    addEventListener,
    removeEventListener,
    listenerCount: () => modernListeners.size + legacyListeners.size,
    setMatches(next: boolean) {
      if (matches === next) return
      matches = next
      const event = { matches, media: DARK_SCHEME_QUERY } as MediaQueryListEvent
      for (const listener of [...modernListeners, ...legacyListeners]) listener(event)
    },
  }
}

function stubDesktopThemeBridge() {
  const setNativeTheme = vi.fn(
    async (_payload: { source: 'light' | 'dark' | 'system' }) => undefined,
  )
  ;(window as unknown as {
    opensquillaDesktop?: { setNativeTheme: typeof setNativeTheme }
  }).opensquillaDesktop = { setNativeTheme }
  return setNativeTheme
}

describe('app store — theme persistence + legacy id migration', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
    ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = undefined
    // Default OS preference to light so an unmigrated fallback would resolve to
    // 'light' (distinct from the renamed dark themes under test).
    stubMatchMedia(false)
  })

  it('migrates a persisted legacy id ("nord") to its renamed theme ("arctic")', () => {
    localStorage.setItem(THEME_KEY, 'nord')
    const store = useAppStore()
    store.initTheme()
    // Resolves to the renamed id — it does NOT fall back to system/default.
    expect(store.theme).toBe('arctic')
    expect(store.resolvedTheme).toBe('arctic')
    // The canonical id is written back, so the migration happens once and the
    // pre-paint anti-flash script stamps the right theme next cold load.
    expect(localStorage.getItem(THEME_KEY)).toBe('arctic')
    store.destroyTheme()
  })

  it('migrates a persisted legacy "phosphor" to "crt-green"', () => {
    localStorage.setItem(THEME_KEY, 'phosphor')
    const store = useAppStore()
    store.initTheme()
    expect(store.theme).toBe('crt-green')
    expect(localStorage.getItem(THEME_KEY)).toBe('crt-green')
    store.destroyTheme()
  })

  it('keeps a current custom theme id as-is', () => {
    localStorage.setItem(THEME_KEY, 'vapor')
    const store = useAppStore()
    store.initTheme()
    expect(store.theme).toBe('vapor')
    expect(localStorage.getItem(THEME_KEY)).toBe('vapor')
    store.destroyTheme()
  })

  it('drops a genuinely unknown persisted id and falls back to system', () => {
    localStorage.setItem(THEME_KEY, 'ferrari-red')
    const store = useAppStore()
    store.initTheme()
    expect(store.theme).toBe('system')
    expect(localStorage.getItem(THEME_KEY)).toBeNull()
    store.destroyTheme()
  })

  it('syncs the Electron native shell theme when the app theme changes', async () => {
    const setNativeTheme = stubDesktopThemeBridge()

    localStorage.setItem(THEME_KEY, 'dark')
    const store = useAppStore()
    store.initTheme()
    await nextTick()
    expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'dark' })

    store.setTheme('light')
    await nextTick()
    expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'light' })

    store.destroyTheme()
  })

  it('keeps Electron on its system source during a cold system-theme startup', async () => {
    const media = stubMatchMedia(true)
    const setNativeTheme = stubDesktopThemeBridge()
    localStorage.setItem(THEME_KEY, 'system')

    const store = useAppStore()
    store.initTheme()
    await nextTick()

    expect(store.theme).toBe('system')
    expect(store.resolvedTheme).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'system' })
    expect(media.addEventListener).toHaveBeenCalledWith('change', expect.any(Function))
    expect(media.addEventListener.mock.invocationCallOrder[0]).toBeLessThan(
      setNativeTheme.mock.invocationCallOrder[0],
    )

    store.destroyTheme()
  })

  it.each(['dark', 'vapor'] as const)(
    'releases a fixed %s theme back to Electron system control',
    async (initialTheme) => {
      stubMatchMedia(true)
      const setNativeTheme = stubDesktopThemeBridge()
      localStorage.setItem(THEME_KEY, initialTheme)

      const store = useAppStore()
      store.initTheme()
      await nextTick()
      expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'dark' })

      store.setTheme('system')
      await nextTick()

      expect(store.theme).toBe('system')
      expect(store.resolvedTheme).toBe('dark')
      expect(localStorage.getItem(THEME_KEY)).toBe('system')
      expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'system' })

      store.destroyTheme()
    },
  )

  it('tracks live OS scheme changes while retaining Electron system control', async () => {
    const media = stubMatchMedia(true)
    const setNativeTheme = stubDesktopThemeBridge()
    localStorage.setItem(THEME_KEY, 'system')

    const store = useAppStore()
    store.initTheme()
    await nextTick()
    setNativeTheme.mockClear()

    media.setMatches(false)
    await nextTick()

    expect(store.resolvedTheme).toBe('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(setNativeTheme).toHaveBeenCalledTimes(1)
    expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'system' })

    store.destroyTheme()
  })

  it.each(['dark', 'vapor'] as const)(
    'isolates a fixed %s theme from OS media-query changes',
    async (fixedTheme) => {
      const media = stubMatchMedia(false)
      const setNativeTheme = stubDesktopThemeBridge()
      localStorage.setItem(THEME_KEY, fixedTheme)

      const store = useAppStore()
      store.initTheme()
      await nextTick()
      setNativeTheme.mockClear()

      media.setMatches(true)
      await nextTick()

      expect(store.resolvedTheme).toBe(fixedTheme)
      expect(document.documentElement.getAttribute('data-theme')).toBe(fixedTheme)
      expect(setNativeTheme).not.toHaveBeenCalled()

      store.destroyTheme()
    },
  )

  it('cleans up idempotently and re-snapshots one MQL listener on re-init', async () => {
    const media = stubMatchMedia(false)
    const setNativeTheme = stubDesktopThemeBridge()
    localStorage.setItem(THEME_KEY, 'system')
    const store = useAppStore()

    store.initTheme()
    await nextTick()
    expect(media.listenerCount()).toBe(1)
    expect(media.addEventListener).toHaveBeenCalledTimes(1)

    store.destroyTheme()
    store.destroyTheme()
    expect(media.listenerCount()).toBe(0)
    expect(media.removeEventListener).toHaveBeenCalledTimes(1)

    setNativeTheme.mockClear()
    media.setMatches(true)
    await nextTick()
    expect(store.resolvedTheme).toBe('light')
    expect(setNativeTheme).not.toHaveBeenCalled()

    store.initTheme()
    await nextTick()
    expect(media.listenerCount()).toBe(1)
    expect(media.addEventListener).toHaveBeenCalledTimes(2)
    expect(store.resolvedTheme).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(setNativeTheme).toHaveBeenLastCalledWith({ source: 'system' })

    setNativeTheme.mockClear()
    media.setMatches(false)
    await nextTick()
    expect(setNativeTheme).toHaveBeenCalledTimes(1)

    store.destroyTheme()
    store.destroyTheme()
    expect(media.listenerCount()).toBe(0)
    expect(media.removeEventListener).toHaveBeenCalledTimes(2)
  })
})
