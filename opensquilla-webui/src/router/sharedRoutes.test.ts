// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RouteLocationNormalized } from 'vue-router'
import i18n, { loadLocaleMessages } from '@/i18n'
import { LAST_ROUTE_KEY } from './lastRoute'
import { defaultRootRedirect } from './sharedRoutes'
import { routeTitle, routes } from './index'

beforeEach(() => {
  localStorage.clear()
  i18n.global.locale.value = 'en'
  delete window.opensquillaDesktop
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
})

describe('defaultRootRedirect', () => {
  it('opens the desktop app on Chat even when a previous route was saved', () => {
    window.opensquillaDesktop = {} as never
    localStorage.setItem(LAST_ROUTE_KEY, '/sessions')

    expect(defaultRootRedirect()).toBe('/chat')
  })

  it('keeps browser desktop restore behavior', () => {
    localStorage.setItem(LAST_ROUTE_KEY, '/overview')

    expect(defaultRootRedirect()).toBe('/overview')
  })
})

describe('route fallback', () => {
  it('keeps the Not Found catch-all after every platform route', () => {
    expect(routes[routes.length - 1]?.path).toBe('/:pathMatch(.*)*')
    expect(routes[routes.length - 1]?.name).toBe('not-found')
  })
})

describe('route hubs', () => {
  function routeAt(path: string) {
    const route = routes.find(candidate => candidate.path === path)
    if (!route) throw new Error(`route not found: ${path}`)
    return route
  }

  it('keeps the same Chat view instance while a draft materializes', () => {
    const chat = routeAt('/chat')
    const draft = routeAt('/chat/new')

    expect(draft.component).toBe(chat.component)
    expect(chat.meta?.viewKey).toBe('chat')
    expect(draft.meta?.viewKey).toBe('chat')
  })

  it('hosts Skills and Channels in one kept-alive destination', () => {
    const skills = routeAt('/skills')
    const channels = routeAt('/channels')

    expect(skills.name).toBe('skills')
    expect(channels.name).toBe('channels')
    expect(channels.component).toBe(skills.component)
    expect(skills.meta?.viewKey).toBe('skills-channels-hub')
    expect(channels.meta?.viewKey).toBe('skills-channels-hub')
    expect(skills.meta?.keepAlive).toBe(true)
    expect(channels.meta?.keepAlive).toBe(true)
    expect(skills.meta?.nav).toBe('primary')
    expect(skills.meta?.navLabelKey).toBe('nav.skillsChannels')
    expect(channels.meta?.nav).toBeUndefined()
  })

  it('keeps runtime Logs and Channels outside the two-tab Overview hub', () => {
    const overview = routeAt('/overview')
    const usage = routeAt('/usage')
    const logs = routeAt('/logs')
    const channels = routeAt('/channels')

    expect(usage.component).toBe(overview.component)
    expect(logs.component).not.toBe(overview.component)
    expect(overview.meta?.viewKey).toBe('overview-hub')
    expect(usage.meta?.viewKey).toBe('overview-hub')
    expect(logs.meta?.viewKey).toBeUndefined()
    expect(logs.meta?.keepAlive).toBe(true)
    expect(overview.meta?.titleKey).toBe('nav.status')
    expect(overview.meta?.navLabelKey).toBeUndefined()
    expect(usage.meta?.navLabelKey).toBe('nav.viewUsage')
    expect(channels.component).not.toBe(overview.component)
    expect(channels.meta?.viewKey).not.toBe('overview-hub')
  })

  it('uses the explicit Status document title without changing canonical route names', () => {
    const titles = ['/overview', '/usage', '/logs'].map((path) => {
      const route = routeAt(path)
      return routeTitle({ name: route.name, meta: route.meta } as unknown as RouteLocationNormalized)
    })

    expect(titles).toEqual(['Status', 'Usage', 'Logs'])
  })

  it('uses the localized task title for the sessions ledger', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const sessions = routeAt('/sessions')

    expect(sessions.meta?.titleKey).toBe('sessions.title')
    expect(routeTitle({
      name: sessions.name,
      meta: sessions.meta,
    } as unknown as RouteLocationNormalized)).toBe('任务')
  })
})
