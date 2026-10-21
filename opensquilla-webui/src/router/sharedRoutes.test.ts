// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter, type RouteLocationNormalized } from 'vue-router'
import i18n from '@/i18n'
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
    localStorage.setItem(LAST_ROUTE_KEY, '/chat')

    expect(defaultRootRedirect()).toBe('/chat')
  })

  it('keeps browser desktop restore behavior', () => {
    localStorage.setItem(LAST_ROUTE_KEY, '/overview')

    expect(defaultRootRedirect()).toBe('/usage')
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

  it('hosts Usage directly without a diagnostic hub', () => {
    const usage = routeAt('/usage')
    const channels = routeAt('/channels')
    expect(usage.component).toBeTypeOf('function')
    expect(usage.meta?.viewKey).toBeUndefined()
    expect(usage.meta?.keepAlive).toBe(true)
    expect(usage.meta?.navLabelKey).toBe('nav.viewUsage')
    expect(channels.component).not.toBe(usage.component)
  })

  it('keeps the Usage document title', () => {
    const route = routeAt('/usage')
    expect(routeTitle({ name: route.name, meta: route.meta } as unknown as RouteLocationNormalized)).toBe('Usage')
  })

  it.each([
    { path: '/overview', target: '/usage' },
    { path: '/health', target: '/usage' },
    { path: '/logs', target: { path: '/settings/gateway', hash: '#logs' } },
  ])('redirects retired $path without loading a page component', ({ path, target }) => {
    const route = routeAt(path)
    expect(route.redirect).toEqual(target)
    expect(route.component).toBeUndefined()
    expect(route.meta?.keepAlive).toBeUndefined()
  })

  it.each([false, true])('preserves a Logs link token and query with an existing route=%s', async existingRoute => {
    const memoryRouter = createRouter({
      history: createMemoryHistory(),
      routes: [
        routeAt('/logs'),
        { path: '/chat', component: { render: () => null } },
        { path: '/settings/:section', component: { render: () => null } },
      ],
    })
    if (existingRoute) await memoryRouter.push('/chat')
    await memoryRouter.push('/logs?token=synthetic-link-token&other=preserved#old-detail')

    expect(memoryRouter.currentRoute.value.path).toBe('/settings/gateway')
    expect(memoryRouter.currentRoute.value.hash).toBe('#logs')
    expect(memoryRouter.currentRoute.value.query).toEqual({
      token: 'synthetic-link-token', other: 'preserved',
    })
  })

  it('keeps the removed sessions page as a chat compatibility redirect', () => {
    const sessions = routeAt('/sessions')
    expect(sessions.redirect).toBe('/chat')
    expect(sessions.component).toBeUndefined()
  })
})
