import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.resetModules()
  vi.unstubAllGlobals()
})

describe('useRouterVisualEffectsPreference', () => {
  it('preserves the existing router visual-effects preference across upgrades', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => JSON.stringify({ enabled: false, variant: 'default' })),
      setItem: vi.fn(),
    })
    const { useRouterVisualEffectsPreference } = await import('./useRouterVisualEffectsPreference')

    expect(useRouterVisualEffectsPreference().enabled.value).toBe(false)
  })

  it('shares live state and keeps the existing storage payload', async () => {
    const setItem = vi.fn()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem,
    })
    const { useRouterVisualEffectsPreference } = await import('./useRouterVisualEffectsPreference')
    const settings = useRouterVisualEffectsPreference()
    const chat = useRouterVisualEffectsPreference()

    settings.setEnabled(false)

    expect(chat.enabled.value).toBe(false)
    expect(setItem).toHaveBeenCalledWith('opensquilla.routerFx', JSON.stringify({
      enabled: false,
      variant: 'default',
    }))
  })
})
