// @vitest-environment happy-dom
import { describe, it, expect, beforeEach } from 'vitest'
import { isRestorableRoute, saveLastRoute, readLastRoute, LAST_ROUTE_KEY } from './lastRoute'

beforeEach(() => localStorage.clear())

describe('isRestorableRoute', () => {
  it('accepts the surviving top-level views', () => {
    for (const p of [
      '/chat', '/channels',
      '/cron', '/skills', '/usage',
    ]) {
      expect(isRestorableRoute(p)).toBe(true)
    }
  })

  it('rejects root, the chat draft, the settings overlay, and unknown/removed paths', () => {
    for (const p of [
      '/', '/chat/new', '/agents', '/settings', '/settings/router', '/settings/auto',
      // /approvals redirects to /chat and is not saved.

      '/approvals',
      '/overview', '/logs', '/health', '/nope', '/settingsx', '',
    ]) {
      expect(isRestorableRoute(p)).toBe(false)
    }
  })
})

describe('saveLastRoute / readLastRoute', () => {
  it('round-trips a restorable view', () => {
    saveLastRoute('/cron')
    expect(readLastRoute()).toBe('/cron')
    saveLastRoute('/usage')
    expect(readLastRoute()).toBe('/usage')
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

  it.each(['/overview', '/health'])('migrates a saved %s to Usage', (path) => {
    localStorage.setItem(LAST_ROUTE_KEY, path)
    expect(readLastRoute()).toBe('/usage')
  })

  it('drops a saved Logs page instead of restoring the Settings overlay', () => {
    localStorage.setItem(LAST_ROUTE_KEY, '/logs')
    expect(readLastRoute()).toBeNull()
    saveLastRoute('/logs')
    expect(readLastRoute()).toBeNull()
  })
})
