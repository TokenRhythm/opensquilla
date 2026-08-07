import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.resetModules()
  vi.unstubAllGlobals()
})

describe('useComposerFloatingPreference', () => {
  it('defaults to enabled when no preference is stored', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
    })
    const { useComposerFloatingPreference } = await import('./useComposerFloatingPreference')

    expect(useComposerFloatingPreference().enabled.value).toBe(true)
  })

  it('restores a persisted disabled preference', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => JSON.stringify({ enabled: false })),
      setItem: vi.fn(),
    })
    const { useComposerFloatingPreference } = await import('./useComposerFloatingPreference')

    expect(useComposerFloatingPreference().enabled.value).toBe(false)
  })

  it('falls back to enabled for malformed or inaccessible storage', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => '{not-json'),
      setItem: vi.fn(),
    })
    let preference = await import('./useComposerFloatingPreference')
    expect(preference.useComposerFloatingPreference().enabled.value).toBe(true)

    vi.resetModules()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => { throw new Error('blocked') }),
      setItem: vi.fn(),
    })
    preference = await import('./useComposerFloatingPreference')
    expect(preference.useComposerFloatingPreference().enabled.value).toBe(true)
  })

  it('shares live state across consumers and persists the toggle', async () => {
    const setItem = vi.fn()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem,
    })
    const { useComposerFloatingPreference } = await import('./useComposerFloatingPreference')
    const settings = useComposerFloatingPreference()
    const chat = useComposerFloatingPreference()

    settings.setEnabled(false)

    expect(chat.enabled.value).toBe(false)
    expect(setItem).toHaveBeenCalledWith('opensquilla.composerFx', JSON.stringify({ enabled: false }))
  })

  it('tolerates missing localStorage', async () => {
    vi.stubGlobal('localStorage', undefined)
    const { useComposerFloatingPreference } = await import('./useComposerFloatingPreference')
    const { enabled, setEnabled } = useComposerFloatingPreference()

    expect(enabled.value).toBe(true)
    expect(() => setEnabled(false)).not.toThrow()
    expect(enabled.value).toBe(false)
  })

  it('keeps live state when persisting is blocked', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(() => { throw new Error('quota') }),
    })
    const { useComposerFloatingPreference } = await import('./useComposerFloatingPreference')
    const { enabled, setEnabled } = useComposerFloatingPreference()

    expect(() => setEnabled(false)).not.toThrow()
    expect(enabled.value).toBe(false)
  })
})
