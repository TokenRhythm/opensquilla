// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { getPlatform } from '@/platform'
import { useAppStore } from './app'

const REQUIRED_V3_METHODS = [
  'createArtifactPreviewLease',
  'renewArtifactPreviewLease',
  'revokeArtifactPreviewLease',
  'createWorkbenchSurface',
  'setWorkbenchSurfaceRect',
  'activateWorkbenchSurface',
  'destroyWorkbenchSurface',
  'onWorkbenchSurfaceEvent',
  'getArtifactAnnotationCapabilities',
  'setArtifactAnnotationMode',
  'showArtifactAnnotationOverlay',
  'closeArtifactAnnotationOverlay',
  'screenshot',
] as const

function stubMatchMedia() {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

function completeV3Bridge(): Record<string, (...args: unknown[]) => unknown> {
  return Object.fromEntries(REQUIRED_V3_METHODS.map(method => [method, vi.fn()]))
}

function setDesktopApi(value: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = value
}

function setFeatureOverrides(value: Record<string, boolean> | undefined): void {
  ;(window as unknown as {
    OPENSQUILLA_FEATURES?: Record<string, boolean>
  }).OPENSQUILLA_FEATURES = value
}

describe('app store — V1 feature defaults', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    stubMatchMedia()
    setDesktopApi(undefined)
    // Reset the platform singleton between synthetic shell shapes.
    getPlatform()
    setFeatureOverrides(undefined)
  })

  it('enables HTML document resources but keeps browser annotations off by default', () => {
    const store = useAppStore()

    expect(store.features.documentWorkbenchResources).toBe(true)
    expect(store.features.artifactPromptAnnotations).toBe(false)
  })

  it('enables annotations by default for a complete Desktop v3 bridge', () => {
    setDesktopApi(completeV3Bridge())

    const store = useAppStore()

    expect(store.features.documentWorkbenchResources).toBe(true)
    expect(store.features.artifactPromptAnnotations).toBe(true)
  })

  it.each(REQUIRED_V3_METHODS)(
    'fails closed when Desktop is missing %s',
    (missingMethod) => {
      const bridge = completeV3Bridge()
      delete bridge[missingMethod]
      setDesktopApi(bridge)

      expect(useAppStore().features.artifactPromptAnnotations).toBe(false)
    },
  )

  it('applies window overrides last, including the emergency false switch', () => {
    setDesktopApi(completeV3Bridge())
    setFeatureOverrides({
      documentWorkbenchResources: false,
      artifactPromptAnnotations: false,
    })

    const store = useAppStore()

    expect(store.features.documentWorkbenchResources).toBe(false)
    expect(store.features.artifactPromptAnnotations).toBe(false)
  })

  it('retains the explicit development override on Web', () => {
    setFeatureOverrides({ artifactPromptAnnotations: true })

    expect(useAppStore().features.artifactPromptAnnotations).toBe(true)
  })
})
