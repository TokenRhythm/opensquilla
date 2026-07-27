// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settle = () => new Promise((resolve) => setTimeout(resolve, 10))

const routerPush = vi.fn()

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

interface RpcHandles {
  data?: { value: unknown }
  execute?: ReturnType<typeof vi.fn>
}

async function mountBanner(status: unknown, { desktop = false }: { desktop?: boolean } = {}) {
  vi.resetModules()
  document.body.innerHTML = ''
  // A truthy preload bridge flips platform detection to desktop.
  setDesktopApi(desktop ? { getOsLocale: async () => 'en' } : undefined)
  vi.doMock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))
  const handles: RpcHandles = {}
  vi.doMock('@/composables/useRpc', async () => {
    const { ref } = await import('vue')
    const data = ref(status)
    const execute = vi.fn()
    handles.data = data
    handles.execute = execute
    return {
      useRpcCall: () => ({
        data,
        loading: ref(false),
        error: ref(null),
        execute,
      }),
    }
  })
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SidebarSetupBanner.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  await settle()
  await nextTick()
  return { app, el, handles }
}

beforeEach(() => {
  routerPush.mockReset()
  setDesktopApi(undefined)
})

describe('SidebarSetupBanner legacy compatibility', () => {
  it('ignores legacyData returned by an older gateway', async () => {
    const { app, el } = await mountBanner({
      needsOnboarding: false,
      legacyData: {
        path: '/tmp/legacy-home',
        kind: 'cli-home',
        command: 'opensquilla migrate opensquilla',
      },
    })
    expect(el.querySelector('[data-testid="legacy-data-banner"]')).toBeNull()
    expect(el.textContent).not.toContain('/tmp/legacy-home')
    app.unmount()
  })
})

describe('SidebarSetupBanner readiness refresh', () => {
  it('re-fetches readiness and clears once a settings save invalidates it', async () => {
    const { app, el, handles } = await mountBanner({ needsOnboarding: true })
    expect(el.querySelector('.sidebar-setup-banner')).toBeTruthy()

    // A save hot-applies config, re-loads the Settings dialog data, and
    // signals invalidation; the banner must re-fetch instead of holding its
    // mount-time snapshot until the next full page reload.
    handles.execute!.mockImplementation(async () => {
      handles.data!.value = { needsOnboarding: false }
    })
    const { invalidateReadiness } = await import('@/composables/setup/useReadinessSummary')
    invalidateReadiness()
    await settle()

    expect(handles.execute).toHaveBeenCalled()
    expect(el.querySelector('.sidebar-setup-banner')).toBeNull()
    app.unmount()
  })

  it('stops listening for readiness invalidations after unmount', async () => {
    const { app, handles } = await mountBanner({ needsOnboarding: true })
    app.unmount()

    const { invalidateReadiness } = await import('@/composables/setup/useReadinessSummary')
    invalidateReadiness()

    expect(handles.execute).not.toHaveBeenCalled()
  })
})
