// @vitest-environment happy-dom
import { describe, it, expect, beforeEach } from 'vitest'
import { isRestorableRoute, saveLastRoute, readLastRoute, LAST_ROUTE_KEY } from './lastRoute'

beforeEach(() => localStorage.clear())

describe('isRestorableRoute', () => {
  it('accepts the known top-level views, including both route hubs', () => {
    for (const p of [
      '/chat', '/sessions', '/channels',
      '/cron', '/skills', '/overview', '/usage', '/logs',
    ]) {
      expect(isRestorableRoute(p)).toBe(true)
    }
  })

  it('rejects root, the chat draft, the settings overlay, and unknown/removed paths', () => {
    for (const p of [
      '/', '/chat/new', '/agents', '/settings', '/settings/router', '/settings/auto',
      // /approvals now redirects to /sessions — restoring it would loop the
      // saved value through a redirect on every launch, so it is not saved.
      '/approvals',
      '/health', '/nope', '/settingsx', '',
    ]) {
      expect(isRestorableRoute(p)).toBe(false)
    }
  })
})

describe('saveLastRoute / readLastRoute', () => {
  it('round-trips a restorable view', () => {
    saveLastRoute('/cron')
    expect(readLastRoute()).toBe('/cron')
    saveLastRoute('/overview')
    expect(readLastRoute()).toBe('/overview')
  })

  it('never persists the settings overlay, and re-validates an already-saved one to null', () => {
    saveLastRoute('/settings/runtime')
    expect(localStorage.getItem(LAST_ROUTE_KEY)).toBeNull()
    // A value written by an older build (pre-fix) is dropped on read, so a user
    // already trapped on /settings after relaunch self-heals to the default.
    localStorage.setItem(LAST_ROUTE_KEY, '/settings/runtime')
    expect(readLastRoute()).toBeNull()
  })

  it('never persists a non-restorable path (draft / root)', () => {
    saveLastRoute('/chat/new')
    expect(localStorage.getItem(LAST_ROUTE_KEY)).toBeNull()
    saveLastRoute('/')
    expect(readLastRoute()).toBeNull()
  })

  it('re-validates on read: a stale/removed saved value yields null (falls back to default)', () => {
    localStorage.setItem(LAST_ROUTE_KEY, '/removed-view')
    expect(readLastRoute()).toBeNull()
  })

  it('drops an Agent page saved by an older build so cold start uses the default', () => {
    localStorage.setItem(LAST_ROUTE_KEY, '/agents')
    expect(readLastRoute()).toBeNull()
  })
})
