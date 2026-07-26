import { describe, expect, it } from 'vitest'
import { getNavigationItems, getWorkNavigationSection } from './nav'
import { sharedRoutes } from './sharedRoutes'

// Guards the flat route taxonomy shared by the sidebar rail, mobile drawer,
// and command palette. Long-lived Agent administration remains a direct route,
// but is intentionally outside this primary navigation source.

describe('getWorkNavigationSection', () => {
  it('returns the flat sidebar order after the dedicated chat action', () => {
    expect(getWorkNavigationSection().map((item) => item.path)).toEqual([
      '/sessions',
      '/overview',
      '/skills',
      '/cron',
    ])
  })

  it('excludes Chat and advanced Agent administration', () => {
    const paths = getWorkNavigationSection().map((item) => item.path)
    expect(paths).not.toContain('/chat')
    expect(paths).not.toContain('/agents')
  })

  it('uses the compound label while keeping /skills as the destination', () => {
    const item = getWorkNavigationSection().find(candidate => candidate.path === '/skills')

    expect(item).toMatchObject({
      path: '/skills',
      title: 'Skills & Channels',
      icon: 'skills',
    })
  })

  it('keeps Overview as the rail label while its page title is Status', () => {
    const item = getWorkNavigationSection().find(candidate => candidate.path === '/overview')
    const route = sharedRoutes.find(candidate => candidate.path === '/overview')

    expect(item?.title).toBe('Overview')
    expect(route?.meta?.titleKey).toBe('nav.status')
  })
})

describe('navigation taxonomy invariants', () => {
  it('keeps Agents out of every primary navigation consumer', () => {
    expect(getNavigationItems('primary').map((item) => item.path)).toEqual([
      '/chat',
      '/sessions',
      '/overview',
      '/skills',
      '/cron',
    ])
  })

  it('keeps the /agents deep link while omitting primary-nav metadata', () => {
    const agentsRoute = sharedRoutes.find((route) => route.path === '/agents')
    expect(agentsRoute).toBeDefined()
    expect(agentsRoute?.name).toBe('agents')
    expect(agentsRoute?.component).toBeDefined()
    expect(agentsRoute?.meta?.nav).toBeUndefined()
  })

  it('keeps retired and hub-hosted routes out of the flat navigation', () => {
    const paths = getWorkNavigationSection().map((item) => item.path)
    for (const path of ['/approvals', '/agents', '/channels', '/usage', '/logs']) {
      expect(paths).not.toContain(path)
    }
  })
})
